# Claude Code spoken briefs

A Stop hook for Claude Code that turns each reply into a short spoken brief, voiced locally on your Mac, available when you click **▶ Listen** in the status line.

Claude Code replies are long. Code blocks, diffs, file paths, tables – useful on screen, impossible to listen to. I run several sessions at once, or step away while Claude works, and coming back means reading a wall of text just to answer three questions: did it finish, what changed, is it waiting on me. Running the raw reply through a normal text-to-speech is worse: it reads markdown syntax and file paths aloud verbatim.

## Why

I built this for myself. A Stop hook catches each reply and sends it to Claude Haiku – through your own `claude` CLI and subscription, no separate API key – which rewrites it as a short brief: what was done, what needs you (a decision, something to test), any open question. It replies in the language of the original response. Then Kokoro-82M, a small open TTS model, voices the brief locally. The audio never leaves your machine.

Nothing plays until you click **▶ Listen**. If a brief is ready while you're in another app, you get a macOS notification with its text. You can optionally set autoplay to play the brief right away when you step away from the terminal, or always.

This started inside my [Kokoro Reader](https://github.com/pcamarajr/kokoro-reader) project, a Chrome extension that reads articles aloud with the same voice. I split it out so it can be installed without the extension.

## What it looks like

After a reply, the status line goes through:

```
◌ brief…          Haiku is writing it, Kokoro is voicing it
▶ Listen (12s)    ready: Cmd-click to play
■ Stop            playing: click again to stop
↻ Replay          played
```

A reply like *"I fixed the token refresh in `src/auth.ts` … ```ts … ``` … Run `npm test` … Should I backport this to 2.x?"*
is heard as: *"I fixed the login bug in the auth file; it was comparing the token
expiry in seconds versus milliseconds. I added a test, so run the test suite to
confirm. Should I also backport this to the 2.x branch?"*

## Requirements

- macOS (Apple Silicon or Intel) with [Homebrew](https://brew.sh)
- Python 3.10–3.12 (`brew install python@3.12`)
- [Claude Code](https://docs.claude.com/en/docs/claude-code) with the `claude` CLI on your `PATH`
- A terminal that supports clickable links (OSC 8): Ghostty, iTerm2, WezTerm or Kitty
- About 2 GB of disk for PyTorch and the model, and ~1.5 GB of RAM while the model is loaded

## Install

```bash
git clone https://github.com/pcamarajr/claude-spoken-briefs.git
cd claude-spoken-briefs
./scripts/install.sh
```

The installer:

1. installs `espeak-ng` and `jq` with Homebrew, and Kokoro into a local `.venv`;
2. starts the TTS server as a login agent on `127.0.0.1:51731`;
3. adds the Stop hook to `~/.claude/settings.json` (backing it up first, keeping
   your other hooks) and sets the status line to refresh every 2 seconds.

Then start a new Claude Code session. The first brief takes ~30 seconds while
the model downloads and loads; after that, a few seconds.

### If you already have a status line

The installer leaves your status line script alone and prints the lines to add.
In short, append the segment's output to what your script prints:

```bash
session_id=$(echo "$input" | jq -r '.session_id')
brief=$(bash /path/to/claude-spoken-briefs/claude/statusline-brief.sh "$session_id")
printf '%s%s\n' "$your_existing_line" "$brief"
```

### tmux

Links inside tmux need hyperlink passthrough. Add to `~/.tmux.conf`:

```
set -as terminal-features ',*:hyperlinks'
```

## Use

| Action | How |
| --- | --- |
| Hear the last reply | Cmd-click **▶ Listen** in the status line |
| Stop it | Click **■ Stop** |
| Hear it again | Click **↻ Replay** |

The link is plain `http://127.0.0.1` (Claude Code drops custom-scheme links from
the status line), so a browser tab flashes open, closes itself, and focus goes
back to your terminal.

## Configure

Create `~/.config/claude-spoken-briefs/config.json`; every key is optional and
changes apply to the next brief:

```json
{
  "speed": 1.1,
  "autoplay": "away",
  "notify": true,
  "voices": {"en": "am_michael", "pt": "pf_dora"}
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `speed` | `1.0` | 0.5–2.0 |
| `autoplay` | `"never"` | `"never"`, `"away"` (when no terminal is the frontmost app) or `"always"` |
| `notify` | `true` | macOS notification when a brief is ready while you're in another app |
| `voices` | `af_heart`, `pf_dora`, `ef_dora`, `ff_siwis`, `if_sara` | Voice per brief language: `en`, `pt`, `es`, `fr`, `it` |
| `terminals` | Ghostty, iTerm2, Terminal, WezTerm, kitty, Alacritty, Warp | App names that count as "at the terminal" |

All voices are listed in [Kokoro's VOICES.md](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md).
The first letter of a voice is its language (`a` American English, `b` British,
`p` Brazilian Portuguese, `e` Spanish, `f` French, `i` Italian).

## How it works

```
Claude Code reply
   │  Stop hook (async, never blocks the session)
   ▼
claude/brief-hook.py ── claude -p --model haiku ──▶ 2–4 sentence brief
   │  POST /brief (bearer token)
   ▼
server/briefs_server.py ── Kokoro-82M ──▶ ~/Library/Caches/claude-spoken-briefs/<session>.wav
   │                                       + <session>.json (state, duration, link key)
   ▼
claude/statusline-brief.sh reads the JSON ──▶ "▶ Listen (12s)" link
   │  click
   ▼
GET /listen/<session>/<key> ──▶ afplay
```

- Short, plain replies (no code, tables or paths) are spoken as they are,
  without calling Haiku.
- The Haiku call runs with no tools, no MCP servers, no hooks and no session
  persistence, so it is fast, cannot recurse, and stays out of `/resume`.
  Headless `claude -p` sessions are skipped.
- The server unloads the model after 30 minutes idle and launchd restarts it
  cheaply; the model loads again on the next brief.
- Briefs are kept for 7 days.

## Privacy and security

- The reply text is sent to Claude Haiku through your own `claude` CLI, the same
  way Claude Code itself works. Nothing else leaves your machine: speech is
  synthesized and played locally.
- The server listens on `127.0.0.1` only. Posting a brief needs a random token
  stored in `~/.config/claude-spoken-briefs/token` (mode 600). Each brief's
  listen link carries its own random key, so a web page cannot trigger playback.

## Troubleshooting

- **No Listen link**: is the server up? `curl http://127.0.0.1:51731/health`.
  Check the log at `~/Library/Logs/claude-spoken-briefs.log`, and that
  `~/.claude/settings.json` has the Stop hook pointing at this checkout.
- **Link shows but isn't clickable**: your terminal lacks OSC 8 support, or
  tmux needs the `hyperlinks` setting above.
- **Moved the repo**: run `./scripts/install.sh` again from the new place.
- **Restart the server**: `launchctl kickstart -k gui/$(id -u)/com.claude-spoken-briefs.server`

## Uninstall

```bash
./scripts/uninstall.sh
```

Removes the login agent, the Stop hook and the cached briefs. It leaves `.venv`,
the model cache (`~/.cache/huggingface`) and your config; delete those by hand
if you want them gone. If your own status line script calls
`statusline-brief.sh`, remove that line.

## License

MIT
