#!/usr/bin/env bash
# Run the whole submission path locally, before spending an ABCI round trip.
#
#   bash scripts/rehearse_pilot.sh
#
# WHY
#
# Blockers were found one at a time, each costing the operator a round trip:
# a variable that did not exist; a plan holding /corpus paths the production
# job never binds; OD_BLUR_FACES missing from the generated job; and the
# `singularity exec --env` list — which is an explicit list, not inheritance
# — omitting both OD_BLUR_FACES and OD_META_ROOT. Every one of them would
# have surfaced here, in one run, on a laptop.
#
# tests/stubs/singularity rewrites bind paths and runs the command on the
# host, isolating neither the filesystem nor the environment — which is
# exactly what let those bugs through. This drives the real chain:
#
#   plan_partition.py (container, corpus bound at /corpus)
#     -> submit_production.sh   generates the job script
#     -> the generated job      production_job.sh
#     -> singularity_docker     real binds, real -e, NO inheritance
#     -> production_task.sh -> img2dataset -> shards
#
# Docker does not inherit the host environment, so this is STRICTER than
# SingularityCE: whatever passes here passes there.
#
# Needs docker and the opendinov3:test image. Takes about a minute.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${OD_TEST_IMAGE:-opendinov3:test}"
WORK="$(mktemp -d)"
SHIM="$(mktemp -d)"
IMAGES=24
FAILURES=0

cleanup() {
  [ -n "${SERVER_PID:-}" ] && kill "${SERVER_PID}" 2>/dev/null
  rm -rf "${SHIM}"
  if [ "${OD_KEEP:-0}" = "1" ]; then
    printf '\nkept: %s\n' "${WORK}"
  else
    rm -rf "${WORK}"
  fi
}
trap cleanup EXIT

step() { printf '\n\033[1m-- %s\033[0m\n' "$*"; }
pass() { printf '  OK   %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; FAILURES=$((FAILURES + 1)); }

dock() { docker run --rm --user "$(id -u):$(id -g)" "$@"; }

# --- fixtures ---------------------------------------------------------------
step "fixtures"
mkdir -p "${WORK}/images" \
         "${WORK}/corpus/datacomp/datacomp_1b/upstream_metadata" \
         "${WORK}/out/production" "${WORK}/out/logs"
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("",0));print(s.getsockname()[1])')

dock -v "${WORK}:/w" "${IMAGE}" python -c "
import io
from PIL import Image
for i in range(${IMAGES}):
    b = io.BytesIO()
    Image.new('RGB', (320, 240), (i * 10 % 256, 90, 160)).save(b, format='JPEG')
    open(f'/w/images/{i:04d}.jpg', 'wb').write(b.getvalue())
" || { fail "could not build images"; exit 1; }

# The URLs must resolve from INSIDE the container, not from the host.
dock -v "${WORK}:/w" "${IMAGE}" python -c "
import pyarrow as pa, pyarrow.parquet as pq
n = ${IMAGES}
pq.write_table(pa.table({
    'uid': [f'{i:032x}' for i in range(n)],
    'url': [f'http://host.docker.internal:${PORT}/{i:04d}.jpg' for i in range(n)],
    'text': [f'caption {i}' for i in range(n)],
    'original_width': [320] * n,
    'original_height': [240] * n,
    'face_bboxes': [[[0.1, 0.1, 0.5, 0.5]] for _ in range(n)],
    'sha256': [f'{i:064x}' for i in range(n)],
}), '/w/corpus/datacomp/datacomp_1b/upstream_metadata/part-00000.parquet')
" || { fail "could not build metadata"; exit 1; }
pass "DataComp-shaped metadata and ${IMAGES} images"

(cd "${WORK}/images" && exec python3 -m http.server "${PORT}") >/dev/null 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  python3 - "${PORT}" <<'PROBE' 2>/dev/null && break
import socket, sys
try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), 0.3).close()
except OSError:
    sys.exit(1)
PROBE
  sleep 0.2
done
pass "image server on ${PORT}"

# --- 1. the plan, made the way od.sh makes it -------------------------------
step "1. plan (in the container, corpus bound at /corpus)"
dock -v "${REPO}:/work" -v "${WORK}/corpus:/corpus" -v "${WORK}/out:/out" \
  "${IMAGE}" python /work/scripts/plan_partition.py \
  /corpus/datacomp/datacomp_1b/upstream_metadata \
  --urls-per-task 8 --json /out/production/plan.json >/dev/null \
  || { fail "planning failed"; exit 1; }

PIECE=$(python3 - "${WORK}/out/production/plan.json" <<'READ'
import json, sys
print(json.load(open(sys.argv[1]))["tasks"][0]["pieces"][0]["path"])
READ
)
case "${PIECE}" in
  /*) fail "the plan records an absolute path: ${PIECE}
       That describes the planning machine, not the data." ;;
  *)  pass "plan records a portable path: ${PIECE}" ;;
esac

# --- 2. the generated job ---------------------------------------------------
step "2. submit_production.sh generates the job"
: > "${WORK}/out/opendinov3.sif"
export OD_SIF="${WORK}/out/opendinov3.sif"
export OD_PLAN="${WORK}/out/production/plan.json"
export OD_META_ROOT="${WORK}/corpus/datacomp/datacomp_1b/upstream_metadata"
export OD_LOGDIR="${WORK}/out/logs"
export OD_OUT_ROOT="${WORK}/out"
export OD_TASK_ROOT="${WORK}/out/raw_shards"
export OD_PROCESSES=2 OD_THREADS=4 OD_SAMPLES_PER_SHARD=4

if env -u OD_BLUR_FACES bash "${REPO}/scripts/submit_production.sh" \
     --from 0 --to 0 --dry-run >/dev/null 2>&1; then
  fail "a wave with no blur choice was accepted"
else
  pass "a wave with no blur choice is refused"
fi

export OD_BLUR_FACES=1
if ! bash "${REPO}/scripts/submit_production.sh" --from 0 --to 0 --dry-run \
       > "${WORK}/submit.log" 2>&1; then
  fail "submit_production.sh failed"; sed 's/^/    /' "${WORK}/submit.log"; exit 1
fi
JOB="${OD_LOGDIR}/production_job.generated.sh"
if grep -q "export OD_BLUR_FACES=1" "${JOB}"; then
  pass "the job script carries the blur choice"
else
  fail "OD_BLUR_FACES is not exported by the generated job"
fi

# The array range, through a qsub that enforces what ABCI's qsub enforces.
# ABCI rejected -J 0-7 with "Array job indices must be greater than 0" only
# after a plan, a wave and a wait; the stub costs nothing.
if OD_SUBMIT="${REPO}/tests/stubs/od_qsub_stub" \
   bash "${REPO}/scripts/submit_production.sh" --from 0 --to 2 \
   > "${WORK}/qsub.log" 2>&1; then
  pass "qsub accepted the array range ($(grep -o '\-J [0-9-]*' "${WORK}/qsub.log" | tail -1))"
else
  fail "qsub rejected the array range"
  sed 's/^/    /' "${WORK}/qsub.log" | tail -6
fi

# --- 3. the subjob, with real container isolation ---------------------------
step "3. the subjob (singularity -> docker, no env inheritance)"
ln -sf "${REPO}/tests/stubs/singularity_docker" "${SHIM}/singularity"
PATH="${SHIM}:${PATH}" PBS_ARRAY_INDEX=1 bash "${JOB}" \
  > "${WORK}/job.log" 2>&1
JOB_RC=$?

# The off-by-one that would silently run the wrong task for every subjob.
if grep -qE "^task      : 0$" "${WORK}/job.log"; then
  pass "PBS index 1 resolved to plan task 0"
else
  fail "PBS index 1 did not resolve to plan task 0"
  grep -E "^(array idx|task)" "${WORK}/job.log" | sed 's/^/    /'
fi

if PATH="${SHIM}:${PATH}" PBS_ARRAY_INDEX=0 bash "${JOB}" >/dev/null 2>&1; then
  fail "array index 0 was accepted; the offset is not being applied"
else
  pass "array index 0 is refused (offset would give a negative task)"
fi

if [ "${JOB_RC}" -eq 0 ]; then
  pass "the subjob completed"
else
  fail "the subjob exited ${JOB_RC}"
  echo "  ---- job output (tail) ----"
  tail -30 "${WORK}/job.log" | sed 's/^/  /'
  echo "  ---------------------------"
fi

if grep -q "blur faces  : 1" "${WORK}/job.log"; then
  pass "OD_BLUR_FACES reached the container"
else
  fail "OD_BLUR_FACES did NOT reach the container
       (singularity --env is an explicit list; exporting is not enough)"
fi

# --- 4. what it produced ----------------------------------------------------
step "4. output"
TASK="${OD_TASK_ROOT}/task-000000"
if [ -f "${TASK}/DONE.json" ]; then
  SUCC=$(python3 - "${TASK}/DONE.json" <<'READ'
import json, sys
print(json.load(open(sys.argv[1]))["successes"])
READ
)
  if [ "${SUCC}" = "8" ]; then pass "DONE.json: 8/8 stored"
  else fail "stored ${SUCC} of 8"; fi
else
  fail "no DONE.json"
fi

if dock -v "${WORK}:/w" "${IMAGE}" python -c "
import glob, sys, tarfile
import pyarrow.parquet as pq
root = '/w/out/raw_shards/task-000000/shards'
tars = sorted(glob.glob(root + '/*.tar'))
if not tars:
    print('no shards'); sys.exit(1)
with tarfile.open(tars[0]) as a:
    suffixes = {n.rsplit('.', 1)[-1] for n in a.getnames()}
print('suffixes:', sorted(suffixes))
missing = {'jpg', 'txt', 'json'} - suffixes
if missing:
    print('missing:', sorted(missing)); sys.exit(1)
names = pq.ParquetFile(sorted(glob.glob(root + '/*.parquet'))[0]).schema_arrow.names
print('columns :', names)
if 'uid' not in names:
    print('uid was not carried'); sys.exit(1)
if names.count('width') != 1:
    print('duplicate width column'); sys.exit(1)
" > "${WORK}/shards.log" 2>&1; then
  pass "shards hold jpg + txt + json; uid carried; no duplicate columns"
  sed 's/^/    /' "${WORK}/shards.log"
else
  fail "shard contents are wrong"
  sed 's/^/    /' "${WORK}/shards.log"
fi

# --- 5. an experiment arm, end to end ---------------------------------------
# The production path was proven here after seven blockers were found one at
# a time on the cluster. The experiment path is a different job body with
# different variables; proving it costs a minute.
step "5. experiment arm 2 (threads 128, retries 0 via arm 4)"
export OD_JOB_SCRIPT="${REPO}/scripts/experiment_0004_job.sh"
export OD_ARRAY_RANGE="1-4"
export OD_EXP_MAX_URLS=8          # the whole fixture, so the arm completes
export OD_TASK_ROOT="${WORK}/out/exp_shards"
if ! bash "${REPO}/scripts/submit_production.sh" --from 0 --to 2 --dry-run \
       > "${WORK}/exp_submit.log" 2>&1; then
  fail "generating the experiment job failed"
  sed 's/^/    /' "${WORK}/exp_submit.log" | tail -6
else
  grep -q "OD_THREADS=128" "${WORK}/exp_submit.log" \
    && pass "the dry run names each arm's settings" \
    || fail "the dry run does not say what the arms differ in"

  EXP_JOB="${OD_LOGDIR}/production_job.generated.sh"
  # Arm 4 -> task 4 - (-7)... the fixture only has tasks 0..2, so run arm 1
  # against the offset the job body sets and check it reaches a real task.
  PATH="${SHIM}:${PATH}" PBS_ARRAY_INDEX=1 bash "${EXP_JOB}" \
    > "${WORK}/exp_job.log" 2>&1
  EXP_RC=$?
  if grep -q "arm       : 1" "${WORK}/exp_job.log"; then
    pass "the arm banner reached the node"
  else
    fail "the experiment job body did not run"
    sed 's/^/    /' "${WORK}/exp_job.log" | tail -12
  fi
  if grep -qE "fetch       : timeout 10s, retries 2" "${WORK}/exp_job.log"; then
    pass "arm 1 settings reached the container"
  else
    fail "arm settings did NOT reach the container"
    grep -E "fetch|threads|max urls" "${WORK}/exp_job.log" | sed 's/^/    /'
  fi
  if grep -q "max urls    : 8 (capped)" "${WORK}/exp_job.log"; then
    pass "OD_MAX_URLS reached the container"
  else
    fail "OD_MAX_URLS did NOT reach the container"
  fi
fi

# --- verdict ----------------------------------------------------------------
printf '\n'
if [ "${FAILURES}" -eq 0 ]; then
  printf '\033[1mOK: the pilot path works end to end. Safe to submit.\033[0m\n\n'
  exit 0
fi
printf '\033[1mFAILED: %d problem(s). Do not submit yet.\033[0m\n\n' "${FAILURES}"
exit 1
