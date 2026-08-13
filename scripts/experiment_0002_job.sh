#!/usr/bin/env bash
# Job body for experiment 0002, run on a compute node.
#
# This file is not submitted directly. scripts/submit_experiment_0002.sh
# prepends the resolved configuration and submits the result, so that nothing
# here depends on which variables the batch system happens to forward.
#
# Expects, already set by that prologue:
#   OD_SIF        container image
#   OD_REPO       host path to this repository
#   OD_URLS       source URL list, read only
#   OD_EXP_OUT    writable output directory
#   OD_SLICE      URLs per level
#   OD_LEVELS     process counts
#   OD_THREADS    threads per process

set -uo pipefail

echo "host      : $(hostname)"
echo "started   : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "cores     : $(grep -c processor /proc/cpuinfo)"
echo

for v in OD_SIF OD_REPO OD_URLS OD_EXP_OUT OD_SLICE OD_LEVELS OD_THREADS; do
  eval "val=\${$v:-}"
  if [ -z "${val}" ]; then
    echo "❌ ${v} is not set. The submit script should have provided it." >&2
    exit 1
  fi
  echo "${v} = ${val}"
done
echo

command -v singularity >/dev/null 2>&1 || {
  echo "❌ singularity not found on this node" >&2
  exit 1
}
singularity --version

# Node-local scratch. The container image sets HOME=/tmp, but Singularity
# overrides HOME with the user's home directory, and a shared home is the
# wrong place for a download job's caches.
SCRATCH="${PBS_LOCALDIR:-/tmp}/od_exp0002"
mkdir -p "${SCRATCH}/cache"

SLICE_DIR="${OD_EXP_OUT}/slices"
mkdir -p "${SLICE_DIR}"

URL_DIR="${OD_URLS%/*}"
URL_NAME="${OD_URLS##*/}"

LEVEL_COUNT=$(printf '%s\n' ${OD_LEVELS} | wc -l)

bind_args=(
  --bind "${OD_REPO}:/work:ro"
  --bind "${URL_DIR}:/urls:ro"
  --bind "${OD_EXP_OUT}:/out"
  --bind "${SCRATCH}:/scratch"
)
env_args=(
  --env "HOME=/scratch"
  --env "XDG_CACHE_HOME=/scratch/cache"
)

echo "──────────────────────────────────────────────────────────"
echo "step 1: cut ${LEVEL_COUNT} disjoint slices of ${OD_SLICE} URLs"
echo

singularity exec "${bind_args[@]}" "${env_args[@]}" "${OD_SIF}" \
  python /work/scripts/slice_urls.py \
    "/urls/${URL_NAME}" /out/slices \
    --count "${OD_SLICE}" --slices "${LEVEL_COUNT}"
rc=$?

if [ "${rc}" -ne 0 ]; then
  echo "❌ slicing failed (exit ${rc}). Not starting the runs: without" >&2
  echo "   disjoint slices the levels are not comparable." >&2
  exit "${rc}"
fi

echo
echo "──────────────────────────────────────────────────────────"
echo "step 2: run the levels"
echo

singularity exec "${bind_args[@]}" "${env_args[@]}" \
  --env "OD_SLICE_DIR=/out/slices" \
  --env "OD_EXP_OUT=/out" \
  --env "OD_LEVELS=${OD_LEVELS}" \
  --env "OD_THREADS=${OD_THREADS}" \
  "${OD_SIF}" \
  bash /work/scripts/experiment_0002_worker.sh
rc=$?

echo
echo "worker exit: ${rc}"
echo "finished   : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo
echo "Analyse on the login node with:"
echo "  singularity exec --bind ${OD_REPO}:/work:ro --bind ${OD_EXP_OUT}:/out:ro \\"
echo "    ${OD_SIF} python /work/scripts/analyse_experiment_0002.py /out"

exit "${rc}"
