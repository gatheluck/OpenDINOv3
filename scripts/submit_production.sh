#!/usr/bin/env bash
# Submit one wave of production tasks as a PBS array job.
#
# Site identifiers are never written here. Everything comes from the
# environment, so this file is safe to publish.
#
#   source <your env file>
#   bash scripts/submit_production.sh --from 0 --to 7 --dry-run
#   bash scripts/submit_production.sh --from 0 --to 7
#
# WHY WAVES
#
# ABCI documents `-J start-stop[:step]` and a 75,000-subjob limit, but does
# not document any way to cap how many subjobs run at once. Submitting the
# range in waves is therefore the only reliable control, and it doubles as
# the ramp: experiment 0003 measured one and two nodes, nothing above, so the
# first wave should be small and its yield checked before the next.
#
# Required:
#   OD_SIF       container image (defaults to opendinov3.sif under OD_OUT_ROOT)
#   OD_PLAN      plan.json from scripts/plan_partition.py --json
#   OD_META_ROOT directory the plan's parquet paths live under
#   OD_LOGDIR    writable directory for batch stdout/stderr
# One of:
#   OD_TASK_ROOT, or OD_OUT_ROOT (writes under datacomp/datacomp_1b/raw_shards)
# Optional:
#   OD_SUBMIT             submitter taking --nodes/--walltime; defaults to
#                         od_qsub.sh in OD_CAPTURE_ROOT
#   OD_PROD_WALLTIME      default 02:00:00 (a task measured 1.01 h)
#   OD_PROCESSES          default 32   (experiment 0002: 64 is 31% worse)
#   OD_THREADS            default 32
#   OD_SAMPLES_PER_SHARD  default 10000

set -uo pipefail

die()  { printf '\n❌ %s\n\n' "$*" >&2; exit 1; }
warn() { printf '\n⚠️  %s\n\n' "$*" >&2; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FROM="" ; TO="" ; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from)    FROM="$2"; shift 2 ;;
    --to)      TO="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *)         die "unknown option: $1" ;;
  esac
done
[ -n "${FROM}" ] && [ -n "${TO}" ] || die "give --from and --to (task ids, inclusive)"
[ "${FROM}" -le "${TO}" ] 2>/dev/null || die "--from must not exceed --to"

# Face blurring has no default, for the same reason production_task.sh has
# none: it is irreversible across 902 million images and it is a legal
# question. It is checked HERE as well because the generated job script is
# the only channel to the compute node — PBS does not forward the submitting
# shell's environment. Left out, the wave queues, waits, starts, and every
# subjob exits 2 on production_task.sh's first check.
if [ "${OD_BLUR_FACES:-unset}" = "unset" ]; then
  {
    echo "❌ OD_BLUR_FACES is not set, so the wave would fail on every node."
    echo
    echo "   DataComp's own downloader blurs faces by default. Blurring"
    echo "   cannot be undone without re-downloading, so state it:"
    echo
    echo "     OD_BLUR_FACES=1   blur, as DataComp does"
    echo "     OD_BLUR_FACES=0   keep the image as fetched"
    echo
    echo "   face_bboxes is stored with the sample either way."
  } >&2
  exit 2
fi
case "${OD_BLUR_FACES}" in
  0|1) ;;
  *) echo "❌ OD_BLUR_FACES must be 0 or 1, got '${OD_BLUR_FACES}'" >&2; exit 2 ;;
esac

WALLTIME="${OD_PROD_WALLTIME:-02:00:00}"
PROCESSES="${OD_PROCESSES:-32}"
THREADS="${OD_THREADS:-32}"
SAMPLES_PER_SHARD="${OD_SAMPLES_PER_SHARD:-10000}"

if [ -z "${OD_SIF:-}" ] && [ -n "${OD_OUT_ROOT:-}" ]; then
  OD_SIF="${OD_OUT_ROOT}/opendinov3.sif"
fi
[ -n "${OD_SIF:-}" ] && [ -f "${OD_SIF}" ] || die "OD_SIF is not set or missing"
[ -n "${OD_LOGDIR:-}" ] || die "OD_LOGDIR is not set. Source your env file first."
[ -n "${OD_PLAN:-}" ] && [ -f "${OD_PLAN}" ] \
  || die "OD_PLAN is not set or missing. Create it with:
   python scripts/plan_partition.py <upstream_metadata> --json plan.json"
[ -n "${OD_META_ROOT:-}" ] && [ -d "${OD_META_ROOT}" ] \
  || die "OD_META_ROOT is not set. It is the directory the plan's parquet
   paths live under; the container must see them at the same path."

TASK_ROOT="${OD_TASK_ROOT:-}"
if [ -z "${TASK_ROOT}" ]; then
  [ -n "${OD_OUT_ROOT:-}" ] || die "set OD_TASK_ROOT, or OD_OUT_ROOT to derive it"
  TASK_ROOT="${OD_OUT_ROOT}/datacomp/datacomp_1b/raw_shards"
fi

SUBMIT="${OD_SUBMIT:-}"
if [ -z "${SUBMIT}" ] && [ -x "${OD_CAPTURE_ROOT:-}/od_qsub.sh" ]; then
  SUBMIT="${OD_CAPTURE_ROOT}/od_qsub.sh"
fi
[ -n "${SUBMIT}" ] || [ "${DRY_RUN}" -eq 1 ] || die "no submitter; set OD_SUBMIT"

mkdir -p "${OD_LOGDIR}" "${TASK_ROOT}" || die "cannot create output directories"

# --- does the plan actually hold this range? ---------------------------------
# A subjob whose task id is outside the plan fails a minute in, after the
# queue wait. Checking here costs a file read.
PLAN_TASKS=$(python3 -c "
import json, sys
plan = json.load(open('${OD_PLAN}'))
print(max((int(t['task_id']) for t in plan.get('tasks', [])), default=-1) + 1)
" 2>/dev/null) || PLAN_TASKS=""
if [ -n "${PLAN_TASKS}" ]; then
  [ "${TO}" -lt "${PLAN_TASKS}" ] \
    || die "the plan holds ${PLAN_TASKS} tasks (0..$((PLAN_TASKS - 1))); --to ${TO} is outside it"
else
  warn "could not read the plan's task count; the range is unchecked"
fi

COUNT=$((TO - FROM + 1))
DONE_ALREADY=$(find "${TASK_ROOT}" -maxdepth 2 -name DONE.json 2>/dev/null | wc -l | tr -d ' ')

JOB="${OD_LOGDIR}/production_job.generated.sh"
{
  echo "#!/usr/bin/env bash"
  echo "# Generated by scripts/submit_production.sh — do not edit."
  echo "# The body below is scripts/production_job.sh, unmodified."
  echo "export OD_SIF=$(printf '%q' "${OD_SIF}")"
  echo "export OD_REPO=$(printf '%q' "${REPO}")"
  echo "export OD_PLAN=$(printf '%q' "${OD_PLAN}")"
  echo "export OD_META_ROOT=$(printf '%q' "${OD_META_ROOT}")"
  echo "export OD_TASK_ROOT=$(printf '%q' "${TASK_ROOT}")"
  echo "export OD_PROCESSES=$(printf '%q' "${PROCESSES}")"
  echo "export OD_THREADS=$(printf '%q' "${THREADS}")"
  echo "export OD_SAMPLES_PER_SHARD=$(printf '%q' "${SAMPLES_PER_SHARD}")"
  echo "export OD_BLUR_FACES=$(printf '%q' "${OD_BLUR_FACES}")"
  echo
  cat "${REPO}/scripts/production_job.sh"
} > "${JOB}"
chmod +x "${JOB}"

cat <<SUMMARY

production wave

  tasks      : ${FROM}..${TO}  (${COUNT} subjobs, 1 node each)
  plan       : ${OD_PLAN}$([ -n "${PLAN_TASKS}" ] && echo " (${PLAN_TASKS} tasks total)")
  metadata   : ${OD_META_ROOT}
  output     : ${TASK_ROOT}
  already done: ${DONE_ALREADY} task(s) carry DONE.json and will be skipped
  per node   : ${PROCESSES} processes x ${THREADS} threads
  shard      : ${SAMPLES_PER_SHARD} samples
  blur faces : ${OD_BLUR_FACES}
  walltime   : ${WALLTIME} per subjob (a task measured 1.01 h)
  image      : ${OD_SIF}
  job file   : ${JOB}
  submitter  : ${SUBMIT:-<none: dry run>}

  Health guard: a subjob that stores too little exits non-zero and does NOT
  write DONE.json, so a later wave retries it. Thresholds and the reasoning
  are in docs/production.md.

SUMMARY

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "dry run — not submitting. Would run:"
  echo "  ${SUBMIT:-<submitter>} --nodes 1 --walltime ${WALLTIME} ${JOB} -- -J ${FROM}-${TO}"
  echo
  exit 0
fi

exec "${SUBMIT}" --nodes 1 --walltime "${WALLTIME}" "${JOB}" -- -J "${FROM}-${TO}"
