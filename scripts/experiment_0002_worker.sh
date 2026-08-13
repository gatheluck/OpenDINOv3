#!/usr/bin/env bash
# Worker for experiment 0002: download concurrency within one node.
#
# Runs one download per concurrency level, each over its own pre-cut slice of
# the URL list, and records the wall time. See
# docs/experiments/0002-download-concurrency.md for the protocol, the
# confounders, and what the result cannot answer.
#
# Slicing is NOT done here. scripts/slice_urls.py cuts the list in one pass so
# that a header is interpreted exactly once; re-guessing the format per slice
# would either drop a URL or fetch a column name. This script consumes the
# slices it is given.
#
# Runs inside the container. Writes only under OD_EXP_OUT.
#
# Required environment:
#   OD_SLICE_DIR  directory holding slice_1.txt … slice_N.txt
#   OD_EXP_OUT    writable output directory
# Optional:
#   OD_LEVELS     process counts, one per slice (default "8 32 64 8")
#   OD_THREADS    threads per process           (default 32)

set -uo pipefail

: "${OD_SLICE_DIR:?set OD_SLICE_DIR}"
: "${OD_EXP_OUT:?set OD_EXP_OUT}"
LEVELS="${OD_LEVELS:-8 32 64 8}"
THREADS="${OD_THREADS:-32}"

mkdir -p "${OD_EXP_OUT}"

# The slicer writes parquet when the corpus is parquet, so that img2dataset
# reads the slices the same way it reads production input. Which one it wrote
# is visible from the files themselves rather than assumed here.
if [ -f "${OD_SLICE_DIR}/slice_1.parquet" ]; then
  INPUT_FORMAT="parquet"
  SLICE_EXT="parquet"
  URL_COL=$(cat "${OD_SLICE_DIR}/url_column" 2>/dev/null)
  [ -n "${URL_COL}" ] || { echo "❌ ${OD_SLICE_DIR}/url_column missing" >&2; exit 1; }
elif [ -f "${OD_SLICE_DIR}/slice_1.txt" ]; then
  INPUT_FORMAT="txt"
  SLICE_EXT="txt"
  URL_COL=""
else
  echo "❌ no slice_1.parquet or slice_1.txt in ${OD_SLICE_DIR}" >&2
  exit 1
fi

echo "experiment 0002 — download concurrency"
echo "slices    : ${OD_SLICE_DIR} (${INPUT_FORMAT})"
[ -n "${URL_COL}" ] && echo "url column: ${URL_COL}"
echo "levels    : ${LEVELS}"
echo "threads   : ${THREADS} (held constant so only one variable moves)"
echo "output    : ${OD_EXP_OUT}"
echo "started   : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

run=0
failures=0

for procs in ${LEVELS}; do
  run=$((run + 1))
  slice_file="${OD_SLICE_DIR}/slice_${run}.${SLICE_EXT}"

  if [ ! -s "${slice_file}" ]; then
    echo "run ${run}: ${slice_file} missing or empty; stopping"
    echo "  a level without its own slice cannot be compared to the others"
    break
  fi

  label="run${run}_p${procs}"
  workdir="${OD_EXP_OUT}/${label}"
  mkdir -p "${workdir}"

  if [ "${INPUT_FORMAT}" = "parquet" ]; then
    n=$(python -c "import pyarrow.parquet as pq,sys;print(pq.ParquetFile(sys.argv[1]).metadata.num_rows)" "${slice_file}")
    url_col_args=(--url_col "${URL_COL}")
  else
    n=$(wc -l < "${slice_file}")
    url_col_args=()
  fi

  echo "──────────────────────────────────────────────────────────"
  echo "run ${run}: processes=${procs} threads=${THREADS} urls=${n}"
  echo "slice: ${slice_file}"
  echo "start: $(date -u +%H:%M:%SZ)"

  t0=$(date +%s)
  img2dataset \
    --url_list "${slice_file}" \
    --input_format "${INPUT_FORMAT}" \
    "${url_col_args[@]}" \
    --output_folder "${workdir}/shards" \
    --output_format webdataset \
    --image_size 256 \
    --resize_mode no \
    --processes_count "${procs}" \
    --thread_count "${THREADS}" \
    --number_sample_per_shard 10000 \
    --compute_hash sha256 \
    --timeout 10 \
    --retries 2 \
    --enable_wandb False \
    --incremental_mode incremental \
    > "${workdir}/img2dataset.log" 2>&1
  rc=$?
  t1=$(date +%s)

  echo "exit : ${rc}"
  echo "wall : $((t1 - t0)) s"
  printf '%s\n' "$((t1 - t0))" > "${workdir}/wall_seconds"
  printf '%s\n' "${procs}"     > "${workdir}/processes"
  printf '%s\n' "${rc}"        > "${workdir}/exit_code"

  if [ "${rc}" -ne 0 ]; then
    failures=$((failures + 1))
    echo "  non-zero exit — see ${workdir}/img2dataset.log"
    echo "  partial output is kept; the analysis reports what it finds"
  fi

  echo "end  : $(date -u +%H:%M:%SZ)"
  echo
done

echo "──────────────────────────────────────────────────────────"
echo "finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "runs completed: ${run}, non-zero exits: ${failures}"
echo
echo "Analyse with:"
echo "  python scripts/analyse_experiment_0002.py ${OD_EXP_OUT}"
