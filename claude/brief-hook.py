#!/usr/bin/env python3
"""Claude Code Stop hook: turn Claude's last reply into a short spoken brief.

Reading a long reply aloud is bad listening (code, tables, paths, markdown),
so Haiku rewrites it as 2-4 spoken sentences: what was done, what needs you,
any open question. The brief goes to the local server (server/briefs_server.py),
which synthesizes it and exposes it to the status line's "Listen" link.

Wire it as an async Stop hook (scripts/install.sh does it for you):

    {"type": "command", "command": "<python> <repo>/claude/brief-hook.py",
     "async": true}

Never blocks or fails the session: every problem exits 0.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

SERVER = "http://127.0.0.1:%s" % os.environ.get("CLAUDE_BRIEFS_PORT", "51731")
TOKEN_FILE = os.path.expanduser("~/.config/claude-spoken-briefs/token")
HAIKU = "claude-haiku-4-5-20251001"
HAIKU_TIMEOUT = 90
MAX_REPLY = 8000
MAX_REQUEST = 1500
# Replies this short with no code or tables are spoken as they are.
PLAIN_LIMIT = 280

SYSTEM_PROMPT = """You write spoken briefs of a coding assistant's reply. The \
developer will LISTEN to the brief instead of reading the reply, so it must \
sound natural read aloud.

Rules:
- 2 to 4 short sentences, 60 words at most, of plain spoken prose. Output only
  the brief. Keep only what matters; the developer can read the details.
- No markdown, lists, code, file paths, URLs, command lines or IDs. Refer to \
things by what they are ("the server file", "the install script").
- Cover, in this order: what was done or found; what needs the developer \
(a decision, an action, something to review or test); any open question. \
Skip parts that do not apply. If the reply mainly asks a question, say the \
question.
- Address the developer as "you". Speak about the assistant as "I".
- Write in the same language as the reply."""


def post(path, payload, token):
    req = urllib.request.Request(
        SERVER + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token})
    # launchd may be restarting the server after an idle exit.
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except OSError:
            time.sleep(1 + attempt)
    return False


def block_text(content):
    """Text of a message's content, ignoring tool calls and results."""
    if isinstance(content, str):
        return content
    return "\n\n".join(b.get("text", "") for b in content or []
                       if isinstance(b, dict) and b.get("type") == "text")


def last_turn(transcript_path):
    """(user request, [assistant texts]) for the last turn in the transcript."""
    request, replies = "", []
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return request, replies
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        msg = entry.get("message") or {}
        if entry.get("type") == "user":
            text = block_text(msg.get("content")).strip()
            # Tool results are user entries too; only real prompts start a turn.
            if text and not text.startswith("<"):
                request, replies = text, []
        elif entry.get("type") == "assistant":
            text = block_text(msg.get("content")).strip()
            if text:
                replies.append(text)
    return request, replies


def looks_plain(text):
    return (len(text) <= PLAIN_LIMIT and "```" not in text
            and not re.search(r"^\s*\|", text, re.M)
            and not re.search(r"[\w.-]+/[\w./-]+", text))


def strip_markdown(text):
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def write_brief(request, reply, earlier):
    prompt = ""
    if request:
        prompt += "The developer asked:\n" + request[:MAX_REQUEST] + "\n\n"
    if earlier:
        prompt += ("Notes the assistant wrote while working:\n"
                   + earlier[-2000:] + "\n\n")
    prompt += "The assistant's final reply:\n" + reply[-MAX_REPLY:]
    env = dict(os.environ, CLAUDE_SPOKEN_BRIEF="1")
    # These flags are load-bearing: without --tools "" the sub-session starts
    # exploring the repo and hangs; --setting-sources "" loads no hooks (so no
    # recursion); --no-session-persistence keeps it out of /resume.
    try:
        out = subprocess.run(
            ["claude", "-p", "--model", HAIKU, "--tools", "",
             "--strict-mcp-config", "--setting-sources", "",
             "--no-session-persistence", "--disable-slash-commands",
             "--system-prompt", SYSTEM_PROMPT],
            input=prompt, capture_output=True, text=True, env=env,
            timeout=HAIKU_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return strip_markdown(out.stdout) if out.returncode == 0 else ""


def main():
    if os.environ.get("CLAUDE_SPOKEN_BRIEF"):
        return
    # Headless `claude -p` runs have no status line to listen from.
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        return
    try:
        hook = json.load(sys.stdin)
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    except (OSError, ValueError):
        return
    if hook.get("stop_hook_active"):
        return
    sid = str(hook.get("session_id", ""))
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", sid):
        return

    request, replies = last_turn(hook.get("transcript_path", ""))
    reply = (hook.get("last_assistant_message") or "").strip()
    if not reply and replies:
        reply = replies[-1]
    if not reply:
        return
    earlier = "\n\n".join(r for r in replies if r != reply)

    post("/brief", {"session": sid, "state": "writing"}, token)
    if looks_plain(reply):
        brief = strip_markdown(reply)
    else:
        brief = write_brief(request, reply, earlier)
    if not brief:
        post("/brief", {"session": sid, "state": "failed"}, token)
        return
    project = os.path.basename(hook.get("cwd") or "") or "Claude"
    # The terminal's bundle id (inherited through tmux too), so the server
    # can refocus it after the Listen link opens a browser tab.
    terminal = os.environ.get("__CFBundleIdentifier", "")
    post("/brief", {"session": sid, "project": project, "terminal": terminal,
                    "text": brief}, token)


if __name__ == "__main__":
    try:
        main()
    except Exception:                    # noqa: BLE001 - never break the session
        pass
