#!/usr/bin/env bash
# install_hooks.sh — install the pre-commit hook.
#
# Run this right after cloning. Hooks live in .git/hooks, which is not part of
# the repository, so `git clone` does not bring them along.
#
#   bash scripts/install_hooks.sh
#
# Both the hook and CI are required:
#   * hook only — not carried by clone, so pushes from other machines slip past
#   * CI only   — catches problems after the push, when history already has them
# The hook stops it locally; CI is the last line of defence.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
HOOK="${ROOT}/.git/hooks/pre-commit"

cat > "${HOOK}" <<'EOF'
#!/usr/bin/env bash
exec bash "$(git rev-parse --show-toplevel)/check_no_identifiers.sh" --staged
EOF

chmod +x "${HOOK}"

echo "Installed pre-commit hook: ${HOOK}"
echo
echo "Verify with:"
echo "  bash check_no_identifiers.sh"
