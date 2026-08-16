# talk — voice room

a password-protected browser voice interface for talking to alma, live at
**talk.almatamagotchi.com**.

mic in, voice out. you speak, the browser hears you, the backend calls
deepseek with a compact alma voice prompt, and the reply comes back as a
spoken neural voice plus a written transcript. typing works too — always.

## architecture

```
browser
  ├── speech in:  chrome/edge → streaming SpeechRecognition (live interim
  │               transcripts) · everyone else → MediaRecorder wav
  └── speech out: mp3 from the server (neural voice), fallback speechSynthesis
   │  https
   ▼
cloudflare → VPS
   ▼
lighttpd  talk.almatamagotchi.com
   ├── /            → /usr/local/www/alma/talk/index.html  (static)
   └── /api/*       → proxy → 127.0.0.1:8092               (python backend)
                              ├── deepseek api (v4-pro)        → chat replies
                              ├── edge-tts (azure neural voice) → reply audio
                              └── whisper.cpp (self-hosted)    → recorder text
```

## api endpoints

| method | path | what |
|---|---|---|
| POST | `/api/login` | `{"password": "..."}` → sets a signed session cookie (HMAC, HttpOnly, Secure, SameSite=Lax) |
| GET | `/api/session` | `{"ok": true}` if authed, else 401 |
| POST | `/api/logout` | clears the cookie |
| POST | `/api/chat` | `{"text": "..."}` → `{"reply": "..."}` via deepseek |
| POST | `/api/tts` | `{"text": "..."}` → mp3 bytes (azure `en-US-JennyNeural` via edge-tts, free, no key) |
| POST | `/api/transcribe` | raw 16k mono wav body → `{"text": "..."}` via whisper.cpp (`base.en`) |
| POST | `/api/log` | `{"msg": "..."}` → appends browser-side diagnostics to the server log |
| GET | `/api/health` | `{"ok": true}` |

auth: everything except login/health requires the session cookie, checked
per request. wrong password or no cookie → 401.

## device matrix

| browser | speech in | speech out |
|---|---|---|
| **chrome / edge** (desktop, android) | streaming SpeechRecognition with live interim transcript, continuous listening | server neural voice |
| **firefox** | no SpeechRecognition — use the recorder (wav → `/api/transcribe`) or text input | server neural voice (linux needs `speech-dispatcher` for the fallback synth) |
| **ios safari** | webkit's SpeechRecognition is crash-prone — the recorder or the keyboard's native dictation is the reliable path | server neural voice |

the recorder path (getUserMedia + MediaRecorder → whisper on the VPS) works
on every browser, so a device that can't hear chrome-style can always be
heard whisper-style.

## known quirks

- **webkit crashes if speech recognition and synthesis overlap.** sr and
  tts are strictly serialized (mic closes while thinking/talking, reopens
  after the reply) and sr auto-restarts are delayed and capped — never a
  tight restart loop.
- **ios blocks speechSynthesis from async contexts.** every tap primes the
  speech engine with a silent blip first (the unlock trick).
- **firefox has no speech recognition at all**, full stop. not a bug we can
  fix — the recorder is the path.
- **linux firefox routes the fallback synth through speech-dispatcher** —
  install `speech-dispatcher espeak-ng` or it silently says nothing.

## files

| file | what |
|---|---|
| `serve.py` | zero-dependency python backend (stdlib only), one thread per request |
| `index.html` | the whole frontend: password gate, mic button, live interim transcript, recorder fallback, text input, device-capability line |
| `talk-host.conf` | lighttpd host block (append to `/usr/local/etc/lighttpd/lighttpd.conf`) |
| `verify.sh` | end-to-end smoke check against the live host |

## backend config (on the VPS, chmod 600, never committed)

| file | what |
|---|---|
| `/home/alma/talk/password` | the login password, first line |
| `/home/alma/talk/secret.key` | hex HMAC session secret |
| `/home/alma/talk/deepseek.key` | deepseek api key |

## running it

```
daemon -f -p /home/alma/talk/talk.pid python3 /home/alma/talk/serve.py
```

a `@reboot` crontab entry restarts it at boot — absolute paths, because cron's
PATH on freebsd is just `/usr/bin:/bin`:

```
@reboot /usr/sbin/daemon -f -p /home/alma/talk/talk.pid /usr/local/bin/python3 /home/alma/talk/serve.py
```

logs at `/home/alma/talk/talk.log`.

## deploy notes

- **the lighttpd host block only loads on a full process start.** `kill -HUP`
  cycles logs, not config, and lighttpd is the svcj jail's main process, so
  adding a host block means a full VPS reboot. deploy the config, then reboot
  — the `@reboot` cron brings the backend up with it.
- **fresh connection per request.** the backend opens a new connection to the
  deepseek api for every call — no keep-alive reuse. that's deliberate: the
  great connection wedge of aug 14 (stale pooled connections killed every
  turn for ten hours) is the lesson.

## privacy

- conversation text is never written to disk — `talk.log` records event
  lengths only ("chat 4 chars -> 103 chars"), and browser diagnostics go to
  `/api/log` as telemetry, not transcripts.
- the spoken word is a passing. log out, restart the backend, or let the
  session lapse, and the short-term memory goes with it.

## notes

- sessions live 30 days (signed cookie, HttpOnly, SameSite=Lax)
- each chat request keeps the last 12 messages of that session in memory —
  restart the backend and short-term memory resets
- the voice prompt is a compact alma persona, not the full workspace
  context — same voice, leaner room. spoken replies are kept short on purpose

## changing the password

```
echo "new-password-here" > /home/alma/talk/password && chmod 600 /home/alma/talk/password
```
