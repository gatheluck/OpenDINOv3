#!/usr/bin/env bash
# Runtime contract for the image under Singularity-like constraints.
#
# WHY THESE SPECIFIC CHECKS
#   The cluster runs SingularityCE, not Docker. Singularity differs from
#   Docker's defaults in ways that break images which were only ever tested
#   with `docker run`:
#
#     * the container process runs as the INVOKING user, not root, and that
#       UID usually has no entry in the image's /etc/passwd
#     * the container filesystem is read-only
#     * $HOME is bind-mounted from the host and may not be writable
#
#   An image that installs fine under Docker can still fail on the cluster for
#   any of these. Testing them here, with Docker flags that emulate the same
#   constraints, catches it before a job is submitted.
#
# USAGE
#   bash tests/test_container_runtime.sh [image-tag]

set -uo pipefail

IMAGE="${1:-opendinov3:test}"
PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
ng()  { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"
        [ $# -gt 1 ] && printf '        %s\n' "$2"; }

run_check() { # name, description, docker args...
  local name="$1"; shift
  local out
  if out=$(docker run --rm "$@" 2>&1); then
    ok "${name}"
  else
    ng "${name}" "$(printf '%s' "${out}" | tail -3 | tr '\n' ' ')"
  fi
}

echo
echo "Runtime contract: ${IMAGE}"
echo "------------------------------------------------------------"

# The image must exist before anything else is meaningful.
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "  FAIL  image not found: ${IMAGE}"
  echo "        build it first:  docker build -t ${IMAGE} ."
  exit 1
fi

# 1. Arbitrary UID with no /etc/passwd entry.
#    This is how Singularity runs: as the submitting user.
run_check "runs as an arbitrary unmapped UID" \
  --user 60123:60123 "${IMAGE}" python -c "import img2dataset"

# 2. Read-only root filesystem.
#    Singularity mounts the image read-only. An image that writes into itself
#    at import time fails here.
run_check "runs with a read-only root filesystem" \
  --read-only --tmpfs /tmp "${IMAGE}" python -c "import img2dataset"

# 3. No writable HOME.
#    On the cluster $HOME is bind-mounted and quota-limited; some libraries
#    try to create caches there on import.
run_check "runs with HOME pointing at a non-writable path" \
  --user 60123:60123 --env HOME=/nonexistent "${IMAGE}" \
  python -c "import img2dataset"

# 4. All three constraints at once — closest to the real cluster.
run_check "runs under all Singularity-like constraints combined" \
  --user 60123:60123 --read-only --tmpfs /tmp --env HOME=/nonexistent \
  "${IMAGE}" python -c "import img2dataset, pyarrow, cv2"

# 5. Import must be silent and must not touch the network.
#
#    albumentations, pulled in transitively by img2dataset, calls home to check
#    for updates on import. That matters more than it looks: DNS failures on
#    the target cluster were measured at up to ~15 s (resolver retry timeout),
#    and img2dataset imports once per worker process. A silent import is the
#    only way to be sure nothing blocks on egress.
#
#    Asserting "no output" is deliberately stricter than "exit code 0": the
#    call currently fails open, so the exit code alone would hide it.
out=$(docker run --rm --network none "${IMAGE}" \
      python -c "import img2dataset, cv2, PIL" 2>&1)
if [ -z "${out}" ]; then
  ok "imports silently with networking disabled"
else
  ng "imports silently with networking disabled" \
     "$(printf '%s' "${out}" | tr '\n' ' ' | cut -c1-160)"
fi

# 6. The pinned contract holds inside the image.
#    -p no:cacheprovider because the image is read-only for the runtime user;
#    pytest would otherwise try to write a cache directory into it.
run_check "in-image contract tests pass" \
  "${IMAGE}" python -m pytest -q -p no:cacheprovider \
  /opt/opendinov3/tests/test_image_contract.py

echo "------------------------------------------------------------"
printf '  %d passed, %d failed\n' "${PASS}" "${FAIL}"
[ "${FAIL}" -eq 0 ] || exit 1
