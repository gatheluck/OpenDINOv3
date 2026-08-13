#!/usr/bin/env bash
# Job body for experiment 0003, run on the first of two allocated nodes.
#
# Not submitted directly: scripts/submit_experiment_0003.sh prepends the
# resolved configuration and submits the result, so nothing here depends on
# which variables the batch system forwards.
#
# Three phases, total concurrency held at OD_TOTAL_PROCESSES throughout:
#
#   phase1_single  1 node  × total          (baseline)
#   phase2_multi   N nodes × total/N        (the question)
#   phase3_single  1 node  × total          (drift control)
#
# The single-node phases run FIRST and LAST on purpose. Launching across
# nodes is the fragile part; if it fails we still have a drift measurement
# and a diagnostic instead of a wasted queue slot.
#
# Expects, set by the submit script:
#   OD_SIF OD_REPO OD_URLS OD_EXP_OUT OD_SLICE OD_TOTAL_PROCESSES
#   OD_NODES OD_THREADS OD_SAMPLES_PER_SHARD

set -uo pipefail

echo "host      : $(hostname)"
echo "started   : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

for v in OD_SIF OD_REPO OD_URLS OD_EXP_OUT OD_SLICE OD_TOTAL_PROCESSES \
         OD_NODES OD_THREADS OD_SAMPLES_PER_SHARD; do
  eval "val=\${$v:-}"
  [ -n "${val}" ] || { echo "❌ ${v} is not set" >&2; exit 1; }
  echo "${v} = ${val}"
done
echo

command -v singularity >/dev/null 2>&1 || {
  echo "❌ singularity not found on this node" >&2; exit 1; }

# --- which nodes did we actually get? ---------------------------------------
#
# PBS writes one line per chunk, so a host repeats when it holds more than
# one. Counting lines would report more nodes than were allocated.
if [ -n "${PBS_NODEFILE:-}" ] && [ -f "${PBS_NODEFILE}" ]; then
  mapfile -t HOSTS < <(awk 'NF{print $1}' "${PBS_NODEFILE}" | awk '!seen[$0]++')
else
  HOSTS=("$(hostname)")
fi
echo "allocated nodes: ${#HOSTS[@]} — ${HOSTS[*]}"

if [ "${#HOSTS[@]}" -lt "${OD_NODES}" ]; then
  echo "⚠️  asked for ${OD_NODES} nodes but got ${#HOSTS[@]}." >&2
  echo "    The multi-node phase will be skipped." >&2
fi
echo

SCRATCH="${PBS_LOCALDIR:-/tmp}/od_exp0003"
mkdir -p "${SCRATCH}/cache" "${OD_EXP_OUT}"

# --- a re-run must not mix with the last one --------------------------------
#
# The output directory survives between attempts. Leftovers cost two runs:
# first a stale symlink that staging could not overwrite, and more seriously
# the analysis would happily sum shards from two different runs and report
# the total as one measurement.
#
# Moved aside, not deleted — a previous attempt may hold the only copy of
# something worth reading.
archive_previous() {
  local dest="${OD_EXP_OUT}/previous_${PBS_JOBID:-manual}" n=1 item found=0
  for item in "${OD_EXP_OUT}"/phase* "${OD_EXP_OUT}"/slices_p*; do
    [ -e "${item}" ] && { found=1; break; }
  done
  [ "${found}" -eq 1 ] || return 0

  while [ -e "${dest}" ]; do
    dest="${OD_EXP_OUT}/previous_${PBS_JOBID:-manual}_${n}"; n=$((n + 1))
  done
  mkdir -p "${dest}" || return 1
  for item in "${OD_EXP_OUT}"/phase* "${OD_EXP_OUT}"/slices_p*; do
    [ -e "${item}" ] || continue
    mv "${item}" "${dest}/" || return 1
  done
  echo "⚠️  output directory was not empty; previous attempt moved to ${dest##*/}"
  return 0
}
archive_previous || { echo "❌ cannot set aside the previous attempt" >&2; exit 1; }

if [ -d "${OD_URLS}" ]; then
  URL_BIND="${OD_URLS}"; URL_PATH="/urls"
else
  URL_BIND="${OD_URLS%/*}"; URL_PATH="/urls/${OD_URLS##*/}"
fi

BINDS=(--bind "${OD_REPO}:/work:ro" --bind "${URL_BIND}:/urls:ro"
       --bind "${OD_EXP_OUT}:/out" --bind "${SCRATCH}:/scratch")
# HOME is deliberately NOT passed. SingularityCE refuses it:
#   "Overriding HOME environment variable with SINGULARITYENV_HOME is not
#    permitted"
# so `--env HOME=...` only produces that warning three times per job and
# changes nothing. XDG_CACHE_HOME is accepted and is what the caches follow.
# Home usage is reported below so that this stays a measured fact.
ENVS=(--env "XDG_CACHE_HOME=/scratch/cache")

echo "home before: $(df -h "${HOME}" 2>/dev/null | tail -1)"

PER_NODE_MULTI=$((OD_TOTAL_PROCESSES / OD_NODES))
URLS_PER_NODE_MULTI=$((OD_SLICE / OD_NODES))

# --- the configuration must measure what it claims --------------------------
echo "──────────────────────────────────────────────────────────"
echo "concurrency plan"
singularity exec "${BINDS[@]}" "${OD_SIF}" \
  python /work/scripts/plan_experiment_0003.py \
    --total "${OD_TOTAL_PROCESSES}" --slice "${OD_SLICE}" \
    --samples-per-shard "${OD_SAMPLES_PER_SHARD}" \
    --nodes "1 ${OD_NODES}" || {
      echo "❌ configuration would not measure what it claims" >&2; exit 1; }
echo

# --- cut disjoint slices for every phase, in one pass each ------------------
#
# Offsets so that no phase reuses another's URLs; otherwise one phase warms
# remote caches for the next and the difference includes that.
echo "──────────────────────────────────────────────────────────"
echo "slicing"
slice_phase() {  # $1 outdir  $2 count  $3 n_slices  $4 offset
  singularity exec "${BINDS[@]}" "${ENVS[@]}" "${OD_SIF}" \
    python /work/scripts/slice_urls.py "${URL_PATH}" "/out/$1" \
      --count "$2" --slices "$3" --offset "$4"
}
slice_phase slices_p1 "${OD_SLICE}" 1 0                       || exit 1
slice_phase slices_p2 "${URLS_PER_NODE_MULTI}" "${OD_NODES}" "${OD_SLICE}" || exit 1
slice_phase slices_p3 "${OD_SLICE}" 1 $((OD_SLICE * 2))       || exit 1
echo

# --- run one node's share ----------------------------------------------------
# A generated script per node, with values baked in, so the launcher does not
# have to propagate environment variables. pbsdsh and ssh differ on that.
write_node_script() {   # $1 phase  $2 node index  $3 slice file  $4 processes
  local phase="$1" k="$2" slice_src="$3" procs="$4"
  local dir="${OD_EXP_OUT}/${phase}/node${k}"
  mkdir -p "${dir}"
  # Copies, never links. An absolute symlink does not resolve through the
  # bind mount the worker reads it under; see scripts/stage_node_slices.sh.
  bash "${OD_REPO}/scripts/stage_node_slices.sh" \
    "${OD_EXP_OUT}" "${phase}" "${k}" "${slice_src}" >/dev/null || return 1
  cat > "${dir}/run.sh" <<EOF
#!/usr/bin/env bash
set -uo pipefail
echo "  node${k} on \$(hostname): ${procs} processes"
singularity exec ${BINDS[*]} ${ENVS[*]} \\
  --env OD_SLICE_DIR=/out/${phase}/node${k}/slices \\
  --env OD_EXP_OUT=/out/${phase}/node${k} \\
  --env OD_LEVELS=${procs} \\
  --env OD_THREADS=${OD_THREADS} \\
  --env OD_SAMPLES_PER_SHARD=${OD_SAMPLES_PER_SHARD} \\
  "${OD_SIF}" bash /work/scripts/experiment_0002_worker.sh \\
  > "${dir}/worker.log" 2>&1
echo "  node${k} exit \$?"
EOF
  chmod +x "${dir}/run.sh"
  printf '%s\n' "${dir}/run.sh"
}

run_phase_single() {   # $1 phase  $2 slice file  $3 processes
  local phase="$1" script t0 t1
  echo "── ${phase}: 1 node × $3 processes"
  # Without this the phase ran `bash ""`, printed "wall 0 s", and looked like
  # it had happened.
  if ! script="$(write_node_script "${phase}" 0 "$2" "$3")" || [ -z "${script}" ]
  then
    echo "❌ ${phase}: could not stage the slice; phase not run" >&2
    return 1
  fi
  t0=$(date +%s); bash "${script}"; t1=$(date +%s)
  printf '%s\n' "$((t1 - t0))" > "${OD_EXP_OUT}/${phase}/wall_seconds"
  echo "   wall $((t1 - t0)) s"
}

# --- how do we start work on the other nodes? -------------------------------
#
# ABCI's documentation states that multi-node jobs need rt_HF and lists the
# hosts in $PBS_NODEFILE, but does not recommend a launch mechanism. SSH to
# allocated nodes requires `-v USE_SSH`, which the reservation wrapper does
# not pass. So: detect, and report what was used.
detect_launcher() {
  if command -v pbsdsh >/dev/null 2>&1 && pbsdsh -n 1 -- hostname >/dev/null 2>&1
  then printf 'pbsdsh\n'; return 0; fi
  if ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
       "${HOSTS[1]}" true >/dev/null 2>&1
  then printf 'ssh\n'; return 0; fi
  printf 'none\n'; return 1
}

run_phase_multi() {   # $1 phase  $2 processes-per-node
  local phase="$1" procs="$2" launcher k script t0 t1 pids=()

  # One node needs no launcher: node 0 is this host. Demanding one would
  # skip the phase for a configuration that can run perfectly well.
  if [ "${OD_NODES}" -le 1 ]; then
    launcher="local"
  else
    launcher="$(detect_launcher)" || {
      echo "⚠️  no way to start work on the other nodes (tried pbsdsh, ssh)." >&2
      echo "    Skipping ${phase}. The single-node phases still ran." >&2
      echo "    For ssh, the job needs -v USE_SSH; see the ABCI docs." >&2
      printf 'skipped: no launcher\n' > "${OD_EXP_OUT}/${phase}_SKIPPED"
      return 1
    }
  fi
  echo "── ${phase}: ${OD_NODES} nodes × ${procs} processes (launcher: ${launcher})"

  t0=$(date +%s)
  for k in $(seq 0 $((OD_NODES - 1))); do
    if ! script="$(write_node_script "${phase}" "${k}" \
                     "slices_p2/slice_$((k + 1)).parquet" "${procs}")" \
       || [ -z "${script}" ]; then
      echo "❌ ${phase}: could not stage node${k}; phase not run" >&2
      return 1
    fi
    if [ "${k}" -eq 0 ]; then
      bash "${script}" & pids+=($!)
    elif [ "${launcher}" = "pbsdsh" ]; then
      pbsdsh -n "${k}" -- bash "${script}" & pids+=($!)
    else
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no "${HOSTS[$k]}" \
        bash "${script}" & pids+=($!)
    fi
  done
  # The phase's wall time is the slowest node: a phase is not done until
  # every node is done.
  for pid in "${pids[@]}"; do wait "${pid}"; done
  t1=$(date +%s)
  printf '%s\n' "$((t1 - t0))" > "${OD_EXP_OUT}/${phase}/wall_seconds"
  echo "   wall $((t1 - t0)) s"
}

echo "──────────────────────────────────────────────────────────"
run_phase_single phase1_single "slices_p1/slice_1.parquet" "${OD_TOTAL_PROCESSES}"
echo
run_phase_multi  phase2_multi  "${PER_NODE_MULTI}"
echo
run_phase_single phase3_single "slices_p3/slice_1.parquet" "${OD_TOTAL_PROCESSES}"

echo
echo "──────────────────────────────────────────────────────────"
echo "home after : $(df -h "${HOME}" 2>/dev/null | tail -1)"

# The previous run exited 0 having produced no shards at all, so qstat
# reported success for a job that measured nothing. Count what was written
# and let the exit status say so.
# Only this run's phases. Counting the whole tree would include anything
# archive_previous set aside and report a failed run as a success.
SHARDS=$(find "${OD_EXP_OUT}"/phase* -name '*_stats.json' 2>/dev/null \
         | wc -l | tr -d ' ')
echo "shards written: ${SHARDS}"
if [ "${SHARDS}" -eq 0 ]; then
  echo "❌ no shard was written by any phase. The job did nothing." >&2
  echo "   Look at */node*/worker.log for the reason." >&2
  FINAL_RC=1
else
  FINAL_RC=0
fi

echo "finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo
echo "Analyse on the login node with:"
echo "  singularity exec --bind ${OD_REPO}:/work:ro --bind ${OD_EXP_OUT}:/out:ro \\"
echo "    ${OD_SIF} python /work/scripts/analyse_experiment_0003.py /out"

exit "${FINAL_RC}"
