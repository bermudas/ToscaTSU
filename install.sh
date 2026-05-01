#!/usr/bin/env bash
# Install the Claude Code skills (tosca-tsu-parser + tosca-tsu-emitter) into a
# target repository — or into your home directory for a global install.
#
# Usage:
#   ./install.sh /path/to/target-repo    # repo-local install (.claude/skills/ inside the target)
#   ./install.sh ~                       # global install (~/.claude/skills/)
#
# After install, reload skills in your Claude Code session:
#   /reload-plugins
#
# Each .skill file is a zip of the skill folder (SKILL.md + bundled scripts).
# This script just unzips both into the target's .claude/skills/ directory.
# It's idempotent — re-running overwrites the previous install with the
# current dist/ contents.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$ROOT/dist"

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <target-repo-or-home>"
  echo
  echo "Examples:"
  echo "  $0 /path/to/your-project   # repo-local: writes to <project>/.claude/skills/"
  echo "  $0 ~                       # global: writes to ~/.claude/skills/"
  exit 1
fi

TARGET="$1"
if [[ ! -d "$TARGET" ]]; then
  echo "✗ target directory does not exist: $TARGET" >&2
  exit 1
fi

SKILLS_DIR="$TARGET/.claude/skills"
mkdir -p "$SKILLS_DIR"

# Verify the .skill artifacts are present; rebuild if missing
if [[ ! -f "$DIST/tosca-tsu-parser.skill" || ! -f "$DIST/tosca-tsu-emitter.skill" ]]; then
  echo "→ .skill artifacts missing in $DIST; building first"
  "$ROOT/package_skills.sh" >/dev/null
fi

echo "→ Installing skills into $SKILLS_DIR"
for sk in tosca-tsu-parser tosca-tsu-emitter; do
  # Remove any existing copy so the install is clean (don't merge stale files)
  rm -rf "$SKILLS_DIR/$sk"
  unzip -q -o "$DIST/${sk}.skill" -d "$SKILLS_DIR/"
  echo "  ✓ $sk"
done

echo
echo "✓ Done. Two skills installed:"
ls -la "$SKILLS_DIR" | grep "tosca-tsu" || true
echo
echo "Next: in Claude Code, run '/reload-plugins' (or restart) to pick them up."
echo "      The skills will trigger automatically when you discuss .tsu files,"
echo "      Tosca exports, gen_tsu.py, or converting between Tosca and Playwright."
