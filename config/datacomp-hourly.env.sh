# Source after selecting explicit production paths and the repartitioned plan.
# 2026-10-26: one-hour team scheduling requirement; 100k candidates/task.
# This is not a measured completion-time guarantee. Validate the first job.
export OD_PROD_WALLTIME=01:00:00
export OD_PROCESSES=4 OD_THREADS=32 OD_SAMPLES_PER_SHARD=10000
export OD_BLUR_FACES=1 OD_TIMEOUT=10 OD_RETRIES=2 OD_MAX_URLS=0
export OD_HTTP_POOL=1 OD_DNS_CACHE=0 OD_DNS_BUDGET=1
