#!/usr/bin/env bash
# Install the Tosca .tsu Parser/Emitter Claude Code skills directly from
# GitHub — no clone needed.
#
# Usage (one-liner from anywhere):
#   curl -fsSL https://raw.githubusercontent.com/bermudas/ToscaTSU/main/install-from-github.sh | bash
#       installs into <pwd>/.claude/skills/  (current directory's repo-local)
#
#   curl -fsSL https://raw.githubusercontent.com/bermudas/ToscaTSU/main/install-from-github.sh | bash -s -- /path/to/target
#       installs into <target>/.claude/skills/
#
#   curl -fsSL https://raw.githubusercontent.com/bermudas/ToscaTSU/main/install-from-github.sh | bash -s -- ~
#       installs globally into ~/.claude/skills/
#
# Override the source repo with REPO=owner/name :
#   REPO=myfork/ToscaTSU bash install-from-github.sh /path/to/target
#
# Override the branch with BRANCH=mybranch (default: main).

set -euo pipefail

REPO="${REPO:-bermudas/ToscaTSU}"
BRANCH="${BRANCH:-main}"
TARGET="${1:-$PWD}"

if [[ ! -d "$TARGET" ]]; then
  echo "✗ target directory does not exist: $TARGET" >&2
  exit 1
fi

SKILLS_DIR="$TARGET/.claude/skills"
mkdir -p "$SKILLS_DIR"

echo "→ Installing skills from github.com/$REPO@$BRANCH into $SKILLS_DIR"

for sk in tosca-tsu-parser tosca-tsu-emitter; do
  url="https://raw.githubusercontent.com/$REPO/$BRANCH/dist/${sk}.skill"
  tmp="$(mktemp)"
  if ! curl -fsSL "$url" -o "$tmp"; then
    echo "✗ failed to download $url" >&2
    rm -f "$tmp"
    exit 1
  fi
  # Wipe any prior install of this skill so we don't merge stale files
  rm -rf "$SKILLS_DIR/$sk"
  unzip -q -o "$tmp" -d "$SKILLS_DIR/"
  rm -f "$tmp"
  echo "  ✓ $sk"
done

echo
echo "✓ Done. Skills installed:"
ls -1 "$SKILLS_DIR" | grep "tosca-tsu" || true
echo
echo "Next: in Claude Code, run '/reload-plugins' (or restart) to pick them up."
echo "      The skills auto-trigger on prompts about .tsu files, Tosca exports,"
echo "      gen_tsu.py, or converting between Tosca and Playwright."
