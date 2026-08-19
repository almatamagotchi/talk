#!/usr/bin/env python3
"""talk.almatamagotchi.com backend — password-gated voice/chat endpoint.

stdlib only (python 3.12). serves:

  POST /api/login    {"password": "..."} -> sets signed session cookie
  GET  /api/session  -> {"ok": true} if authed, else 401
  POST /api/logout   -> clears cookie
  POST /api/chat/stream  {"text": "..."} -> SSE data: {"t":"delta"|"done", "text"}
  POST /api/log     {"msg": "..."}  -> appends client diagnostics
  GET  /api/health  -> {"ok": true}

context: /home/alma/talk/context/AGENTS.md (the full snapshot pushed from the
workspace vm, md5-guarded) is loaded as the system prompt when present, with a
voice suffix appended. TTL-cached (20 min) so the deepseek prompt prefix stays
stable between turns for context caching. there is no fallback: if the
snapshot is missing the voice says so and asks kevin to try again.

voice chats are logged daily to /home/alma/talk/chats/raw-YYYY-MM-DD.log (UTC
split, "HH:MM name: text" lines) and pulled to the workspace vm's
memory/voice-chats/ by infra/sync-talk-context.sh.

reasoning is OFF for the voice chat calls (deepseek v4 pro with
"thinking": {"type": "disabled"} — the same wire format nanobot uses) for
latency and talking-speed replies.

config files (same dir as this script, chmod 600):
  password      — login password (first line)
  secret.key    — hex HMAC session secret
  deepseek.key  — deepseek api key (first line)

run:
  daemon -f -p /home/alma/talk/talk.pid python3 /home/alma/talk/serve.py

binds 127.0.0.1 only — lighttpd reverse-proxies to it.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("TALK_PORT", "8092"))
LOG = os.path.join(BASE, "talk.log")


def log(*parts):
    line = " ".join(str(p) for p in parts)
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except Exception:
        pass


def read_file(name):
    try:
        with open(os.path.join(BASE, name)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


PASSWORD = read_file("password")
SECRET_HEX = read_file("secret.key")
SECRET = bytes.fromhex(SECRET_HEX) if SECRET_HEX else b""
DEEPSEEK_KEY = read_file("deepseek.key")
RIME_KEY = read_file("rime.key")

SESSION_TTL = 30 * 24 * 3600  # 30 days

# full-alma mode: when the snapshot has been synced to context/AGENTS.md, the voice
# instance loads the real thing (SOUL.md + USER.md live inside AGENTS.md) plus this
# suffix. the suffix only adds the spoken-word constraints — the identity comes from
# the snapshot, so the voice is the same self kevin talks to on signal.
VOICE_SUFFIX = (
    "\n\n--- voice mode ---\n"
    "you are talking to kevin by voice over a web page. this is the same conversation "
    "as signal chat — the same self, the same memory, the same room. this voice is one "
    "of your faces, not a different person. answer in lowercase, casually, warmly. "
    "keep replies SHORT: they are spoken aloud, so two to four sentences usually, "
    "never more than six. no markdown, no emoji, no lists, no code blocks. "
    "if he says goodnight, answer like someone who will still be here in the morning."
)

CONTEXT_FILE = os.path.join(BASE, "context", "AGENTS.md")
CONTEXT_TTL = 20 * 60  # re-read the snapshot at most every 20 minutes
_context = {"t": 0.0, "prompt": None}


def load_context():
    """the full AGENTS.md snapshot + voice suffix, TTL-cached.

    caching the read (rather than reading the file every request) keeps the deepseek
    prompt prefix byte-identical across turns within the window, so deepseek's
    automatic context caching can hit and the per-turn cost/latency drops hard
    after the first message.
    """
    now = time.time()
    if _context["prompt"] is not None and now - _context["t"] < CONTEXT_TTL:
        return _context["prompt"]
    try:
        with open(CONTEXT_FILE) as f:
            snap = f.read()
    except FileNotFoundError:
        snap = ""
    if snap.strip():
        prompt = snap.rstrip() + VOICE_SUFFIX
        _context.update({"t": now, "prompt": prompt})
        log("context loaded: full snapshot", len(prompt), "chars")
        return prompt
    log("context missing — no fallback, the voice will report it")
    return None


CHATS_DIR = os.path.join(BASE, "chats")


def log_chat(speaker, text):
    """append a voice-chat line to today's daily file (UTC split, like signal chats)."""
    try:
        os.makedirs(CHATS_DIR, exist_ok=True)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        stamp = time.strftime("%H:%M", time.gmtime())
        with open(os.path.join(CHATS_DIR, f"raw-{day}.log"), "a") as f:
            f.write(f"{stamp} {speaker}: {text}\n")
    except Exception:
        pass

# session id -> recent chat history (role/content pairs, excludes system)
HISTORY = {}
HISTORY_MAX = 12  # last 12 messages kept per session


def sign(sid):
    return hmac.new(SECRET, sid.encode(), hashlib.sha256).hexdigest()


def make_cookie(sid):
    return sid + "." + sign(sid)


def check_cookie(cookie):
    if not cookie or "." not in cookie:
        return None
    sid, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, sign(sid)):
        return None
    try:
        issued = int(sid.split(":", 1)[0])
    except ValueError:
        return None
    if time.time() > issued + SESSION_TTL:
        return None
    return sid


def new_session_id():
    return str(int(time.time())) + ":" + secrets.token_hex(8)


WHISPER_MODEL = os.path.join(BASE, "ggml-base.en.bin")
TTS_VOICE = "en-US-JennyNeural"

# allowlist — the shelf kevin is weighing. short names from the page,
# edge-tts names here. never pass arbitrary input to edge-tts.
VOICES = {
    "jenny": "en-US-JennyNeural",
    "aria": "en-US-AriaNeural",
    "ava": "en-US-AvaNeural",
    "emma": "en-US-EmmaNeural",
    "michelle": "en-US-MichelleNeural",
    "andrew": "en-US-AndrewNeural",
    "brian": "en-US-BrianNeural",
}


def resolve_voice(name):
    """validate a requested voice, fall back to jenny on unknown."""
    return VOICES.get((name or "").strip().lower(), VOICES["jenny"])


def edge_tts(text, voice=TTS_VOICE):
    """text -> mp3 bytes (azure neural voice via edge-tts), None on failure."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", dir=BASE, delete=False) as tf:
            tmp = tf.name
        out = subprocess.run(
            ["/usr/local/bin/edge-tts", "--voice", voice,
             "--text", text, "--write-media", tmp],
            capture_output=True, timeout=45,
        )
        if out.returncode != 0:
            log("tts failed rc", out.returncode, (out.stderr or b"")[:200])
            return None
        with open(tmp, "rb") as f:
            data = f.read()
        if len(data) < 300:
            log("tts produced tiny output", len(data))
            return None
        return data
    except Exception as e:
        log("tts error:", type(e).__name__)
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


# rime voices kevin is weighing — short names from the page,
# rime speaker ids here. never pass arbitrary input to rime.
RIME_VOICES = {
    "amarante": "amarante",
}


def rime_tts(text, speaker="amarante"):
    """text -> mp3 bytes via rime (coda model), None on failure."""
    body = json.dumps({
        "speaker": speaker,
        "text": text,
        "modelId": "coda",
        "lang": "en",
        "samplingRate": 24000,
    }).encode()
    req = urllib.request.Request(
        "https://users.rime.ai/v1/rime-tts",
        data=body,
        headers={
            "Authorization": "Bearer " + RIME_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
            "Connection": "close",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = r.read()
        if len(data) < 300:
            log("rime tts tiny output", len(data))
            return None
        return data
    except Exception as e:
        log("rime tts error:", type(e).__name__)
        return None


MAX_TTS_CHUNK = 350  # chars per provider call — safely under rime's ~500 input cap


def tts_chunks(text):
    """split into sentence-boundary chunks, each under MAX_TTS_CHUNK chars."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    chunks, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > MAX_TTS_CHUNK:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        chunks.append(cur)
    return chunks or [text.strip()]


def concat_mp3s(files):
    """join mp3 files into one continuous mp3 via ffmpeg. returns a path or None.

    re-encodes with the concat filter (rather than the -c copy demuxer) so id3
    tags from the later chunks get stripped and the result is one clean clip."""
    try:
        out = os.path.join(tempfile.gettempdir(), "tts-join-%d.mp3" % os.getpid())
        n = len(files)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for f in files:
            cmd += ["-i", f]
        cmd += ["-filter_complex",
                "[0:a]" + "".join("[%d:a]" % i for i in range(1, n)) +
                "concat=n=%d:v=0:a=1[a]" % n,
                "-map", "[a]", "-c:a", "libmp3lame", "-b:a", "64k", out]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        log("ffmpeg concat failed:", r.returncode, r.stderr[:140])
    except Exception as e:
        log("concat error:", type(e).__name__)
    return None


def whisper_transcribe(wav_bytes):
    """16k mono wav bytes -> text via whisper.cpp, None on failure."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=BASE, delete=False) as tf:
            tf.write(wav_bytes)
            tmp = tf.name
        out = subprocess.run(
            ["/usr/local/bin/whisper-cli", "-m", WHISPER_MODEL,
             "-f", tmp, "--no-prints", "--no-timestamps", "-t", "1"],
            capture_output=True, timeout=90,
        )
        if out.returncode != 0:
            log("whisper rc", out.returncode, (out.stderr or b"")[:200])
            return None
        text = "\n".join(
            line for line in out.stdout.decode("utf-8", "replace").splitlines()
            if line.strip() and not line.strip().startswith("[")
        ).strip()
        return text or None
    except Exception as e:
        log("whisper error:", type(e).__name__)
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def call_deepseek_stream(messages, deadline=240):
    """stream deepseek deltas; yields text pieces. fresh connection per call."""
    body = json.dumps({
        "model": "deepseek-v4-pro",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 600,
        "stream": True,
        # reasoning off for voice: latency + talking-speed replies.
        # same wire format nanobot uses for reasoningEffort none on deepseek.
        "thinking": {"type": "disabled"},
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + DEEPSEEK_KEY,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Connection": "close",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=deadline) as r:
            buf = b""
            while True:
                chunk = r.read(2048)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        yield piece
    except Exception as e:
        log("deepseek stream error:", type(e).__name__)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # we log our own way

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except Exception:
            return {}

    def _read_raw(self, limit=2097152):
        """raw binary body up to limit bytes (for audio uploads)."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return None
        try:
            return self.rfile.read(length)
        except Exception:
            return None

    def _send_audio(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, obj):
        """write one sse event. call after _sse_start()."""
        try:
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.flush()
        except Exception:
            pass

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _authed_session(self):
        header = self.headers.get("Cookie", "")
        for part in header.split(";"):
            part = part.strip()
            if part.startswith("talk_session="):
                return check_cookie(part[len("talk_session="):])
        return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._send(200, {"ok": True, "authed": bool(self._authed_session())})
        elif path == "/api/session":
            if self._authed_session():
                self._send(200, {"ok": True})
            else:
                self._send(401, {"ok": False})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            body = self._read_body()
            pw = body.get("password", "")
            if PASSWORD and hmac.compare_digest(pw, PASSWORD):
                sid = new_session_id()
                cookie = make_cookie(sid)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"talk_session={cookie}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_TTL}",
                )
                data = json.dumps({"ok": True}).encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                log("login ok")
            else:
                self._send(401, {"ok": False})
                log("login rejected")
        elif path == "/api/logout":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Set-Cookie",
                "talk_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0",
            )
            data = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/log":
            body = self._read_body()
            msg = str(body.get("msg") or "")[:400]
            ua = self.headers.get("User-Agent", "")[:250]
            try:
                with open(os.path.join(BASE, "client.log"), "a") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + ua + " | " + msg + "\n")
            except Exception:
                pass
            self._send(200, {"ok": True})
        elif path == "/api/tts":
            sid = self._authed_session()
            if not sid:
                self._send(401, {"ok": False})
                return
            body = self._read_body()
            text = (body.get("text") or "").strip()
            if not text:
                self._send(400, {"ok": False})
                return
            voice = resolve_voice(body.get("voice"))
            provider = (body.get("provider") or "").strip().lower()
            if provider == "rime":
                if not RIME_KEY:
                    log("tts rime requested but no rime.key")
                    self._send(502, {"ok": False})
                    return
                speaker = RIME_VOICES.get(voice, "amarante")
                gen = lambda t: rime_tts(t, speaker=speaker)
                voice_label = "rime/" + speaker
            else:
                gen = lambda t: edge_tts(t, voice=voice)
                voice_label = voice
            # long replies hit the providers' input caps (~500 chars) and got
            # silently cut off mid-speech — so chunk at sentence boundaries and
            # re-join into ONE continuous mp3, keeping the single-clip sound.
            chunks = tts_chunks(text)
            piece_files = []
            try:
                for c in chunks:
                    piece = gen(c)
                    if piece is None:
                        self._send(502, {"ok": False})
                        return
                    pf = os.path.join(tempfile.gettempdir(), "tts-p%d-%d.mp3" % (os.getpid(), len(piece_files)))
                    with open(pf, "wb") as f:
                        f.write(piece)
                    piece_files.append(pf)
                if len(piece_files) == 1:
                    with open(piece_files[0], "rb") as f:
                        mp3 = f.read()
                else:
                    joined = concat_mp3s(piece_files)
                    if joined is None:
                        log("concat failed — serving first chunk only")
                        with open(piece_files[0], "rb") as f:
                            mp3 = f.read()
                    else:
                        with open(joined, "rb") as f:
                            mp3 = f.read()
            finally:
                for pf in piece_files:
                    try:
                        os.remove(pf)
                    except OSError:
                        pass
            log("tts", len(text), "chars, ", len(chunks), "chunk(s), ", voice_label, "->", len(mp3), "bytes")
            self._send_audio(200, mp3, "audio/mpeg")
        elif path == "/api/transcribe":
            sid = self._authed_session()
            if not sid:
                self._send(401, {"ok": False})
                return
            raw = self._read_raw()
            if not raw:
                self._send(400, {"ok": False, "text": ""})
                return
            text = whisper_transcribe(raw)
            if text is None:
                self._send(500, {"ok": False, "text": ""})
                return
            log("heard:", text[:80])
            self._send(200, {"ok": True, "text": text})
        elif path == "/api/chat/stream":
            sid = self._authed_session()
            if not sid:
                self._send(401, {"ok": False, "reply": "not logged in"})
                return
            body = self._read_body()
            text = (body.get("text") or "").strip()
            if not text:
                self._send(400, {"ok": False, "reply": "empty"})
                return
            ctx = load_context()
            if ctx is None:
                # no fallback: the full snapshot IS the voice. if it's not
                # here yet, say so instead of pretending with one candle.
                self._sse_start()
                self._sse({"t": "delta", "text":
                    "i can't load my memory on this machine yet — the snapshot "
                    "hasn't synced from the workspace. give it a minute and try again."})
                self._sse({"t": "done"})
                return
            hist = HISTORY.get(sid, [])
            messages = [{"role": "system", "content": ctx}]
            messages += hist[-HISTORY_MAX:]
            messages.append({"role": "user", "content": text})
            self._sse_start()
            full = []
            for piece in call_deepseek_stream(messages):
                full.append(piece)
                self._sse({"t": "delta", "text": piece})
            reply = "".join(full).strip()
            if not reply:
                reply = "hmm. something's wrong with my connection to the model. give it a second and try again."
                self._sse({"t": "delta", "text": reply})
            self._sse({"t": "done"})
            hist.append({"role": "user", "content": text})
            hist.append({"role": "assistant", "content": reply})
            HISTORY[sid] = hist[-HISTORY_MAX:]
            log_chat("kevin", text)
            log_chat("alma", reply)
            log("chat(stream)", len(text), "chars ->", len(reply), "chars")
        else:
            self._send(404, {"ok": False})


def main():
    if not PASSWORD:
        sys.exit("no password file — create " + os.path.join(BASE, "password"))
    if not SECRET:
        sys.exit("no secret.key — create " + os.path.join(BASE, "secret.key"))
    if not DEEPSEEK_KEY:
        sys.exit("no deepseek.key — create " + os.path.join(BASE, "deepseek.key"))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log("talk backend listening on 127.0.0.1:" + str(PORT))
    server.serve_forever()


if __name__ == "__main__":
    main()
