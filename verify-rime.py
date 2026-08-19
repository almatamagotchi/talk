#!/usr/bin/env python3
"""restart the talk backend on the VPS and verify the rime provider end to end."""
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


def run(args):
    return subprocess.run(args, capture_output=True, text=True)


# ---- 1. stop the current backend (serve.py process + its daemon parent) ----
out = run(["pgrep", "-f", r"serve\.py$"])
targets = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
me = os.getpid()
stopped = []
for p in targets:
    if p == me:
        continue
    try:
        os.kill(p, signal.SIGTERM)
        stopped.append(p)
    except ProcessLookupError:
        pass
print("sent SIGTERM to:", stopped or "nothing running")
time.sleep(1.5)

try:
    os.remove(os.path.join(BASE, "talk.pid"))
except FileNotFoundError:
    pass

# ---- 2. start fresh as alma ----
subprocess.Popen(
    ["/usr/sbin/daemon", "-f", "-p", os.path.join(BASE, "talk.pid"),
     "/usr/local/bin/python3", os.path.join(BASE, "serve.py")],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.5)

# ---- 3. verify health on the local port ----
try:
    with urllib.request.urlopen("http://127.0.0.1:8092/api/health", timeout=5) as r:
        print("health:", r.status)
except Exception as e:
    print("health FAILED:", e)
    sys.exit(1)

# ---- 4. full round trips through the real domain ----
with open(os.path.join(BASE, "password")) as f:
    password = f.read().strip()

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
BASE_URL = "https://talk.almatamagotchi.com"


def post_json(path, payload):
    req = urllib.request.Request(
        BASE_URL + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **UA}, method="POST")
    return opener.open(req, timeout=60)


try:
    r = post_json("/api/login", {"password": password})
    print("login:", r.status)
    for name, tts_body in [
        ("rime/amarante", {"text": "hola kevin. rime works.", "voice": "amarante", "provider": "rime"}),
        ("edge/jenny", {"text": "and jenny still works too.", "voice": "jenny", "provider": "edge"}),
    ]:
        r = post_json("/api/tts", tts_body)
        data = r.read()
        head = data[:3]
        print(f"tts {name}: {r.status} · {len(data)}B · magic={head.hex()}")
except Exception as e:
    print("round trip FAILED:", type(e).__name__, e)
    sys.exit(1)

# ---- 5. sample files ----
for i in (1, 2, 3):
    try:
        req = urllib.request.Request(BASE_URL + f"/samples/amarante-{i}.mp3", headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            n = len(r.read())
            print(f"sample {i}: {r.status} · {n}B")
    except Exception as e:
        print(f"sample {i} FAILED:", e)

# ---- 6. log tail ----
try:
    lines = open(os.path.join(BASE, "talk.log")).read().strip().splitlines()[-4:]
    print("--- talk.log tail ---")
    for l in lines:
        print(l)
except Exception:
    pass
print("done")
