# Source only after selecting COYO paths and explicitly deciding face policy.
# 2026-10-27: one-hour scheduling requirement, not a throughput guarantee.
# No face boxes exist in COYO metadata; never silently disable requested blur.
if [ "${OD_BLUR_FACES:-}" != 0 ]; then
  echo 'COYO requires an explicit OD_BLUR_FACES=0 decision; face boxes are unavailable.' >&2
  return 2
fi
for _od_coyo_path in OD_PLAN OD_META_ROOT OD_TASK_ROOT OD_SIF OD_LOGDIR; do
  if [ -z "${!_od_coyo_path:-}" ]; then
    echo "COYO requires explicit ${_od_coyo_path}; no DataComp path fallback." >&2
    unset _od_coyo_path
    return 2
  fi
done
unset _od_coyo_path
export OD_PROD_WALLTIME=01:00:00
export OD_PROCESSES=4 OD_THREADS=32 OD_SAMPLES_PER_SHARD=10000
export OD_TIMEOUT=10 OD_RETRIES=2 OD_MAX_URLS=0
export OD_HTTP_POOL=1 OD_DNS_CACHE=0 OD_DNS_BUDGET=1
export OD_BLUR_FACES=0
