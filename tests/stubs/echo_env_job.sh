#!/usr/bin/env bash
# Stands in for production_job.sh so the arm's resolved environment is
# observable without running a download.
for v in OD_THREADS OD_RETRIES OD_MAX_URLS OD_TASK_ID_OFFSET; do
  eval "printf '%s=%s\n' ${v} \"\${${v}:-}\""
done
