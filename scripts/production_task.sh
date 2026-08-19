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

# Fetch settings, variable because the first wave measured 34.9 URLs/sec/node
# against a model of 277 while using 0.40% of the bandwidth and 0.53 of 192
# cores. That leaves per-request latency as the cost, and these are the knobs
# that move it. The defaults are what that wave ran, so nothing changes unless
# an experiment sets them.
TIMEOUT="${OD_TIMEOUT:-10}"
# retries=0, chosen by experiment 0004 on four equal 100,000-URL slices.
# Measured, not reasoned:
#
#   threads retries   yield    unreachable   URLs/s
#        32       2   64.9%         0.312%    105.3   <- the first wave
#        32       0   64.7%         0.135%    348.4   <- this
#       128       2   64.0%         0.621%    160.3
#       128       0   58.7%         2.993%      —     rejected by the guard
#
# 3.31x the throughput for 0.2 points of yield, and it more than halves the
# unreachable rate rather than raising it. Retrying was close to pure waste:
# the failures are 403, 404 and DNS, which never succeed on a second attempt,
# and each dead URL held a thread for three timeouts instead of one.
#
# 128 threads is worse on both axes and is not a knob worth turning.
RETRIES="${OD_RETRIES:-0}"
# Cap the manifest so an experiment arm finishes inside its walltime. Without
# it an arm measures how long a kill takes, not how fast the setting is.
MAX_URLS="${OD_MAX_URLS:-0}"

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
echo "fetch       : timeout ${TIMEOUT}s, retries ${RETRIES}"
[ "${MAX_URLS}" -gt 0 ] && echo "max urls    : ${MAX_URLS} (capped)"
echo "blur faces  : ${OD_BLUR_FACES}"
echo "started     : $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- already done? -----------------------------------------------------------
# Subjobs get requeued and waves get resubmitted. Re-downloading a finished
# task would waste a node-hour and replace good data with whatever the web
# returns today.
# A task capped by OD_MAX_URLS holds a fraction of the URLs the plan allots
# it. Experiment 0004 left three such tasks marked complete, which would
# have silently dropped 2.7 million URLs from the corpus. The marker records
# what it is, and a partial one does not count as done.
# Checked against the PLAN, not against a flag. Tasks 8-10 carry markers
# written before the flag existed — 100,000 candidates against a plan that
# allots them 1,000,000 — and a flag-based check skips them forever.
#
# A marker that cannot be verified is redone. Resumption makes that nearly
# free: the finished shards are kept, nothing is re-downloaded, and the
# marker is rewritten with the numbers to verify against next time.
if [ -f "${TASK_DIR}/DONE.json" ]; then
  VERDICT=$(python "${REPO}/scripts/is_task_complete.py" \
             "${TASK_DIR}/DONE.json" "${OD_PLAN}" "${OD_TASK_ID}")
  case "${VERDICT}" in
    skip)
      echo "already complete; nothing to do"
      exit 0 ;;
    *)
      echo "⚠️  the marker does not account for the whole task (${VERDICT#redo }); redoing" ;;
  esac
fi

# --- claim the task ----------------------------------------------------------
# Without this, correctness depended on the operator remembering which
# ranges were still in flight. Two subjobs on the same task would each treat
# the other's live output as wreckage and write to the same directory.
#
# `mkdir` is atomic on POSIX and on GPFS, so it is the claim. A lock whose
# owner died — ABCI stops, a node is lost — goes stale and is taken over,
# or the task would be stranded forever.
#
# With this, `--from 0 --to 1387` is always the right command: finished
# tasks skip, live ones are left alone, everything else resumes.
LOCK="${TASK_DIR}/RUNNING.lock"
STALE_AFTER="${OD_LOCK_STALE_SECONDS:-1800}"

mkdir -p "${TASK_DIR}" || { echo "❌ cannot create ${TASK_DIR}" >&2; exit 1; }

if ! mkdir "${LOCK}" 2>/dev/null; then
  age=$(python - "${LOCK}" <<'AGE'
import os, sys, time
try:
    print(int(time.time() - os.path.getmtime(sys.argv[1])))
except OSError:
    print(-1)
AGE
)
  if [ "${age}" -ge 0 ] && [ "${age}" -lt "${STALE_AFTER}" ]; then
    echo "another subjob owns task ${OD_TASK_ID}; leaving it alone"
    [ -f "${LOCK}/owner" ] && sed "s/^/   /" "${LOCK}/owner"
    echo "   claimed ${age}s ago; considered stale after ${STALE_AFTER}s"
    exit 0
  fi
  echo "⚠️  stale lock (${age}s old, limit ${STALE_AFTER}s); taking over"
  rm -rf "${LOCK}" && mkdir "${LOCK}" || {
    echo "❌ cannot take over the lock" >&2; exit 1; }
fi

printf '{"job": "%s", "host": "%s", "pid": %s}\n' \
  "${PBS_JOBID:-manual}" "$(hostname)" "$$" > "${LOCK}/owner"

# Released however this exits, or the task is stranded until it goes stale.
cleanup_lock() { rm -rf "${LOCK}"; }
trap cleanup_lock EXIT INT TERM

# A long task must keep its claim fresh, or a later wave would judge it
# stale and start a second copy.
# stdout and stderr are detached: a background child holding the pipe open
# makes any caller that captures output wait for it, which hung the tests.
( while [ -d "${LOCK}" ]; do sleep 60; touch "${LOCK}" 2>/dev/null; done ) \
  >/dev/null 2>&1 &
HEARTBEAT=$!
trap 'kill ${HEARTBEAT} 2>/dev/null; cleanup_lock' EXIT INT TERM

# --- resume a previous attempt -----------------------------------------------
# This used to move the WHOLE directory aside, so a task killed at the
# walltime with 90 of 100 shards finished re-downloaded all 100 — while
# passing --incremental_mode incremental, which exists to prevent exactly
# that. The flag reads `NNNNN_stats.json`, and the wholesale move deleted
# the state it reads.
#
# Only the finished-but-empty shards have to go, or the 2026-07-28 outage's
# 100 zero-yield shards per task would be inherited and skipped. A shard
# killed mid-write has no `_stats.json` and is already not counted.
if [ -e "${TASK_DIR}" ]; then
  python "${REPO}/scripts/prepare_retry.py" "${TASK_DIR}" \
    --tag "${ATTEMPT_TAG//\//_}" || {
      echo "❌ cannot prepare the previous attempt for retry" >&2; exit 1; }
fi

# --- the URL list ------------------------------------------------------------
echo "──────────────────────────────────────────────────────────"
python "${REPO}/scripts/build_task_manifest.py" \
  --plan "${OD_PLAN}" --task-id "${OD_TASK_ID}" \
  --output "${TASK_DIR}/urls.parquet" || {
    echo "❌ task ${OD_TASK_ID}: could not build the manifest" >&2; exit 1; }

PLANNED_URLS=$(python -c "
import pyarrow.parquet as pq, sys
print(pq.ParquetFile(sys.argv[1]).metadata.num_rows)
" "${TASK_DIR}/urls.parquet") || PLANNED_URLS=0

if [ "${MAX_URLS}" -gt 0 ]; then
  python "${REPO}/scripts/cap_manifest.py" \
    "${TASK_DIR}/urls.parquet" "${MAX_URLS}" || {
      echo "❌ could not cap the manifest" >&2; exit 1; }
fi

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

# img2dataset appends bbox_col to save_additional_columns itself, with no
# deduplication (main.py: `save_additional_columns.append(bbox_col)`).
# Passing it as well puts two fields of that name in the schema and every
# shard dies with:
#     KeyError: 'Field "face_bboxes" exists 2 times in schema'
# When blurring is off there is no bbox_col, so it must be carried here or
# blurring later becomes impossible without re-downloading.
SKIP=""
[ "${OD_BLUR_FACES}" = "1" ] && SKIP="face_bboxes"

keep=""
for column in ${OD_CARRY_COLUMNS:-uid face_bboxes}; do
  [ "${column}" = "${SKIP}" ] && continue
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
# The argv actually used, recorded before the run. DONE.json records what we
# INTENDED; this records what was RUN. They diverge the moment a value is
# hard-coded back into the call, and an experiment that misattributes an
# arm's result to the wrong setting is worse than no experiment.
IMG2DATASET_ARGS=(
  --url_list "${TASK_DIR}/urls.parquet"
  --input_format parquet
  --url_col url
  "${CAPTION_ARGS[@]}"
  "${EXTRA_ARGS[@]}"
  "${BLUR_ARGS[@]}"
  "${REENCODE_ARGS[@]}"
  --output_folder "${TASK_DIR}/shards"
  --output_format webdataset
  --image_size 256
  --resize_mode no
  --processes_count "${PROCESSES}"
  --thread_count "${THREADS}"
  --number_sample_per_shard "${SAMPLES_PER_SHARD}"
  --compute_hash sha256
  --timeout "${TIMEOUT}"
  --retries "${RETRIES}"
  --enable_wandb False
  --incremental_mode incremental
)
# Which downloader fetches the images. The upstream one opens a TCP
# connection and a TLS session per image; ours pools them per host and
# reuses them. Off by default: the upstream path fetched every image in the
# corpus so far, and reuse is new.
#
# The wave at 20 nodes failed on connection count, not bandwidth —
# `Network is unreachable` 35.3%, `timed out` 38.5%, DNS normal at 6.1%, the
# 400 Gbps link at 0.005%. Reuse is the one lever on that mechanism that
# does not need the site to change anything.
if [ "${OD_HTTP_POOL:-0}" = "1" ]; then
  DOWNLOADER=(python "${REPO}/scripts/img2dataset_pooled.py")
else
  DOWNLOADER=(img2dataset)
fi

# Recorded with the argv, not beside it: a throughput number is meaningless
# without knowing which downloader produced it, and DONE.json records only
# what was intended.
printf '%s\n' "${DOWNLOADER[@]}" "${IMG2DATASET_ARGS[@]}" \
  > "${TASK_DIR}/img2dataset.cmd"
"${DOWNLOADER[@]}" "${IMG2DATASET_ARGS[@]}" \
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
        "${PROCESSES}" "${THREADS}" "${SAMPLES_PER_SHARD}" \
        "${TIMEOUT}" "${RETRIES}" "${PLANNED_URLS}" <<'PY'
import json, sys, datetime, pathlib
(task_dir, task_id, wall, procs, threads, sps, timeout, retries,
 planned) = sys.argv[1:10]
health = json.loads((pathlib.Path(task_dir) / "health.json").read_text())
(pathlib.Path(task_dir) / "DONE.json").write_text(json.dumps({
    "task_id": int(task_id),
    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "wall_seconds": int(wall),
    "candidates": health["candidates"],
    "planned_candidates": int(planned),
    # True when the manifest was capped, so this task holds a fraction of
    # what the plan allots it and a later wave must redo it in full.
    "partial": int(planned) > 0 and health["candidates"] < int(planned),
    "successes": health["successes"],
    "yield": health["yield_rate"],
    "settings": {"processes": int(procs), "threads": int(threads),
                 "samples_per_shard": int(sps),
                 "image_size": 256, "resize_mode": "no",
                 "compute_hash": "sha256",
                 "timeout": int(timeout), "retries": int(retries)},
}, indent=1))
PY

echo "task ${OD_TASK_ID} complete"
echo "finished    : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
