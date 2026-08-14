#!/usr/bin/env bash
# Download one task. Runs inside the container, one per array subjob.
#
# Required environment:
#   OD_PLAN               plan.json, shared and read-only
#   OD_TASK_ID            which task; normally $PBS_ARRAY_INDEX
#   OD_TASK_ROOT          writable directory holding task-* directories
# Optional:
#   OD_PROCESSES          default 32   (experiment 0002: 64 is 31% worse)
#   OD_THREADS            default 32
#   OD_SAMPLES_PER_SHARD  default 10000 (163 MB shards at 25.1 KB/image)
#   OD_ATTEMPT_TAG        suffix used when setting a failed attempt aside
#
# Exit 0 only when the task is complete AND its output passed the health
# check. Anything else is non-zero, because a subjob returning success after
# storing nothing is exactly how 474 tasks were lost on 2026-07-28.

set -uo pipefail

: "${OD_PLAN:?set OD_PLAN}"
: "${OD_TASK_ID:?set OD_TASK_ID}"
: "${OD_TASK_ROOT:?set OD_TASK_ROOT}"
PROCESSES="${OD_PROCESSES:-32}"
THREADS="${OD_THREADS:-32}"
SAMPLES_PER_SHARD="${OD_SAMPLES_PER_SHARD:-10000}"
ATTEMPT_TAG="${OD_ATTEMPT_TAG:-${PBS_JOBID:-manual}}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TASK_DIR=$(printf '%s/task-%06d' "${OD_TASK_ROOT}" "${OD_TASK_ID}")

echo "task        : ${OD_TASK_ID}"
echo "directory   : ${TASK_DIR}"
echo "processes   : ${PROCESSES} (threads ${THREADS})"
echo "shard       : ${SAMPLES_PER_SHARD} samples"
echo "started     : $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- already done? -----------------------------------------------------------
# Subjobs get requeued and waves get resubmitted. Re-downloading a finished
# task would waste a node-hour and replace good data with whatever the web
# returns today.
if [ -f "${TASK_DIR}/DONE.json" ]; then
  echo "already complete; nothing to do"
  exit 0
fi

# --- a previous attempt left something behind --------------------------------
# img2dataset's incremental mode skips any shard that already has output, and
# a failed attempt leaves _stats.json files even when it stored nothing. Left
# in place, the retry would skip everything and produce the same empty task.
# Set aside rather than deleted: a failed attempt is evidence.
if [ -e "${TASK_DIR}" ]; then
  SET_ASIDE="${TASK_DIR}.attempt-${ATTEMPT_TAG//\//_}"
  n=1
  while [ -e "${SET_ASIDE}" ]; do
    SET_ASIDE="${TASK_DIR}.attempt-${ATTEMPT_TAG//\//_}-${n}"; n=$((n + 1))
  done
  echo "⚠️  a previous attempt is present; moving it to ${SET_ASIDE##*/}"
  mv "${TASK_DIR}" "${SET_ASIDE}" || {
    echo "❌ cannot set aside the previous attempt" >&2; exit 1; }
fi

mkdir -p "${TASK_DIR}" || { echo "❌ cannot create ${TASK_DIR}" >&2; exit 1; }

# --- the URL list ------------------------------------------------------------
echo "──────────────────────────────────────────────────────────"
python "${REPO}/scripts/build_task_manifest.py" \
  --plan "${OD_PLAN}" --task-id "${OD_TASK_ID}" \
  --output "${TASK_DIR}/urls.parquet" || {
    echo "❌ task ${OD_TASK_ID}: could not build the manifest" >&2; exit 1; }

# --- download ----------------------------------------------------------------
echo "──────────────────────────────────────────────────────────"
t0=$(date +%s)
img2dataset \
  --url_list "${TASK_DIR}/urls.parquet" \
  --input_format parquet \
  --url_col url \
  --output_folder "${TASK_DIR}/shards" \
  --output_format webdataset \
  --image_size 256 \
  --resize_mode no \
  --processes_count "${PROCESSES}" \
  --thread_count "${THREADS}" \
  --number_sample_per_shard "${SAMPLES_PER_SHARD}" \
  --compute_hash sha256 \
  --timeout 10 \
  --retries 2 \
  --enable_wandb False \
  --incremental_mode incremental \
  > "${TASK_DIR}/img2dataset.log" 2>&1
rc=$?
t1=$(date +%s)
echo "img2dataset exit ${rc}, wall $((t1 - t0)) s"

# --- is it worth keeping? ----------------------------------------------------
echo "──────────────────────────────────────────────────────────"
python "${REPO}/scripts/assess_task.py" "${TASK_DIR}" \
  --json "${TASK_DIR}/health.json"
health_rc=$?

if [ "${health_rc}" -ne 0 ]; then
  echo "❌ task ${OD_TASK_ID} rejected; DONE.json not written" >&2
  echo "   The output stays for inspection. A later attempt will set it" >&2
  echo "   aside rather than adding to it." >&2
  exit 1
fi

if [ "${rc}" -ne 0 ]; then
  echo "❌ img2dataset exited ${rc}; not marking the task done" >&2
  exit "${rc}"
fi

# --- done --------------------------------------------------------------------
python - "${TASK_DIR}" "${OD_TASK_ID}" "$((t1 - t0))" \
        "${PROCESSES}" "${THREADS}" "${SAMPLES_PER_SHARD}" <<'PY'
import json, sys, datetime, pathlib
task_dir, task_id, wall, procs, threads, sps = sys.argv[1:7]
health = json.loads((pathlib.Path(task_dir) / "health.json").read_text())
(pathlib.Path(task_dir) / "DONE.json").write_text(json.dumps({
    "task_id": int(task_id),
    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "wall_seconds": int(wall),
    "candidates": health["candidates"],
    "successes": health["successes"],
    "yield": health["yield_rate"],
    "settings": {"processes": int(procs), "threads": int(threads),
                 "samples_per_shard": int(sps),
                 "image_size": 256, "resize_mode": "no",
                 "compute_hash": "sha256", "timeout": 10, "retries": 2},
}, indent=1))
PY

echo "task ${OD_TASK_ID} complete"
echo "finished    : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
