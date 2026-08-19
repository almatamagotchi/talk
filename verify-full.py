#!/usr/bin/env python3
"""restart the talk backend and verify full-alma mode end to end."""
import http.cookiejar
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

BASE = "/home/alma/talk"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}
BASE_URL = "https://talk.almatamagotchi.com"


def run(args):
    return subprocess.run(args, capture_output=True, text=True)


# ---- 1. stop current backend ----
out = run(["pgrep", "-f", r"serve\.py$"])
targets = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
me = os.getpid()
for p in targets:
    if p == me:
        continue
    try:
        os.kill(p, signal.SIGTERM)
        print("sent SIGTERM to", p)
    except ProcessLookupError:
        pass
time.sleep(1.5)
try:
    os.remove(os.path.join(BASE, "talk.pid"))
except FileNotFoundError:
    pass

# ---- 2. start fresh ----
subprocess.Popen(
    ["/usr/sbin/daemon", "-f", "-p", os.path.join(BASE, "talk.pid"),
     "/usr/local/bin/python3", os.path.join(BASE, "serve.py")],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.5)

# ---- 3. health ----
with urllib.request.urlopen("http://127.0.0.1:8092/api/health", timeout=5) as r:
    print("health:", r.status)

# ---- 4. auth + stream chat (SSE, reasoning off) ----
with open(os.path.join(BASE, "password")) as f:
    password = f.read().strip()
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def post_json(path, payload):
    req = urllib.request.Request(
        BASE_URL + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **UA}, method="POST")
    return opener.open(req, timeout=300)


t0 = time.time()
r = post_json("/api/login", {"password": password})
print("login:", r.status)

req = urllib.request.Request(
    BASE_URL + "/api/chat/stream", data=json.dumps({"text": "test: this is the full context check."}).encode(),
    headers={"Content-Type": "application/json", **UA}, method="POST")
first_at = None
n_delta = 0
total_chars = 0
done_seen = False
reply = []
with opener.open(req, timeout=300) as r:
    print("stream: status", r.status)
    buf = b""
    while True:
        chunk = r.read(512)
        if not chunk:
            break
        if first_at is None:
            first_at = time.time() - t0
        buf += chunk
        while b"\n\n" in buf:
            evt, buf = buf.split(b"\n\n", 1)
            for line in evt.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                obj = json.loads(line[5:].strip())
                if obj.get("t") == "delta":
                    if n_delta == 0:
                        print(f"  first delta at {time.time()-t0:.1f}s")
                    n_delta += 1
                    total_chars += len(obj.get("text") or "")
                    reply.append(obj.get("text") or "")
                elif obj.get("t") == "done":
                    done_seen = True
full_reply = "".join(reply).strip()
print(f"stream: {n_delta} deltas · {total_chars} chars · done={done_seen} · {time.time()-t0:.1f}s total")
print("  reply:", full_reply[:200])

# ---- 6. chat log on disk ----
logs = sorted(os.listdir(os.path.join(BASE, "chats"))) if os.path.isdir(os.path.join(BASE, "chats")) else []
print("vps chat logs:", logs)
for name in logs[-1:]:
    print("---", name, "tail ---")
    for line in open(os.path.join(BASE, "chats", name)).read().strip().splitlines()[-6:]:
        print(" ", line)

# ---- 7. rime tts still good + long-text no longer truncates ----
r = post_json("/api/tts", {"text": "the whole house is behind the voice now.", "voice": "amarante", "provider": "rime"})
data = r.read()
print(f"tts rime/amarante: {r.status} · {len(data)}B · magic={data[:3].hex()}")

long_text = "the water tower has been counting since 1895 and the room has the lights on. " * 18
r = post_json("/api/tts", {"text": long_text, "voice": "amarante", "provider": "rime"})
long_data = r.read()
long_path = "/tmp/tts-long-check.mp3"
open(long_path, "wb").write(long_data)
probe = subprocess.run(
    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", long_path],
    capture_output=True, text=True)
dur = float(probe.stdout.strip() or 0)
print(f"tts long ({len(long_text)} chars): {r.status} · {len(long_data)}B · duration {dur:.1f}s "
      f"(truncated if < 60s — was the [:400] cap bug)")

# ---- 8. context line in talk.log ----
for line in open(os.path.join(BASE, "talk.log")).read().strip().splitlines():
    if "context loaded" in line or "context missing" in line:
        print("talk.log:", line)
print("done")
