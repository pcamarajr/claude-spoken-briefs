#!/usr/bin/env bash
# Status line segment for Claude Code spoken briefs.
#
#   statusline-brief.sh <session_id>
#   statusline-brief.sh < status-line-json     (usable as the whole status line)
#
# Prints " ▶ Listen (9s)" (or "◌ brief…", "■ Stop", "↻ Replay") as an OSC 8
# link to the local server's /listen/<session>/<key>, which plays or stops the
# brief. (Plain http: Claude Code drops custom-scheme links from the status
# line.) Prints nothing when the session has no brief. Call it from
# your own status line script and append its output; it reads one small JSON
# file, so it is cheap enough for a short refreshInterval.
sid="${1:-$(jq -r '.session_id // ""' 2>/dev/null)}"
[[ "$sid" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || exit 0
file="$HOME/Library/Caches/claude-spoken-briefs/$sid.json"
[ -f "$file" ] || exit 0

read -r state duration key < <(jq -r '"\(.state // "") \(.duration // 0 | round) \(.key // "")"' "$file" 2>/dev/null)
url="http://127.0.0.1:${CLAUDE_BRIEFS_PORT:-51731}/listen/$sid/$key"

GREEN='\033[1;32m'
DIM='\033[2m'
RESET='\033[0m'
link() { printf '\033]8;;%s\a%s\033]8;;\a' "$1" "$2"; }

[ "$state" = writing ] || [ -n "$key" ] || exit 0

case "$state" in
  writing) printf ' %b◌ brief…%b' "$DIM" "$RESET" ;;
  ready)   printf ' %b%s%b' "$GREEN" "$(link "$url" "▶ Listen (${duration}s)")" "$RESET" ;;
  queued|playing) printf ' %b%s%b' "$GREEN" "$(link "$url" "■ Stop")" "$RESET" ;;
  played)  printf ' %b%s%b' "$DIM" "$(link "$url" "↻ Replay")" "$RESET" ;;
esac
