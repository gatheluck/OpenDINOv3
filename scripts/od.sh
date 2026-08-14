#!/usr/bin/env bash
# Short commands for this project, so nothing long has to be pasted.
#
# WHY THIS EXISTS
#
# A metadata inspection failed because the command split across a line
# boundary when pasted: `singularity exec --bind ...` ended up on one line and
# the image path on the next, so exec received no container. The binds and
# paths belong in a tested script, not in a message.
#
#   source <your env file>
#   bash scripts/od.sh inspect
#   bash scripts/od.sh --dry-run plan
#   bash scripts/od.sh exec <script.py> [args...]
#
# Every subcommand runs inside the container with the same bind set: the
# repository and the predecessor's corpus read-only, our output writable.

set -uo pipefail

die() { printf '\n❌ %s\n\n' "$*" >&2; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && { DRY_RUN=1; shift; }

usage() {
  cat >&2 <<'USAGE'
usage: od.sh [--dry-run] <subcommand> [args...]

  inspect            what the upstream metadata schema holds
  resolution         how large the images are, before downloading any
  verify             does what arrived match what the metadata claimed
  submit --from N --to M   send one production wave to the queue
  plan               partition the metadata into tasks, writing plan.json
  assess <task dir>  whether a finished task is worth keeping
  exec <script> ...  any script in scripts/, with the standard binds

Source your environment file first: OD_ROOT and OD_OUT_ROOT must be set.
USAGE
}

[ $# -ge 1 ] || { usage; exit 2; }

[ -n "${OD_ROOT:-}" ] || die "OD_ROOT is not set. Source your env file first."
[ -n "${OD_OUT_ROOT:-}" ] || die "OD_OUT_ROOT is not set. Source your env file first."

SIF="${OD_SIF:-${OD_OUT_ROOT}/opendinov3.sif}"
[ -f "${SIF}" ] || die "container image not found: ${SIF}"

METADATA="${OD_METADATA:-${OD_ROOT}/datacomp/datacomp_1b/upstream_metadata}"
PRODUCTION="${OD_PRODUCTION:-${OD_OUT_ROOT}/production}"
mkdir -p "${PRODUCTION}" 2>/dev/null

# The corpus is read-only: it is not ours and must not be written to. Only
# our own output is writable.
BINDS=(--bind "${REPO}:/work:ro"
       --bind "${OD_ROOT}:/corpus:ro"
       --bind "${OD_OUT_ROOT}:/out")

run() {   # $@: the command inside the container
  if [ "${DRY_RUN}" -eq 1 ]; then
    printf 'singularity exec'
    printf ' %s' "${BINDS[@]}" "${SIF}" "$@"
    printf '\n'
    return 0
  fi
  singularity exec "${BINDS[@]}" "${SIF}" "$@"
}

# Paths inside the container, derived from the binds above.
in_corpus() { printf '/corpus%s\n' "${1#${OD_ROOT}}"; }
in_out()    { printf '/out%s\n'    "${1#${OD_OUT_ROOT}}"; }

SUBCOMMAND="$1"; shift
case "${SUBCOMMAND}" in
  inspect)
    run python /work/scripts/inspect_metadata.py "$(in_corpus "${METADATA}")" "$@"
    ;;
  resolution)
    run python /work/scripts/measure_resolution.py "$(in_corpus "${METADATA}")" \
      --files "${OD_SAMPLE_FILES:-40}" \
      --json "$(in_out "${PRODUCTION}")/resolution.json" "$@"
    ;;
  verify)
    run python /work/scripts/verify_recorded_sizes.py \
      "$(in_corpus "${OD_SHARDS:-${OD_ROOT}/datacomp/datacomp_1b/raw_shards}")" \
      --files "${OD_SAMPLE_FILES:-40}" \
      --baseline "$(in_out "${PRODUCTION}")/resolution.json" \
      --json "$(in_out "${PRODUCTION}")/verify_sizes.json" "$@"
    ;;
  submit)
    # NOT through the container: qsub does not exist inside the image.
    # OD_PLAN and OD_META_ROOT are derived here because passing them inline
    # makes a 152-character command, which does not survive a paste.
    OD_PLAN="${OD_PLAN:-${PRODUCTION}/plan.json}"
    [ -f "${OD_PLAN}" ] || die "no plan at ${OD_PLAN}. Run: bash scripts/od.sh plan"
    export OD_PLAN
    export OD_META_ROOT="${OD_META_ROOT:-${METADATA}}"
    # Same derivation env.local.sh uses, so the two cannot drift.
    export OD_LOGDIR="${OD_LOGDIR:-${OD_OUT_ROOT}/logs/pbs_stdout}"
    # od.sh consumes --dry-run before dispatch; forward it explicitly or a
    # rehearsal would submit for real.
    [ "${DRY_RUN}" -eq 1 ] && set -- "$@" --dry-run
    exec bash "${REPO}/scripts/submit_production.sh" "$@"
    ;;
  plan)
    run python /work/scripts/plan_partition.py "$(in_corpus "${METADATA}")" \
      --urls-per-task "${OD_URLS_PER_TASK:-1000000}" \
      --json "$(in_out "${PRODUCTION}")/plan.json" "$@"
    ;;
  assess)
    [ $# -ge 1 ] || die "assess needs a task directory"
    target="$1"; shift
    run python /work/scripts/assess_task.py "$(in_out "${target}")" "$@"
    ;;
  exec)
    [ $# -ge 1 ] || die "exec needs a script name from scripts/"
    script="$1"; shift
    run python "/work/scripts/${script}" "$@"
    ;;
  *)
    printf '\n❌ unknown subcommand: %s\n\n' "${SUBCOMMAND}" >&2
    usage
    exit 2
    ;;
esac
