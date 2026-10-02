#!/usr/bin/env bash
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)"
dest="${CLAUDE_HOME:-$HOME/.claude}"
mkdir -p "$dest/hooks" "$dest/skills/questioning/scripts" "$dest/vendor/smart-subagents/scripts/ssa"
cp "$src"/hooks/*.py "$dest/hooks/"
cp "$src/skills/questioning/SKILL.md" "$dest/skills/questioning/"
cp "$src/skills/questioning/scripts/record.py" "$dest/skills/questioning/scripts/"
for f in __init__.py jev.py; do
  [ -e "$dest/vendor/smart-subagents/scripts/ssa/$f" ] || cp "$src/vendor/smart-subagents/scripts/ssa/$f" "$dest/vendor/smart-subagents/scripts/ssa/"
done
for f in hooks/delegation-gate.py hooks/questioning-session.py skills/questioning/scripts/record.py; do
  python3 "$dest/$f" --test
done
echo "Installed into $dest. Add the hooks from README.md to $dest/settings.json."
