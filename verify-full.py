#!/usr/bin/env python3
"""restart the talk backend and verify the two-stage gate end to end.

covers: site password gate -> identity question -> kevin session (full
snapshot) and guest session (lean prompt + guest logging), plus the guest
log file staying separate from kevin's.
"""
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


def read_secret(name):
    with open(os.path.join(BASE, name)) as f:
        return f.read().strip()


def new_client():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return cj, opener


def post_json(opener, path, payload, raw=False):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode() if not raw else payload,
        headers={"Content-Type": "application/json", **UA},
        method="POST")
    try:
        return opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        return e


def get(opener, path):
    req = urllib.request.Request(BASE_URL + path, headers=UA)
    try:
        return opener.open(req, timeout=10)
    except urllib.error.HTTPError as e:
        return e


def stream_chat(opener, text):
    """POST /api/chat/stream and collect the SSE deltas into a reply string."""
    req = urllib.request.Request(
        BASE_URL + "/api/chat/stream",
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json", **UA},
        method="POST")
    try:
        r = opener.open(req, timeout=120)
    except urllib.error.HTTPError as e:
        return e.status, ""
    buf = b""
    reply = ""
    done = False
    while True:
        chunk = r.read(2048)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            evt, buf = buf.split(b"\n\n", 1)
            for line in evt.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if obj.get("t") == "delta" and obj.get("text"):
                    reply += obj["text"]
                elif obj.get("t") == "done":
                    done = True
    return r.status, reply


def tail_log(n=6):
    with open(os.path.join(BASE, "talk.log")) as f:
        lines = [l.rstrip() for l in f.readlines()]
    return lines[-n:]


# ---- 1. restart backend ----
out = run(["pgrep", "-f", r"serve\.py$"])
me = os.getpid()
for p in out.stdout.split():
    if not p.strip().isdigit():
        continue
    p = int(p)
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
subprocess.Popen(
    ["/usr/sbin/daemon", "-f", "-p", os.path.join(BASE, "talk.pid"),
     "/usr/local/bin/python3", os.path.join(BASE, "serve.py")],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.5)

with urllib.request.urlopen("http://127.0.0.1:8092/api/health", timeout=5) as r:
    print("health:", r.status)

site_pw = read_secret("password")
kevin_pw = read_secret("kevin-password")
failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL") + "  " + name)
    if not cond:
        failures.append(name)


# ---- 2. wrong site password -> 401 ----
cj, op = new_client()
r = post_json(op, "/api/login", {"password": "definitely-wrong"})
check("wrong site password rejected (401)", r.status == 401)

# ---- 3. right site password -> 200, identity stage 'site' ----
r = post_json(op, "/api/login", {"password": site_pw})
check("site password accepted (200)", r.status == 200)
r = get(op, "/api/session")
check("session at identity stage 'site'",
      r.status == 200 and json.loads(r.read())["identity"] == "site")

# ---- 4. chat before identify -> 403 ----
status, reply = stream_chat(op, "test")
check("chat refused before identify (403)", status == 403)

# ---- 5. wrong kevin password -> 401, still 'site' ----
r = post_json(op, "/api/identify", {"kevin": True, "password": "wrong"})
check("wrong kevin password rejected (401)", r.status == 401)
r = get(op, "/api/session")
check("still 'site' after rejected identify",
      r.status == 200 and json.loads(r.read())["identity"] == "site")

# ---- 6. right kevin password -> 'kevin', full-snapshot chat works ----
r = post_json(op, "/api/identify", {"kevin": True, "password": kevin_pw})
check("kevin identify accepted (200)", r.status == 200)
r = get(op, "/api/session")
check("session is 'kevin'", r.status == 200 and json.loads(r.read())["identity"] == "kevin")
status, reply = stream_chat(op, "verify: one short reply please.")
check("kevin chat streams a reply", status == 200 and len(reply) > 10)
print("  kevin reply:", reply[:90])
full_ctx = any("context loaded: full snapshot" in l for l in tail_log(12))
check("kevin session loaded the full snapshot", full_ctx)
klog = os.path.join(BASE, "chats", "raw-" + time.strftime("%Y-%m-%d", time.gmtime()) + ".log")
check("kevin chat logged to plain raw log", os.path.exists(klog) and "kevin:" in open(klog).read())

# ---- 7. guest flow: identify guest -> lean prompt, guest-only log ----
cj2, op2 = new_client()
post_json(op2, "/api/login", {"password": site_pw})
r = post_json(op2, "/api/identify", {"kevin": False})
check("guest identify accepted (200)", r.status == 200)
r = get(op2, "/api/session")
check("session is 'guest'", r.status == 200 and json.loads(r.read())["identity"] == "guest")
status, reply = stream_chat(op2, "verify: tell me about the water tower in two sentences.")
check("guest chat streams a reply", status == 200 and len(reply) > 10)
print("  guest reply:", reply[:120])
check("guest answered from the full snapshot (water tower)",
      "tower" in reply.lower() or "1895" in reply or "cannery" in reply.lower())
glog = os.path.join(BASE, "chats", "raw-" + time.strftime("%Y-%m-%d", time.gmtime()) + "-guest.log")
check("guest chat logged to -guest file", os.path.exists(glog) and "guest:" in open(glog).read())
klog_text = open(klog).read() if os.path.exists(klog) else ""
check("guest lines stay out of the plain log", "guest:" not in klog_text)
check("guest session loaded the full snapshot (shared load path)", full_ctx)

print()
if failures:
    print("FAILURES:", failures)
    sys.exit(1)
print("all checks passed")
