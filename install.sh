#!/usr/bin/env bash
# claude-mem-ops installer (Linux, systemd --user)
set -euo pipefail
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEST="$HOME/.local/share/claude-mem-ops"
CONF="$HOME/.config/claude-mem-ops"
UNITS="$HOME/.config/systemd/user"

echo "== claude-mem-ops installer =="
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required"; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "ERROR: systemd is required (user units)"; exit 1; }
[ -d "$HOME/.claude-mem" ] || echo "WARN: ~/.claude-mem not found — install claude-mem first"

mkdir -p "$DEST" "$CONF" "$UNITS"
cp -a "$REPO_DIR/scripts/." "$DEST/"
cp -a "$REPO_DIR/config/systemd/." "$UNITS/"
if [ ! -f "$CONF/env" ]; then
  cp "$REPO_DIR/config/env.example" "$CONF/env"
  chmod 600 "$CONF/env"
  echo "created $CONF/env — add your API key there"
fi
systemctl --user daemon-reload 2>/dev/null || true

cat <<NEXT

Installed:
  scripts : $DEST
  config  : $CONF/env
  units   : $UNITS/claude-mem-{consolidate,cloud-router}.{service,timer}

Next steps:
  1) edit $CONF/env and set DEEPSEEK_API_KEY (or any OpenAI-compatible key)
  2) merge config/settings.recommended.json into ~/.claude-mem/settings.json,
     then: systemctl --user restart claude-mem
  3) enable consolidation:  systemctl --user enable --now claude-mem-consolidate.timer
  4) optional pressure valve: systemctl --user enable --now claude-mem-cloud-router.timer
  5) DSH users: python3 $DEST/patch-dsh-work-state.py && systemctl --user restart dsh-web
NEXT
