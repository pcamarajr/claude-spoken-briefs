#!/bin/bash
# Install Claude Code spoken briefs.
#
#   1. Installs espeak-ng and jq (Homebrew) and a Python virtualenv with Kokoro.
#   2. Runs the local TTS server as a login agent (launchd) on 127.0.0.1:51731.
#   3. Adds the async Stop hook to ~/.claude/settings.json (backed up first),
#      alongside any hooks you already have, and makes the status line refresh
#      so the Listen link shows up while Claude is idle. If you have no status
#      line, the brief segment becomes yours.
#
# Safe to run again: it updates in place.
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
LABEL="com.claude-spoken-briefs.server"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/claude-spoken-briefs.log"
PORT=51731
SETTINGS="$HOME/.claude/settings.json"
SEGMENT="$ROOT/claude/statusline-brief.sh"

[ "$(uname)" = Darwin ] || { echo "macOS only (playback uses afplay, the agent uses launchd)"; exit 1; }
command -v brew >/dev/null || { echo "Need Homebrew: https://brew.sh"; exit 1; }
command -v claude >/dev/null || echo "Warning: the claude CLI is not on PATH; the hook needs it to write briefs."

# kokoro supports Python 3.10-3.12 only.
SYSPY=""
for v in 3.12 3.11 3.10; do
  if command -v "python$v" >/dev/null; then SYSPY="$(command -v "python$v")"; break; fi
done
[ -n "$SYSPY" ] || { echo "Need Python 3.10-3.12 (brew install python@3.12)"; exit 1; }

command -v espeak-ng >/dev/null || { echo "==> Installing espeak-ng"; brew install espeak-ng; }
command -v jq >/dev/null || { echo "==> Installing jq"; brew install jq; }

if [ ! -x "$PY" ]; then
  echo "==> Creating virtualenv with $SYSPY"
  "$SYSPY" -m venv "$VENV"
fi
echo "==> Installing Python dependencies (first time: a few minutes)"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$ROOT/server/requirements.txt"

echo "==> Installing launch agent $LABEL"
mkdir -p "$(dirname "$AGENT")" "$(dirname "$LOG")"
cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$ROOT/server/briefs_server.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONWARNINGS</key><string>ignore</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardErrorPath</key><string>$LOG</string>
    <key>StandardOutPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT"

for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  sleep 0.5
done
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null \
  || { echo "Server did not start; see $LOG"; exit 1; }
echo "   Server running on http://127.0.0.1:$PORT (log: $LOG)"

echo "==> Updating $SETTINGS"
mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
BACKUP="$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
cp "$SETTINGS" "$BACKUP"
"$PY" - "$SETTINGS" "$PY $ROOT/claude/brief-hook.py" "bash $SEGMENT" <<'PYEDIT'
import json, sys
path, hook_cmd, segment_cmd = sys.argv[1:]
with open(path) as f:
    s = json.load(f)
# Drop any earlier brief hook (another checkout, an old path), then add ours.
stop = s.setdefault("hooks", {}).setdefault("Stop", [])
for group in stop:
    group["hooks"] = [h for h in group.get("hooks", [])
                      if "brief-hook.py" not in h.get("command", "")]
stop[:] = [g for g in stop if g["hooks"]]
stop.append({"hooks": [{"type": "command", "command": hook_cmd, "async": True}]})
line = s.setdefault("statusLine", {"type": "command", "command": segment_cmd})
line.setdefault("refreshInterval", 2)
with open(path, "w") as f:
    json.dump(s, f, indent=2)
    f.write("\n")
print("   Stop hook:", hook_cmd)
print("   statusLine:", line["command"])
PYEDIT
echo "   Backup: $BACKUP"

STATUSLINE_CMD="$(jq -r '.statusLine.command // ""' "$SETTINGS")"
if [ "$STATUSLINE_CMD" != "bash $SEGMENT" ]; then
  cat <<HINT

You already have a status line. Add the Listen link to the end of your script:

  session_id=\$(echo "\$input" | jq -r '.session_id')
  brief=\$(bash "$SEGMENT" "\$session_id")
  printf '%s%s\n' "<your existing line>" "\$brief"
HINT
fi

cat <<NEXT

Done. Start a new Claude Code session; after each reply the status line shows
"▶ Listen (Ns)". Cmd-click it (Ghostty, iTerm2, WezTerm, Kitty).
Inside tmux, links need:  set -as terminal-features ',*:hyperlinks'

Options go in ~/.config/claude-spoken-briefs/config.json, for example:
  {"speed": 1.2, "autoplay": "away", "voices": {"en": "am_michael"}}
NEXT
