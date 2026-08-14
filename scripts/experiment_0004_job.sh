#!/usr/bin/env bash
# Experiment 0004: which fetch settings are worth 11,000 node-hours.
#
# Not submitted directly. scripts/submit_experiment_0004.sh prepends the
# resolved configuration, exactly as the production submitter does.
#
# WHY
#
# The first wave measured 34.9 URLs/sec/node against a planning model of
# 277 — 11,041 node-hours for the corpus, 28.8 days on 16 nodes instead of
# 3.7. The cause is not throughput of any resource we own:
#
#   bandwidth   0.50 MB/s per node, 0.40% of a 1 Gbps link
#   CPU         0.53 of 192 cores
#   processes   confirmed all 32 busy on every node
#
# So it is per-request latency. Two explanations fit and neither has been
# measured: --timeout does not bound DNS resolution, and a URL that times
# out once and succeeds on retry is recorded as a plain success with no
# trace of the seconds it burned.
#
# Rather than identify the mechanism, this measures the outcome. Four arms,
# equal slices, one array job:
#
#   1  threads  32, retries 2   the first wave, as a control
#   2  threads 128, retries 2   is it simply not enough in flight?
#   3  threads  32, retries 0   how much are the retries costing?
#   4  threads 128, retries 0   both
#
# If arm 2 is far faster, latency is the cost and concurrency is the fix —
# there is 250x headroom in bandwidth to spend. If it is not, something
# shared is saturated (DNS is the suspect) and more threads will not help.

set -uo pipefail

: "${PBS_ARRAY_INDEX:?experiment 0004 runs as an array job}"

# Arms use tasks 8..11, the first the pilot did not claim. Index 1 -> task 8,
# so the offset production_job.sh subtracts is -7.
export OD_TASK_ID_OFFSET=-7

case "${PBS_ARRAY_INDEX}" in
  1) export OD_THREADS=32  OD_RETRIES=2 ;;
  2) export OD_THREADS=128 OD_RETRIES=2 ;;
  3) export OD_THREADS=32  OD_RETRIES=0 ;;
  4) export OD_THREADS=128 OD_RETRIES=0 ;;
  *) echo "❌ arm ${PBS_ARRAY_INDEX} is not defined; submit -J 1-4" >&2
     exit 2 ;;
esac

# Equal slices, small enough that every arm finishes. At the first wave's
# 34.9 URLs/sec a 100,000-URL slice takes 48 minutes; a four-times-faster
# arm takes 12. Both fit a 2 hour walltime, so no arm is measuring a kill.
export OD_MAX_URLS="${OD_EXP_MAX_URLS:-100000}"

echo "experiment 0004"
echo "  arm       : ${PBS_ARRAY_INDEX}"
echo "  threads   : ${OD_THREADS}"
echo "  retries   : ${OD_RETRIES}"
echo "  urls      : ${OD_MAX_URLS}"
echo "  task      : $((PBS_ARRAY_INDEX - OD_TASK_ID_OFFSET))"

exec bash "${OD_PRODUCTION_JOB:-${OD_REPO}/scripts/production_job.sh}"
