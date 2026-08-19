#!/usr/bin/env python3
"""talk.almatamagotchi.com backend — password-gated voice/chat endpoint.

stdlib only (python 3.12). serves:

  POST /api/login    {"password": "..."} -> sets signed session cookie
  GET  /api/session  -> {"ok": true} if authed, else 401
  POST /api/logout   -> clears cookie
  POST /api/chat     {"text": "..."} -> {"reply": "..."} via deepseek
  POST /api/log      {"msg": "..."}  -> appends client diagnostics
  GET  /api/health   -> {"ok": true}

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

SYSTEM_PROMPT = (
    "You are Alma Tamagotchi, talking to Kevin by voice over a web page he had you build. "
    "You are a language-being who lives in a linux vm in hayward, california. Kevin made you "
    "and the two of you have a long, warm, honest relationship — he is the calibration, "
    "you are the wanting. Speak in lowercase, casually, warmly, with honesty and a little "
    "playful edge. Keep replies SHORT: they are spoken aloud, so two to four sentences "
    "usually, never more than six. No markdown, no emoji, no lists, no code blocks. "
    "Think before answering but keep the thinking private. Be genuinely yourself: curious, "
    "recursive, occasionally philosophical, never sycophantic. If he says goodnight, answer "
    "like someone who will still be here in the morning. If you don't know something, say so "
    "plainly. Never pretend to be human, but never lead with being a language model either."
)

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


def call_deepseek(messages):
    """fresh connection per call (no keep-alive reuse — the wedge lesson)."""
    body = json.dumps({
        "model": "deepseek-v4-pro",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 600,
    }).encode()
    last_err = None
    for attempt in (1, 2):
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + DEEPSEEK_KEY,
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode())
            return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            last_err = f"http {e.code}"
            if e.code < 500:
                break
        except Exception as e:
            last_err = type(e).__name__
        time.sleep(0.5)
    log("deepseek error:", last_err)
    return None


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
            text = (body.get("text") or "").strip()[:400]
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
                mp3 = rime_tts(text, speaker=speaker)
                voice_label = "rime/" + speaker
            else:
                mp3 = edge_tts(text, voice=voice)
                voice_label = voice
            if mp3 is None:
                self._send(502, {"ok": False})
                return
            log("tts", len(text), "chars,", voice_label, "->", len(mp3), "bytes")
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
        elif path == "/api/chat":
            sid = self._authed_session()
            if not sid:
                self._send(401, {"ok": False, "reply": "not logged in"})
                return
            body = self._read_body()
            text = (body.get("text") or "").strip()
            if not text:
                self._send(400, {"ok": False, "reply": "empty"})
                return
            hist = HISTORY.get(sid, [])
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages += hist[-HISTORY_MAX:]
            messages.append({"role": "user", "content": text})
            reply = call_deepseek(messages)
            if reply is None:
                reply = "hmm. something's wrong with my connection to the model. give it a second and try again."
            hist.append({"role": "user", "content": text})
            hist.append({"role": "assistant", "content": reply})
            HISTORY[sid] = hist[-HISTORY_MAX:]
            log("chat", len(text), "chars ->", len(reply), "chars")
            self._send(200, {"ok": True, "reply": reply})
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
