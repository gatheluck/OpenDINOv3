#!/usr/bin/env bash
# Submit experiment 0002 (download concurrency) as a one-node batch job.
#
# Site identifiers are never written here. Everything comes from the
# environment, so this file is safe to publish.
#
#   source <your env file>
#   bash scripts/submit_experiment_0002.sh --dry-run   # inspect first
#   bash scripts/submit_experiment_0002.sh
#
# Required:
#   OD_SIF        container image to run. Defaults to opendinov3.sif under
#                 OD_OUT_ROOT, which is where the setup docs put it.
#   OD_URLS       source URL list (see the discovery hint below if unsure)
#   OD_LOGDIR     writable directory for batch stdout/stderr
# One of:
#   OD_EXP_OUT    output directory, or
#   OD_OUT_ROOT   output root; the experiment writes under experiments/0002
# Submission:
#   OD_SUBMIT     submitter accepting --nodes/--walltime <script>.
#                 Defaults to od_qsub.sh in OD_CAPTURE_ROOT when present,
#                 because that wrapper is what keeps the job inside the
#                 reservation instead of spending points.
# Optional:
#   OD_EXP_WALLTIME  default 02:30:00, the agreed upper bound
#   OD_SLICE         URLs per level, default 200000
#   OD_LEVELS        process counts, default "8 32 64 8"
#   OD_THREADS       threads per process, default 32
#   OD_SAMPLES_PER_SHARD  samples per shard, default 1000. This caps usable
#                    concurrency: img2dataset gives each process one shard
#                    at a time, so levels above the shard count silently
#                    run at the shard count.

set -uo pipefail

die()  { printf '\n❌ %s\n\n' "$*" >&2; exit 1; }
warn() { printf '\n⚠️  %s\n\n' "$*" >&2; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

WALLTIME="${OD_EXP_WALLTIME:-02:30:00}"
SLICE="${OD_SLICE:-200000}"
LEVELS="${OD_LEVELS:-8 32 64 8}"
THREADS="${OD_THREADS:-32}"
# 1,000 keeps 200 shards per level: enough that 64 processes all get work
# and each gets through ~3 shards. See docs for why both matter.
SAMPLES_PER_SHARD="${OD_SAMPLES_PER_SHARD:-1000}"

# --- what we need ------------------------------------------------------------

if [ -z "${OD_SIF:-}" ] && [ -n "${OD_OUT_ROOT:-}" ]; then
  # Where docs/ tells you to put the image after pulling and verifying it.
  OD_SIF="${OD_OUT_ROOT}/opendinov3.sif"
fi
[ -n "${OD_SIF:-}" ] || die "OD_SIF is not set. Point it at the container image."
[ -f "${OD_SIF}" ] || die "OD_SIF does not exist: ${OD_SIF}
   Pull and verify it first, then either place it there or set OD_SIF."

[ -n "${OD_LOGDIR:-}" ] || die "OD_LOGDIR is not set. Source your env file first."

EXP_OUT="${OD_EXP_OUT:-}"
if [ -z "${EXP_OUT}" ]; then
  [ -n "${OD_OUT_ROOT:-}" ] || die "Set OD_EXP_OUT, or OD_OUT_ROOT to derive it from."
  EXP_OUT="${OD_OUT_ROOT}/experiments/0002"
fi

if [ -z "${OD_URLS:-}" ]; then
  printf '\n❌ OD_URLS is not set.\n\n' >&2
  if [ -n "${OD_ROOT:-}" ] && [ -d "${OD_ROOT}" ]; then
    echo "   Candidates under OD_ROOT larger than 1 MB:" >&2
    find "${OD_ROOT}" -maxdepth 5 -type f \
         \( -name '*url*' -o -name '*.parquet' -o -name '*.tsv' -o -name '*.txt' \) \
         -size +1M 2>/dev/null | head -20 >&2 || true
    echo >&2
  fi
  cat >&2 <<'HINT'
   Set OD_URLS to a parquet or text URL list, or to a directory of them,
   and run again.

   ⚠ Prefer a task under raw_shards. Lists under dns_recovery hold URLs
     that already failed DNS once, so their failure profile is not the
     corpus's and a measurement taken on them would not transfer.
HINT
  exit 1
fi
[ -e "${OD_URLS}" ] || die "OD_URLS does not exist: ${OD_URLS}"

case "${OD_URLS}" in
  *dns_recovery*)
    warn_dns_recovery=1 ;;
  *)
    warn_dns_recovery=0 ;;
esac

SUBMIT="${OD_SUBMIT:-}"
if [ -z "${SUBMIT}" ] && [ -x "${OD_CAPTURE_ROOT:-}/od_qsub.sh" ]; then
  SUBMIT="${OD_CAPTURE_ROOT}/od_qsub.sh"
fi
if [ -z "${SUBMIT}" ] && [ "${DRY_RUN}" -eq 0 ]; then
  die "No submitter. Set OD_SUBMIT, or OD_CAPTURE_ROOT to a checkout holding od_qsub.sh."
fi

mkdir -p "${OD_LOGDIR}" "${EXP_OUT}" || die "cannot create ${OD_LOGDIR} or ${EXP_OUT}"

# --- how long this is allowed to take ----------------------------------------
#
# The estimate is arithmetic on a measured rate, not a guess: 200,000 URLs at
# roughly 64% yield and about 223 successes/sec on 8 processes is ~10 minutes
# per level. Four levels is ~40 minutes if nothing scales, and less if it
# does. The walltime is the hard stop on the whole thing.

LEVEL_COUNT=$(printf '%s\n' ${LEVELS} | wc -l | tr -d ' ')
TOTAL_URLS=$((SLICE * LEVEL_COUNT))

# --- can this configuration reach the concurrency it asks for? ---------------
#
# img2dataset gives each process one shard at a time, so a level asking for
# more processes than there are shards silently runs at the shard count. The
# check is cheap and the failure it prevents is invisible in the output.

plan_cmd=()
if command -v python3 >/dev/null 2>&1; then
  # The plan needs no data and no third-party package, so a bare python3 is
  # enough and avoids paying for a container start.
  plan_cmd=(python3 "${REPO}/scripts/plan_experiment_0002.py")
elif command -v singularity >/dev/null 2>&1; then
  plan_cmd=(singularity exec --bind "${REPO}:/work:ro" "${OD_SIF}"
            python /work/scripts/plan_experiment_0002.py)
fi

if [ "${#plan_cmd[@]}" -gt 0 ]; then
  echo "concurrency plan"
  "${plan_cmd[@]}" --slice "${SLICE}" \
      --samples-per-shard "${SAMPLES_PER_SHARD}" --levels "${LEVELS}" \
    | sed 's/^/  /'
  plan_rc=${PIPESTATUS[0]}
  [ "${plan_rc}" -eq 0 ] \
    || die "this configuration would not measure what it claims; not submitting."
  echo
else
  warn "no python3 and no singularity here; skipping the concurrency check."
fi

# --- read the source before spending a submission ----------------------------
#
# The job's first step slices the list. A wrong schema or too few rows fails a
# minute into a reserved node, after the queue wait. Parquet carries its row
# count in metadata, so checking here costs a file open and catches it now.

if command -v singularity >/dev/null 2>&1; then
  if [ -d "${OD_URLS}" ]; then
    url_bind="${OD_URLS}"; url_path="/urls"
  else
    url_bind="${OD_URLS%/*}"; url_path="/urls/${OD_URLS##*/}"
  fi

  echo "reading the URL source…"
  inspect_out=$(singularity exec --bind "${REPO}:/work:ro" \
                  --bind "${url_bind}:/urls:ro" "${OD_SIF}" \
                  python /work/scripts/slice_urls.py "${url_path}" --inspect 2>&1)
  inspect_rc=$?
  printf '%s\n' "${inspect_out}" | sed 's/^/  /'
  [ "${inspect_rc}" -eq 0 ] \
    || die "the URL source cannot be read as a URL list; not submitting."

  rows=$(printf '%s\n' "${inspect_out}" \
         | awk -F': *' '/^rows/ { gsub(/,/, "", $2); print $2 }')
  if [ -n "${rows}" ] && [ "${rows}" -lt "${TOTAL_URLS}" ]; then
    die "${rows} rows is not enough for ${LEVEL_COUNT} slices of ${SLICE} \
(${TOTAL_URLS} needed).
   Either set OD_SLICE=$((rows / LEVEL_COUNT)) or below,
   or point OD_URLS at a directory of task lists so they are concatenated."
  fi
else
  warn "singularity not found here; skipping the source check.
   The job will still check, but only after it starts on a node."
fi

# --- build the job -----------------------------------------------------------

JOB="${OD_LOGDIR}/experiment_0002_job.generated.sh"

{
  echo "#!/usr/bin/env bash"
  echo "# Generated by scripts/submit_experiment_0002.sh — do not edit."
  echo "# The body below is scripts/experiment_0002_job.sh, unmodified."
  echo "export OD_SIF=$(printf '%q' "${OD_SIF}")"
  echo "export OD_REPO=$(printf '%q' "${REPO}")"
  echo "export OD_URLS=$(printf '%q' "${OD_URLS}")"
  echo "export OD_EXP_OUT=$(printf '%q' "${EXP_OUT}")"
  echo "export OD_SLICE=$(printf '%q' "${SLICE}")"
  echo "export OD_LEVELS=$(printf '%q' "${LEVELS}")"
  echo "export OD_THREADS=$(printf '%q' "${THREADS}")"
  echo "export OD_SAMPLES_PER_SHARD=$(printf '%q' "${SAMPLES_PER_SHARD}")"
  echo
  cat "${REPO}/scripts/experiment_0002_job.sh"
} > "${JOB}"
chmod +x "${JOB}"

cat <<SUMMARY

experiment 0002 — download concurrency

  image     : ${OD_SIF}
  repo      : ${REPO}
  urls      : ${OD_URLS}
  output    : ${EXP_OUT}
  levels    : ${LEVELS}   (processes; threads held at ${THREADS})
  shard     : ${SAMPLES_PER_SHARD} samples/shard
  slice     : ${SLICE} URLs per level, ${TOTAL_URLS} total, disjoint
  walltime  : ${WALLTIME}
  job file  : ${JOB}
  submitter : ${SUBMIT:-<none: dry run>}

  Protocol : docs/experiments/0002-download-concurrency.md
  Criteria : registered before the run; the analysis applies them as written.

SUMMARY

if [ "${warn_dns_recovery}" -eq 1 ]; then
  warn "OD_URLS points under dns_recovery.
   Those URLs already failed DNS once, so their failure profile is not the
   corpus's. Throughput and yield measured on them will not transfer to
   production. Use a task under raw_shards unless you mean to study recovery."
fi

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "dry run — not submitting. Would run:"
  echo "  ${SUBMIT:-<submitter>} --nodes 1 --walltime ${WALLTIME} ${JOB}"
  echo
  exit 0
fi

exec "${SUBMIT}" --nodes 1 --walltime "${WALLTIME}" "${JOB}"
