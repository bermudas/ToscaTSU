#!/usr/bin/env bash
# Build script for the bundled Claude Code skills.
#
# Treats the repo-root Python files as the canonical source. Copies them into
# each skill's scripts/ directory (so the resulting .skill bundle is fully
# self-contained when installed in another project), then runs the
# skill-creator's package_skill.py on each skill and writes the .skill files
# into dist/.
#
# Run after editing any of: parse_tsu.py, gen_tsu.py, spec_to_manifest.py.
#
# Usage:  ./package_skills.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARSER_SKILL="$ROOT/.claude/skills/tosca-tsu-parser"
EMITTER_SKILL="$ROOT/.claude/skills/tosca-tsu-emitter"
DIST="$ROOT/dist"
PACKAGER="$HOME/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown/skills/skill-creator"

# 1. Sync canonical scripts into the skill bundles
echo "→ Syncing canonical scripts into skill bundles"
mkdir -p "$PARSER_SKILL/scripts" "$EMITTER_SKILL/scripts" "$DIST"
cp "$ROOT/parse_tsu.py"         "$PARSER_SKILL/scripts/parse_tsu.py"
cp "$ROOT/gen_tsu.py"           "$EMITTER_SKILL/scripts/gen_tsu.py"
cp "$ROOT/spec_to_manifest.py"  "$EMITTER_SKILL/scripts/spec_to_manifest.py"

# 2. Run the skill-creator packager on each skill
if [[ ! -d "$PACKAGER" ]]; then
  echo "✗ skill-creator plugin not found at $PACKAGER" >&2
  echo "  Install it via Claude Code:  /plugin install skill-creator" >&2
  exit 1
fi

echo "→ Packaging skills into $DIST"
cd "$PACKAGER"
python3 -m scripts.package_skill "$PARSER_SKILL"  "$DIST" | tail -3
python3 -m scripts.package_skill "$EMITTER_SKILL" "$DIST" | tail -3

cd "$ROOT"
echo
echo "✓ Built artifacts:"
ls -la "$DIST"/*.skill
