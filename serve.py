#!/usr/bin/env python3
"""talk.almatamagotchi.com backend — password-gated voice/chat endpoint.

stdlib only (python 3.12). serves:

  POST /api/login    {"password": "..."} -> sets signed session cookie
  GET  /api/session  -> {"ok": true} if authed, else 401
  POST /api/logout   -> clears cookie
  POST /api/chat     {"text": "..."} -> {"reply": "..."} via deepseek
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
import sys
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
