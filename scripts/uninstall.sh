#!/bin/bash
# Remove Claude Code spoken briefs: the launch agent, the Stop hook, and the
# status line if it is the brief segment. Leaves .venv, the model cache
# (~/.cache/huggingface) and ~/.config/claude-spoken-briefs in place.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.claude-spoken-briefs.server"
SETTINGS="$HOME/.claude/settings.json"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Library/Caches/claude-spoken-briefs"
echo "Server agent removed."

if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
  /usr/bin/python3 - "$SETTINGS" <<'PYEDIT'
import json, sys
path = sys.argv[1]
with open(path) as f:
    s = json.load(f)
stop = s.get("hooks", {}).get("Stop", [])
for group in stop:
    group["hooks"] = [h for h in group.get("hooks", [])
                      if "brief-hook.py" not in h.get("command", "")]
stop[:] = [g for g in stop if g["hooks"]]
if "statusline-brief.sh" in s.get("statusLine", {}).get("command", ""):
    del s["statusLine"]
with open(path, "w") as f:
    json.dump(s, f, indent=2)
    f.write("\n")
PYEDIT
  echo "Stop hook removed from $SETTINGS."
fi
grep -qs "statusline-brief.sh" "$HOME"/.claude/*.sh \
  && echo "Your status line script still calls statusline-brief.sh; remove that line."
exit 0
