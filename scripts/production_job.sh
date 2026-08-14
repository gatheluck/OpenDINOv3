#!/usr/bin/env bash
# PBS array subjob body: download one task on one node.
#
# Not submitted directly. scripts/submit_production.sh prepends the resolved
# configuration and submits the result, so nothing here depends on which
# variables the batch system forwards.
#
# One node per subjob, deliberately. Experiment 0003 measured two nodes at
# 0.97 of one at the same total concurrency, so spreading buys nothing —
# while pbsdsh, cross-node staging and shared scratch produced all four bugs
# in that experiment. Independent single-node subjobs also schedule far
# better on a full cluster and fail independently.
#
# Expects, set by the submit script:
#   OD_SIF OD_REPO OD_PLAN OD_TASK_ROOT OD_META_ROOT OD_BLUR_FACES
#   OD_PROCESSES OD_THREADS OD_SAMPLES_PER_SHARD
#
# EVERY variable the task needs must appear in the --env list below. That
# list is explicit; it is not the host environment. Exporting a variable in
# the job script is NOT enough — proven by scripts/rehearse_pilot.sh, which
# caught OD_BLUR_FACES and OD_META_ROOT missing from it after both had been
# correctly exported. Add a variable here whenever production_task.sh grows
# a new one, and re-run the rehearsal.

set -uo pipefail

# ABCI's qsub refuses an array index of 0:
#
#     qsub: Array job indices must be greater than 0.
#           [-J 0-7]
#
# observed 2026-08-14. PBS Pro accepts 0 elsewhere — HPCMP's documentation
# shows `#PBS -J 0-12:3` running subjob 0 — so this is a site constraint,
# and the machine is the fact.
#
# Plan task ids stay 0-based: the plan describes the data, not the queue.
# submit_production.sh shifts the submitted range up by OD_TASK_ID_OFFSET
# and records it here, so the two numbering schemes meet in exactly one
# place.
if [ -n "${PBS_ARRAY_INDEX:-}" ]; then
  TASK_ID=$((PBS_ARRAY_INDEX - ${OD_TASK_ID_OFFSET:-0}))
  if [ "${TASK_ID}" -lt 0 ]; then
    echo "❌ array index ${PBS_ARRAY_INDEX} minus offset ${OD_TASK_ID_OFFSET:-0}" >&2
    echo "   gives task ${TASK_ID}, which cannot exist. The submitted" >&2
    echo "   range and the offset disagree." >&2
    exit 1
  fi
else
  TASK_ID="${OD_TASK_ID:-}"
fi
if [ -z "${TASK_ID}" ]; then
  echo "❌ no task id: PBS_ARRAY_INDEX is unset and OD_TASK_ID was not given" >&2
  exit 1
fi

echo "host      : $(hostname)"
echo "array idx : ${PBS_ARRAY_INDEX:-n/a} (offset ${OD_TASK_ID_OFFSET:-0})"
echo "task      : ${TASK_ID}"
echo "started   : $(date -u +%Y-%m-%dT%H:%M:%SZ)"

for v in OD_SIF OD_REPO OD_PLAN OD_TASK_ROOT OD_PROCESSES OD_THREADS \
         OD_SAMPLES_PER_SHARD OD_BLUR_FACES OD_META_ROOT; do
  eval "val=\${$v:-}"
  [ -n "${val}" ] || { echo "❌ ${v} is not set" >&2; exit 1; }
done

command -v singularity >/dev/null 2>&1 || {
  echo "❌ singularity not found on this node" >&2; exit 1; }

# $PBS_LOCALDIR is node-local — ABCI's documentation is explicit about that.
# Created here, on the node that will use it. Experiment 0003 lost a run to a
# bind whose source existed only on the first node.
SCRATCH="${PBS_LOCALDIR:-/tmp}/od_production"
mkdir -p "${SCRATCH}/cache" || { echo "❌ cannot create ${SCRATCH}" >&2; exit 1; }
mkdir -p "${OD_TASK_ROOT}" || { echo "❌ cannot create ${OD_TASK_ROOT}" >&2; exit 1; }

PLAN_DIR="${OD_PLAN%/*}"
PLAN_NAME="${OD_PLAN##*/}"

# The plan names upstream parquet files by absolute path, and the container
# must see them at the same path or the manifest cannot be built. Binding the
# metadata root at its own location keeps the plan valid inside and out.
#
# Written as an explicit test rather than ${VAR:?message}: an apostrophe
# inside that message opens a quote context and breaks the whole file.
if [ -z "${OD_META_ROOT:-}" ]; then
  echo "❌ OD_META_ROOT is not set. It is the directory the plan refers to." >&2
  exit 1
fi

singularity exec \
  --bind "${OD_REPO}:/work:ro" \
  --bind "${PLAN_DIR}:/plan:ro" \
  --bind "${OD_META_ROOT}:${OD_META_ROOT}:ro" \
  --bind "${OD_TASK_ROOT}:/tasks" \
  --bind "${SCRATCH}:/scratch" \
  --env "XDG_CACHE_HOME=/scratch/cache" \
  --env "OD_PLAN=/plan/${PLAN_NAME}" \
  --env "OD_TASK_ID=${TASK_ID}" \
  --env "OD_TASK_ROOT=/tasks" \
  --env "OD_PROCESSES=${OD_PROCESSES}" \
  --env "OD_THREADS=${OD_THREADS}" \
  --env "OD_SAMPLES_PER_SHARD=${OD_SAMPLES_PER_SHARD}" \
  --env "OD_ATTEMPT_TAG=${PBS_JOBID:-manual}" \
  --env "OD_BLUR_FACES=${OD_BLUR_FACES}" \
  --env "OD_META_ROOT=${OD_META_ROOT}" \
  --env "OD_TIMEOUT=${OD_TIMEOUT:-10}" \
  --env "OD_RETRIES=${OD_RETRIES:-2}" \
  --env "OD_MAX_URLS=${OD_MAX_URLS:-0}" \
  "${OD_SIF}" bash /work/scripts/production_task.sh
rc=$?

echo "finished  : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "exit      : ${rc}"

if [ "${rc}" -ne 0 ]; then
  echo "❌ task ${TASK_ID} did not complete. It is NOT marked done, so a" >&2
  echo "   later wave will retry it and set this attempt aside." >&2
fi
exit "${rc}"
