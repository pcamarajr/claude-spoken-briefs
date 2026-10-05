#!/usr/bin/env python3
"""Local Kokoro TTS server for Claude Code spoken briefs.

Listens on 127.0.0.1 only. The Stop hook (claude/brief-hook.py) posts each
brief here; the server voices it with Kokoro-82M, stores the audio, and plays
it when you click the status line link.

    POST /brief        {"session", "state": "writing"|"failed"}  mark progress
                       {"session", "project", "terminal", "text"} synthesize it
    POST /play/<sid>   play a stored brief now (again: stop it)
    POST /stop         stop playback and clear the queue
    GET  /listen/<sid>/<key>
                       the status line's "Listen" link: plays (or stops) the
                       brief, closes its own tab and refocuses the terminal
    GET  /health       {"ok": true, "loaded": bool}

POSTs need the bearer token in ~/.config/claude-spoken-briefs/token, created
on first start. The /listen link is plain http because Claude Code drops
custom-scheme links from the status line; each brief has a random key, so a
web page cannot trigger playback. CORS preflights are refused.

Briefs live in ~/Library/Caches/claude-spoken-briefs as <sid>.json + <sid>.wav;
the status line reads the JSON directly. Playback is native (afplay).

The model (~1.5 GB resident) is imported lazily and the process exits after
IDLE_TIMEOUT seconds without requests; launchd restarts it cheaply, without
the model, and it loads again on the next brief.
"""
import io
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HOST = "127.0.0.1"
PORT = int(os.environ.get("CLAUDE_BRIEFS_PORT", 51731))
IDLE_TIMEOUT = float(os.environ.get("CLAUDE_BRIEFS_IDLE", 1800))
SR = 24000

# Kokoro pads every chunk with silence; trim it and put back a natural pause.
PAUSE = 0.30
SILENCE_THRESHOLD = 0.006
KEEP_MARGIN = 0.08

CONFIG_DIR = os.path.expanduser("~/.config/claude-spoken-briefs")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
BRIEF_DIR = os.path.expanduser("~/Library/Caches/claude-spoken-briefs")
BRIEF_TTL = 7 * 86400
MAX_BRIEF = 1500
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LISTEN_RE = re.compile(r"^/listen/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9_-]{8,64})$")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9.-]{1,128}$")

# Served by /listen. The tab was opened from the terminal, so it has a single
# history entry and may close itself.
LISTEN_PAGE = b"""<!doctype html><meta charset="utf-8"><title>Claude brief</title>
<body style="font:16px system-ui;padding:2em">Playing the brief. You can close this tab.
<script>window.close()</script>"""

# Override any key in ~/.config/claude-spoken-briefs/config.json.
#   voices:   Kokoro voice per language of the brief
#   autoplay: "never" | "away" (you are not in a terminal) | "always"
#   notify:   macOS notification when a brief is ready while you are away
DEFAULT_CONFIG = {
    "voices": {"en": "af_heart", "pt": "pf_dora", "es": "ef_dora",
               "fr": "ff_siwis", "it": "if_sara"},
    "speed": 1.0,
    "autoplay": "never",
    "notify": True,
    "terminals": ["Ghostty", "iTerm2", "Terminal", "WezTerm", "kitty",
                  "Alacritty", "Warp"],
}

# Common words that tell the brief's language apart.
LANG_WORDS = {
    "en": {"the", "and", "is", "to", "you", "it", "that", "was", "with", "for",
           "this", "now", "needs", "i"},
    "pt": {"não", "você", "com", "uma", "foi", "isso", "mas", "também",
           "precisa", "agora", "eu", "os", "dos", "das", "ou"},
    "es": {"el", "los", "las", "y", "pero", "también", "ahora", "del", "es",
           "hay", "yo", "esto", "necesitas", "usted"},
    "fr": {"le", "la", "les", "et", "est", "vous", "pour", "avec", "une",
           "pas", "des", "je", "ce", "maintenant"},
    "it": {"il", "gli", "è", "e", "per", "non", "sono", "ho", "ora", "anche",
           "questo", "devi", "della"},
}

_model_lock = threading.Lock()
_brief_lock = threading.Lock()
_pipelines = {}
_last_used = time.time()


def log(msg):
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def get_pipeline(lang):
    """KPipeline for a language code ('a' US English, 'p' pt-BR, 'e' es...)."""
    if lang not in _pipelines:
        t0 = time.time()
        from kokoro import KPipeline          # heavy: pulls in torch
        # Every pipeline shares one model instance.
        model = next(iter(_pipelines.values())).model if _pipelines else True
        _pipelines[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M",
                                     model=model)
        log(f"pipeline '{lang}' loaded in {time.time() - t0:.1f}s")
    return _pipelines[lang]


def synthesize(text, voice, speed):
    """Float32 audio for one sentence, trimmed, with a pause after it."""
    pipeline = get_pipeline(voice[0])
    pieces = [r.audio.numpy().astype(np.float32).reshape(-1)
              for r in pipeline(text, voice=voice, speed=speed, split_pattern=None)
              if r.audio is not None]
    if not pieces:
        return np.zeros(0, np.float32)
    audio = np.concatenate(pieces)
    loud = np.nonzero(np.abs(audio) > SILENCE_THRESHOLD)[0]
    if len(loud):
        margin = int(KEEP_MARGIN * SR)
        audio = audio[max(0, loud[0] - margin):loud[-1] + margin]
    pause = np.zeros(int(PAUSE / max(speed, 0.5) * SR), np.float32)
    return np.concatenate([audio, pause])


def to_wav(audio):
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def load_token():
    """The shared secret for brief requests, created on first start."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
        cfg.update(user)
        cfg["voices"] = {**DEFAULT_CONFIG["voices"], **user.get("voices", {})}
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


def brief_path(sid, ext):
    return os.path.join(BRIEF_DIR, f"{sid}.{ext}")


def read_brief(sid):
    try:
        with open(brief_path(sid, "json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_brief(sid, **fields):
    """Merge fields into <sid>.json atomically (the status line reads it)."""
    with _brief_lock:
        os.makedirs(BRIEF_DIR, exist_ok=True)
        data = read_brief(sid) or {}
        data.update(fields, updated=time.time())
        tmp = brief_path(sid, "json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, brief_path(sid, "json"))


def tidy_briefs():
    """Drop briefs older than BRIEF_TTL; nothing is playing after a restart."""
    if not os.path.isdir(BRIEF_DIR):
        return
    now = time.time()
    for name in os.listdir(BRIEF_DIR):
        path = os.path.join(BRIEF_DIR, name)
        if now - os.path.getmtime(path) > BRIEF_TTL:
            os.remove(path)
        elif name.endswith(".json"):
            sid = name[:-5]
            b = read_brief(sid)
            if b and b.get("state") in ("playing", "queued"):
                write_brief(sid, state="played")


def frontmost_app():
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True,
                               text=True, timeout=2).stdout.strip()
        out = subprocess.run(["lsappinfo", "info", "-only", "name", front],
                             capture_output=True, text=True, timeout=2).stdout
        return out.split("=", 1)[-1].strip().strip('"')
    except (OSError, subprocess.SubprocessError):
        return ""


def notify(title, text):
    script = ("on run argv\n"
              "display notification (item 2 of argv) with title (item 1 of argv)\n"
              "end run")
    subprocess.Popen(["osascript", "-e", script, title, text],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Player:
    """Plays stored briefs one at a time with afplay.

    Autoplayed briefs queue behind each other; a click (play_now) interrupts
    whatever is playing, because that is the one you asked for.
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._proc = None
        self._current = None
        threading.Thread(target=self._run, daemon=True).start()

    def enqueue(self, sid):
        write_brief(sid, state="queued")
        self._queue.put(sid)

    def play_now(self, sid):
        with self._lock:
            toggling = self._current == sid
        self.stop()
        if not toggling:
            self.enqueue(sid)

    def stop(self):
        while True:
            try:
                sid = self._queue.get_nowait()
            except queue.Empty:
                break
            write_brief(sid, state="played")
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    def _run(self):
        global _last_used
        while True:
            sid = self._queue.get()
            wav = brief_path(sid, "wav")
            if not os.path.exists(wav):
                continue
            write_brief(sid, state="playing")
            with self._lock:
                self._current = sid
                self._proc = subprocess.Popen(["afplay", wav])
            self._proc.wait()
            with self._lock:
                self._current = self._proc = None
            write_brief(sid, state="played", played=time.time())
            _last_used = time.time()


def detect_lang(text):
    """The language of the brief with the most common words in it."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    scores = {lang: sum(w in vocab for w in words)
              for lang, vocab in LANG_WORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "en"


def make_brief(sid, project, text, player, terminal=""):
    """Synthesize a brief to <sid>.wav, then notify and/or autoplay."""
    global _last_used
    cfg = load_config()
    lang = detect_lang(text)
    voice = cfg["voices"].get(lang) or DEFAULT_CONFIG["voices"]["en"]
    try:
        speed = min(2.0, max(0.5, float(cfg.get("speed", 1.0))))
    except (TypeError, ValueError):
        speed = 1.0
    t0 = time.time()
    try:
        # Sentence by sentence, so each gets the same trimmed, natural pause.
        parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
        with _model_lock:                # the model is not thread-safe
            audio = np.concatenate([synthesize(p, voice, speed) for p in parts])
        with open(brief_path(sid, "wav"), "wb") as f:
            f.write(to_wav(audio))
    except Exception as exc:             # noqa: BLE001 - keep the server up
        log(f"brief synthesis failed: {exc!r}")
        write_brief(sid, state="failed")
        return
    _last_used = time.time()
    duration = len(audio) / SR
    write_brief(sid, state="ready", project=project, text=text, lang=lang,
                duration=round(duration, 1), created=time.time(),
                key=secrets.token_urlsafe(12), terminal=terminal)
    log(f"brief {sid[:8]} ({project}, {lang}): {duration:.1f}s audio "
        f"in {time.time() - t0:.1f}s")

    away = frontmost_app() not in cfg.get("terminals", [])
    if cfg.get("autoplay") == "always" or (cfg.get("autoplay") == "away" and away):
        player.enqueue(sid)
    elif away and cfg.get("notify", True):
        notify(f"Claude · {project}", text)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeSpokenBriefs/1"

    def log_message(self, fmt, *args):   # quiet default access log
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        got = self.headers.get("Authorization", "")
        return secrets.compare_digest(got, f"Bearer {self.server.token}")

    def do_OPTIONS(self):                # refuse every CORS preflight
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "loaded": bool(_pipelines)})
        m = LISTEN_RE.match(self.path)
        if m:
            return self._listen(*m.groups())
        self._send(404, {"error": "not found"})

    def _listen(self, sid, key):
        brief = read_brief(sid) or {}
        if not brief.get("key") or not secrets.compare_digest(key, brief["key"]):
            return self._send(403, {"error": "forbidden"})
        # The click brought the browser forward; hand focus back right away
        # (and once more in case the browser was still activating).
        terminal = brief.get("terminal", "")
        if BUNDLE_RE.match(terminal):
            subprocess.Popen(["open", "-b", terminal])
            threading.Timer(0.3, subprocess.run,
                            [["open", "-b", terminal]]).start()
        self.server.player.play_now(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(LISTEN_PAGE)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(LISTEN_PAGE)

    def do_POST(self):
        global _last_used
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        _last_used = time.time()
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 64 * 1024)
            req = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self._send(400, {"error": "bad json"})

        player = self.server.player
        if self.path == "/stop":
            player.stop()
            return self._send(200, {"ok": True})
        if self.path.startswith("/play/"):
            sid = self.path[len("/play/"):]
            if not SESSION_RE.match(sid):
                return self._send(400, {"error": "bad session"})
            if not os.path.exists(brief_path(sid, "wav")):
                return self._send(404, {"error": "no brief"})
            player.play_now(sid)
            return self._send(200, {"ok": True})
        if self.path != "/brief":
            return self._send(404, {"error": "not found"})

        sid = str(req.get("session", ""))
        if not SESSION_RE.match(sid):
            return self._send(400, {"error": "bad session"})
        state = req.get("state")
        if state in ("writing", "failed"):
            write_brief(sid, state=state)
            return self._send(200, {"ok": True})
        text = str(req.get("text", "")).strip()[:MAX_BRIEF]
        project = str(req.get("project", "")).strip()[:64] or "Claude"
        if not text:
            return self._send(400, {"error": "empty text"})
        write_brief(sid, state="writing")
        # Reply at once; the hook does not need to wait for the audio.
        terminal = str(req.get("terminal", ""))
        threading.Thread(target=make_brief,
                         args=(sid, project, text, player, terminal),
                         daemon=True).start()
        return self._send(200, {"ok": True})


def reaper():
    while True:
        time.sleep(30)
        if _pipelines and time.time() - _last_used > IDLE_TIMEOUT:
            log("idle, exiting to free memory")
            os._exit(0)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.token = load_token()
    srv.player = Player()
    tidy_briefs()
    threading.Thread(target=reaper, daemon=True).start()
    log(f"listening on http://{HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
