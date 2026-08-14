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

# Face blurring has no default on purpose. It is irreversible, it applies to
# 902 million images, and it is a legal question rather than a technical one.
# DataComp's own downloader blurs by default; a silent default either way
# would decide that by accident, so the run refuses until it is stated.
if [ "${OD_BLUR_FACES:-unset}" = "unset" ]; then
  {
    echo "❌ OD_BLUR_FACES is not set."
    echo
    echo "   DataComp's own downloader blurs faces by default, using the"
    echo "   face_bboxes column. Blurring cannot be undone without"
    echo "   re-downloading, so it has to be chosen rather than defaulted:"
    echo
    echo "     OD_BLUR_FACES=1   blur, as DataComp does"
    echo "     OD_BLUR_FACES=0   keep the image as fetched"
    echo
    echo "   Either way face_bboxes is stored with the sample, so blurring"
    echo "   later remains possible without fetching anything again."
  } >&2
  exit 2
fi
case "${OD_BLUR_FACES}" in
  0|1) ;;
  *) echo "❌ OD_BLUR_FACES must be 0 or 1, got '${OD_BLUR_FACES}'" >&2; exit 2 ;;
esac

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TASK_DIR=$(printf '%s/task-%06d' "${OD_TASK_ROOT}" "${OD_TASK_ID}")

echo "task        : ${OD_TASK_ID}"
echo "directory   : ${TASK_DIR}"
echo "processes   : ${PROCESSES} (threads ${THREADS})"
echo "shard       : ${SAMPLES_PER_SHARD} samples"
echo "blur faces  : ${OD_BLUR_FACES}"
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
# The same column names DataComp's own download_upstream.py passes — but
# only for the columns this manifest actually has. The manifest carries what
# upstream ships and skips what it does not, so naming a missing column here
# would make img2dataset fail on metadata that is merely older or derivative.
COLUMNS=$(python -c "
import pyarrow.parquet as pq, sys
print(' '.join(pq.ParquetFile(sys.argv[1]).schema_arrow.names))
" "${TASK_DIR}/urls.parquet") || {
  echo "❌ cannot read the manifest schema" >&2; exit 1; }
echo "manifest columns: ${COLUMNS}"

CAPTION_ARGS=() ; EXTRA_ARGS=() ; BLUR_ARGS=() ; REENCODE_ARGS=()
case " ${COLUMNS} " in
  *" text "*) CAPTION_ARGS=(--caption_col text) ;;
  *) echo "⚠️  no caption column: this task's shards will hold no text." >&2 ;;
esac

# Names img2dataset appends to the input schema itself. Carrying an upstream
# column with one of these names puts two fields of the same name in the
# output schema; the writer's buffer is keyed by name, so the duplicate
# collapses to one key that receives two appends per row while every other
# key receives one. The rows misalign and pyarrow raises "Expected bytes, got
# a 'int' object" — at write time, so after a shard has been downloaded in
# full, on every shard, for the whole run.
#
# `width` is an obvious column to want to carry, which is exactly why this
# refuses up front rather than 23 TB later. img2dataset records the real
# decoded size under these names anyway; the manifest's claim is redundant.
RESERVED="key status error_message width height original_width original_height"

keep=""
for column in ${OD_CARRY_COLUMNS:-uid face_bboxes}; do
  case " ${RESERVED} " in
    *" ${column} "*)
      echo "❌ cannot carry the column '${column}': img2dataset writes a" >&2
      echo "   field of that name itself, and two fields with one name" >&2
      echo "   misalign every row at write time." >&2
      echo "   Reserved: ${RESERVED}" >&2
      exit 2 ;;
  esac
  case " ${COLUMNS} " in *" ${column} "*) keep="${keep:+${keep},}\"${column}\"" ;; esac
done
[ -n "${keep}" ] && EXTRA_ARGS=(--save_additional_columns "[${keep}]")

if [ "${OD_BLUR_FACES}" = "1" ]; then
  # Checked before the column check: this is a contradiction in the request
  # itself, true whatever the manifest happens to hold.
  #
  # With --resize_mode no, neither resize branch runs, so nothing sets
  # `encode_needed`. It was already decided by skip_reencode. Turn that on
  # and the resizer blurs the decoded array, then writes the ORIGINAL bytes
  # and reports the blurred array's dimensions: every recorded field looks
  # right and the image is unblurred. No error, no warning.
  #
  # The documented img2dataset recipes for COYO-700M and LAION both pass
  # --skip_reencode=True, so this is one plausible speed optimisation away.
  # Pinned by test_skip_reencode_silently_discards_face_blurring.
  if [ "${OD_SKIP_REENCODE:-0}" = "1" ]; then
    echo "❌ OD_SKIP_REENCODE=1 cannot be combined with OD_BLUR_FACES=1." >&2
    echo "   img2dataset would compute the blur and then write the" >&2
    echo "   original unblurred bytes, silently. Faces would not be" >&2
    echo "   blurred and nothing would say so." >&2
    exit 2
  fi

  case " ${COLUMNS} " in
    *" face_bboxes "*) BLUR_ARGS=(--bbox_col face_bboxes) ;;
    *) echo "❌ OD_BLUR_FACES=1 but the manifest has no face_bboxes column." >&2
       echo "   Blurring was asked for and cannot be done." >&2
       exit 2 ;;
  esac
fi
[ "${OD_SKIP_REENCODE:-0}" = "1" ] && REENCODE_ARGS=(--skip_reencode True)

t0=$(date +%s)
img2dataset \
  --url_list "${TASK_DIR}/urls.parquet" \
  --input_format parquet \
  --url_col url \
  "${CAPTION_ARGS[@]}" \
  "${EXTRA_ARGS[@]}" \
  "${BLUR_ARGS[@]}" \
  "${REENCODE_ARGS[@]}" \
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
