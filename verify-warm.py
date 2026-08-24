#!/usr/bin/env python3
# end-to-end warm/chat timing check, run on the talk VPS against 127.0.0.1:8092
import http.client, json, time, urllib.request

B = "127.0.0.1:8092"
jar = {}  # name -> value

def req(method, path, body=None):
    conn = http.client.HTTPConnection(B, timeout=180)
    headers = {"Content-Type": "application/json"}
    if jar:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    sc = resp.headers.get("Set-Cookie")
    if sc:
        name, rest = sc.split("=", 1)
        jar[name] = rest.split(";", 1)[0]
    out = resp.read()
    conn.close()
    return resp.status, out

pw = open("/home/alma/talk/password").read().strip()
kpw = open("/home/alma/talk/kevin-password").read().strip()

s, o = req("POST", "/api/login", {"password": pw})
print("login:", s, o[:60])
s, o = req("POST", "/api/identify", {"kevin": True, "password": kpw})
print("identify:", s, o[:60])

t0 = time.time()
s, o = req("POST", "/api/warm")
t1 = time.time()
print(f"warm: {s} {o[:60]} {t1-t0:.2f}s")

# read the streamed chat response fully and time it
conn = http.client.HTTPConnection(B, timeout=180)
conn.request("POST", "/api/chat/stream",
             body=json.dumps({"text": "speed test, one short reply"}).encode(),
             headers={"Content-Type": "application/json",
                      "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())})
resp = conn.getresponse()
t0 = time.time()
chunks = 0
first_delta = None
while True:
    line = resp.readline()
    if not line:
        break
    chunks += 1
    if first_delta is None:
        first_delta = time.time() - t0
t1 = time.time()
print(f"chat after warm: {resp.status}, first delta at {first_delta:.2f}s, "
      f"total {t1-t0:.2f}s, {chunks} lines")

print("--- talk.log tail ---")
print("\n".join(open("/home/alma/talk/talk.log").read().splitlines()[-4:]))
