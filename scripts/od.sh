#!/usr/bin/env bash
# Short commands for this project, so nothing long has to be pasted.
#
# WHY THIS EXISTS
#
# A metadata inspection failed because the command split across a line
# boundary when pasted: `singularity exec --bind ...` ended up on one line and
# the image path on the next, so exec received no container. The binds and
# paths belong in a tested script, not in a message.
#
#   source <your env file>
#   bash scripts/od.sh inspect
#   bash scripts/od.sh --dry-run plan
#   bash scripts/od.sh exec <script.py> [args...]
#
# Every subcommand runs inside the container with the same bind set: the
# repository and the predecessor's corpus read-only, our output writable.

set -uo pipefail

die() { printf '\n❌ %s\n\n' "$*" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && { DRY_RUN=1; shift; }

usage() {
  cat >&2 <<'USAGE'
usage: od.sh [--dry-run] <subcommand> [args...]

  inspect            what the upstream metadata schema holds
  resolution         how large the images are, before downloading any
  hosts              how much connection reuse this corpus would allow
  verify             does what arrived match what the metadata claimed
  submit --from N --to M   send one production wave to the queue
  report             does the pilot justify widening the wave
  slow --node-hours H  why a wave is slow, and what would fix it
  experiment         run experiment 0004 (4 fetch-setting arms)
  arms               compare the finished arms and choose a setting
  salvage <task dir>...  mark healthy tasks done without re-downloading
  plan               partition the metadata into tasks, writing plan.json
  assess <task dir>  whether a finished task is worth keeping
  exec <script> ...  any script in scripts/, with the standard binds

Source your environment file first: OD_ROOT and OD_OUT_ROOT must be set.

OD_METADATA picks the corpus to plan from, and may be under either root:
the predecessor's tree (read-only) or our own output, where a corpus we
fetched ourselves has to live. OD_TASK_ROOT picks where its shards go.
USAGE
}

[ $# -ge 1 ] || { usage; exit 2; }

[ -n "${OD_ROOT:-}" ] || die "OD_ROOT is not set. Source your env file first."
[ -n "${OD_OUT_ROOT:-}" ] || die "OD_OUT_ROOT is not set. Source your env file first."

SIF="${OD_SIF:-${OD_OUT_ROOT}/opendinov3.sif}"
[ -f "${SIF}" ] || die "container image not found: ${SIF}"

METADATA="${OD_METADATA:-${OD_ROOT}/datacomp/datacomp_1b/upstream_metadata}"
PRODUCTION="${OD_PRODUCTION:-${OD_OUT_ROOT}/production}"
# Named once: `report`, `slow` and `arms` all read the same tree, and three
# copies of the default is three places to forget when a corpus changes.
TASK_ROOT="${OD_TASK_ROOT:-${OD_OUT_ROOT}/datacomp/datacomp_1b/raw_shards}"
mkdir -p "${PRODUCTION}" 2>/dev/null

# The corpus is read-only: it is not ours and must not be written to. Only
# our own output is writable.
BINDS=(--bind "${REPO}:/work:ro"
       --bind "${OD_ROOT}:/corpus:ro"
       --bind "${OD_OUT_ROOT}:/out")

run() {   # $@: the command inside the container
  if [ "${DRY_RUN}" -eq 1 ]; then
    printf 'singularity exec'
    printf ' %s' "${BINDS[@]}" "${SIF}" "$@"
    printf '\n'
    return 0
  fi
  singularity exec "${BINDS[@]}" "${SIF}" "$@"
}

# Paths inside the container, derived from the binds above.
#
# The bind that contains the path decides the mount. A corpus is not always
# in the predecessor's tree: DataComp's metadata was already on the cluster
# under OD_ROOT, but Re-LAION's is gated and has to be fetched, and the only
# place we may write is our own output root.
#
# `/corpus` used to be prepended whatever the path was. For metadata under
# OD_OUT_ROOT that produced /corpus/groups/... — a directory that exists
# nowhere — and the run went ahead and reported the corpus had no parquet
# files, which is indistinguishable from an empty corpus.
under() {   # $1 path, $2 host root, $3 mount point; prints the mapped path
  local path="$1" root="${2%/}" mount="$3"
  [ -n "${root}" ] || return 1
  [ "${path}" = "${root}" ] && { printf '%s\n' "${mount}"; return 0; }
  # Quoted in the pattern so the match is literal and ends at a separator:
  # a plain string prefix accepts `<root>-backup/...` and maps it into
  # /corpus, where it is a different directory that happens to spell alike.
  case "${path}" in
    "${root}"/*) printf '%s%s\n' "${mount}" "${path#"${root}"}"; return 0 ;;
  esac
  return 1
}

# Callers must check: `die` inside $(...) exits only the subshell, so an
# unchecked call would substitute an empty string and run anyway.
in_container() {
  # Longest root first, so the more specific bind wins if one root is nested
  # inside the other.
  if [ "${#OD_OUT_ROOT}" -gt "${#OD_ROOT}" ]; then
    under "$1" "${OD_OUT_ROOT}" /out && return 0
    under "$1" "${OD_ROOT}" /corpus && return 0
  else
    under "$1" "${OD_ROOT}" /corpus && return 0
    under "$1" "${OD_OUT_ROOT}" /out && return 0
  fi
  die "${1} is not inside any bind.
   The container sees only OD_ROOT at /corpus and OD_OUT_ROOT at /out,
   so there is no path there that would have worked."
}

# Assign rather than substitute inline. `--json "$(in_container "${dir}")/x"`
# reads as if a failure would stop the run, and does the opposite: the `die`
# exits only the subshell, so the argument becomes `/x` and the command goes
# ahead writing inside the container, onto a layer discarded when it exits.
resolve() {   # $1 variable to set, $2 host path
  local mapped
  mapped="$(in_container "$2")" || exit 1
  printf -v "$1" '%s' "${mapped}"
}

# NOT through the container: qsub does not exist inside the image. OD_PLAN
# and OD_META_ROOT are derived here because passing them inline makes a
# 152-character command, which does not survive a paste.
do_submit() {
  OD_PLAN="${OD_PLAN:-${PRODUCTION}/plan.json}"
  [ -f "${OD_PLAN}" ] || die "no plan at ${OD_PLAN}. Run: bash scripts/od.sh plan"
  export OD_PLAN
  export OD_META_ROOT="${OD_META_ROOT:-${METADATA}}"
  # Same derivation env.local.sh uses, so the two cannot drift.
  export OD_LOGDIR="${OD_LOGDIR:-${OD_OUT_ROOT}/logs/pbs_stdout}"
  # od.sh consumes --dry-run before dispatch; forward it explicitly or a
  # rehearsal would submit for real.
  [ "${DRY_RUN}" -eq 1 ] && set -- "$@" --dry-run
  exec bash "${REPO}/scripts/submit_production.sh" "$@"
}

SUBCOMMAND="$1"; shift
case "${SUBCOMMAND}" in
  inspect)
    resolve META_IN "${METADATA}"
    run python /work/scripts/inspect_metadata.py "${META_IN}" "$@"
    ;;
  resolution)
    resolve META_IN "${METADATA}"
    resolve PROD_IN "${PRODUCTION}"
    run python /work/scripts/measure_resolution.py "${META_IN}" \
      --files "${OD_SAMPLE_FILES:-40}" \
      --json "${PROD_IN}/resolution.json" "$@"
    ;;
  hosts)
    resolve META_IN "${METADATA}"
    resolve PROD_IN "${PRODUCTION}"
    run python /work/scripts/measure_host_concentration.py "${META_IN}" \
      --window "${OD_SAMPLES_PER_SHARD:-10000}" \
      --json "${PROD_IN}/host_concentration.json" "$@"
    ;;
  verify)
    resolve SHARDS_IN \
      "${OD_SHARDS:-${OD_ROOT}/datacomp/datacomp_1b/raw_shards}"
    resolve PROD_IN "${PRODUCTION}"
    run python /work/scripts/verify_recorded_sizes.py "${SHARDS_IN}" \
      --files "${OD_SAMPLE_FILES:-40}" \
      --baseline "${PROD_IN}/resolution.json" \
      --json "${PROD_IN}/verify_sizes.json" "$@"
    ;;
  slow)
    resolve TASKS_IN "${TASK_ROOT}"
    resolve PROD_IN "${PRODUCTION}"
    run python /work/scripts/diagnose_throughput.py "${TASKS_IN}" \
      --json "${PROD_IN}/throughput.json" "$@"
    ;;
  report)
    resolve TASKS_IN "${TASK_ROOT}"
    resolve PROD_IN "${PRODUCTION}"
    run python /work/scripts/inspect_pilot.py "${TASKS_IN}" \
      --json "${PROD_IN}/pilot_report.json" "$@"
    ;;
  salvage)
    [ $# -ge 1 ] || die "salvage needs at least one task directory"
    inside=""
    for target in "$@"; do
      resolve TASK_IN "${target}"
      inside="${inside} ${TASK_IN}"
    done
    # shellcheck disable=SC2086 -- the paths are container-side and known
    run python /work/scripts/salvage_task.py ${inside}
    ;;
  arms)
    resolve TASKS_IN "${TASK_ROOT}"
    resolve PROD_IN "${PRODUCTION}"
    run python /work/scripts/compare_arms.py "${TASKS_IN}" \
      --json "${PROD_IN}/arms.json" "$@"
    ;;
  experiment)
    # Four arms on tasks 8..11. Arms are numbered from 1 and map to tasks
    # through the offset experiment_0004_job.sh sets, so the array range is
    # overridden while --from/--to still range-check the plan.
    #
    # Calls do_submit rather than falling through to `submit)`: `;;&`
    # re-tests the remaining patterns against the ORIGINAL word, which is
    # still "experiment", so the fallthrough silently reached the default
    # branch and reported an unknown subcommand.
    export OD_JOB_SCRIPT="${REPO}/scripts/experiment_0004_job.sh"
    export OD_ARRAY_RANGE="1-4"
    # An arm is 100,000 URLs. At the first wave's 34.9 URLs/sec that is 48
    # minutes, and a faster arm is quicker still. Requesting the production
    # default of 12 h would make the arms harder to schedule and more likely
    # to run into the end of the reservation, for no benefit.
    export OD_PROD_WALLTIME="${OD_PROD_WALLTIME:-02:00:00}"
    do_submit --from 8 --to 11 "$@"
    ;;
  submit)
    do_submit "$@"
    ;;
  plan)
    resolve META_IN "${METADATA}"
    resolve PROD_IN "${PRODUCTION}"
    run python /work/scripts/plan_partition.py "${META_IN}" \
      --urls-per-task "${OD_URLS_PER_TASK:-1000000}" \
      --json "${PROD_IN}/plan.json" "$@"
    ;;
  assess)
    [ $# -ge 1 ] || die "assess needs a task directory"
    target="$1"; shift
    resolve TASK_IN "${target}"
    run python /work/scripts/assess_task.py "${TASK_IN}" "$@"
    ;;
  exec)
    [ $# -ge 1 ] || die "exec needs a script name from scripts/"
    script="$1"; shift
    run python "/work/scripts/${script}" "$@"
    ;;
  *)
    printf '\n❌ unknown subcommand: %s\n\n' "${SUBCOMMAND}" >&2
    usage
    exit 2
    ;;
esac
