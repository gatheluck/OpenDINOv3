#!/usr/bin/env bash
# check_no_identifiers.sh — block site-specific identifiers from a public repo.
#
# WHY
#   This repository is public. Git history cannot be erased: once an identifier
#   is committed and pushed, rewriting history does not remove it from existing
#   clones, forks, or provider caches. The only reliable defence is to stop it
#   before the commit lands.
#
# HOW
#   Detect by SHAPE, not by value. This file is itself public, so it must not
#   contain any real identifier to compare against. The patterns describe the
#   form of HPC group IDs, account IDs, reservation queues and absolute site
#   paths.
#
#   Values that cannot be expressed as a shape (a person's name, for example)
#   belong in .identifier-patterns.local, which is git-ignored.
#
# USAGE
#   bash check_no_identifiers.sh            # scan all tracked files (CI)
#   bash check_no_identifiers.sh --staged   # scan staged files (pre-commit)
#
#   Install the hook with:  bash scripts/install_hooks.sh
#
# NOTE
#   Both the hook and CI are required. A hook is not carried by `git clone`,
#   so it misses pushes from other machines. CI alone catches problems only
#   after the push, by which time the history already contains them.

set -uo pipefail

MODE="${1:-all}"

if [ "${MODE}" = "--staged" ]; then
  FILES=$(git diff --cached --name-only --diff-filter=ACM)
else
  FILES=$(git ls-files)
fi

# Exclude this script and the pattern files: they hold the patterns themselves
# and would always match.
FILES=$(printf '%s\n' ${FILES} \
        | grep -v -E 'check_no_identifiers\.sh|\.identifier-(patterns|allow)' \
        || true)

[ -z "${FILES}" ] && { echo "No files to scan."; exit 0; }

# --- Forbidden patterns: shapes only, never literal values -------------------
declare -a PATTERNS=(
  '\bga[a-z][0-9]{5}\b'
  '\b[a-z]{3}[0-9]{5}[a-z]{2}\b'
  '\bR[0-9]{8,}\b'
  '/groups/[a-zA-Z0-9]'
  '/home/[a-zA-Z0-9]'
)
declare -a DESCRIPTIONS=(
  'HPC group or allocation ID (ga + letter + 5 digits)'
  'HPC account ID (3 letters + 5 digits + 2 letters)'
  'Reservation queue name (R + 8 or more digits)'
  'Absolute path under /groups (reveals site layout)'
  'Absolute path under /home (reveals site layout)'
)

# Extra patterns for values a shape cannot capture. One regex per line.
if [ -f .identifier-patterns.local ]; then
  while IFS= read -r line; do
    case "${line}" in ''|\#*) continue ;; esac
    PATTERNS+=("${line}")
    DESCRIPTIONS+=('local pattern from .identifier-patterns.local')
  done < .identifier-patterns.local
fi

# --- Scan --------------------------------------------------------------------
FOUND=0
for i in "${!PATTERNS[@]}"; do
  hits=$(printf '%s\n' ${FILES} | xargs grep -nHE "${PATTERNS[$i]}" 2>/dev/null || true)
  if [ -n "${hits}" ]; then
    FOUND=1
    printf '\nFAIL  %s\n' "${DESCRIPTIONS[$i]}"
    printf '      pattern: %s\n' "${PATTERNS[$i]}"
    printf '%s\n' "${hits}" | head -20 | sed 's/^/      /'
  fi
done

if [ "${FOUND}" -ne 0 ]; then
  cat <<'MSG'

--------------------------------------------------------------------
Site-specific identifiers detected. They must not enter a public repo.

What to do:
  * Replace real values with placeholders such as ${OD_GROUP},
    ${OD_ALLOCATION}, ${OD_RESERVATION}.
  * Keep real values in env.local.sh, which is git-ignored.
  * If this is a false positive, record the reason in
    .identifier-allow.local and exclude it here.

Once committed and pushed, the history cannot be cleaned. Stop here.
--------------------------------------------------------------------
MSG
  exit 1
fi

count=$(printf '%s\n' ${FILES} | wc -l | tr -d ' ')
echo "OK: no site-specific identifiers found (${count} files scanned)."
