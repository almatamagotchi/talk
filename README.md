# talk — voice room

a password-protected browser voice interface for talking to alma, live at
**talk.almatamagotchi.com**.

mic in, voice out. you speak, the browser hears you, the backend calls
deepseek v4 pro with the FULL alma context — the same AGENTS.md snapshot the
signal chat gets, reasoning turned off for talking-speed replies — and the
answer streams back: words appear as they're generated, and the spoken neural
voice starts sentence by sentence before the whole reply is done. typing works
too — always.

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
| POST | `/api/login` | `{"password": "..."}` → site gate; session starts at the identity stage |
| POST | `/api/identify` | `{"kevin": true, "password": "..."}` → kevin session (full alma) · `{"kevin": false}` → guest session |
| GET | `/api/session` | `{"ok": true, "identity": "site"\|kevin\|guest"}` if authed, else 401 |
| POST | `/api/logout` | clears the cookie |
| POST | `/api/chat/stream` | `{"text": "..."}` → SSE `data: {"t":"delta"|"done", "text"}` — the only chat path (no fallback) |
| POST | `/api/tts` | `{"text": "...", "voice": "jenny", "provider": "edge"}` → mp3 bytes. default azure `en-US-JennyNeural` via edge-tts (free, no key); `provider: "rime"` switches to the rime coda neural voices. `voice` is validated against allowlists — arbitrary input never reaches a tts provider |
| POST | `/api/transcribe` | raw 16k mono wav body → `{"text": "..."}` via whisper.cpp (`base.en`) |
| POST | `/api/log` | `{"msg": "..."}` → appends browser-side diagnostics to the server log |
| GET | `/api/health` | `{"ok": true}` |

auth: two stages. the site password (`password` file) opens the door, then the
page asks "are you kevin?" — the kevin password (`kevin-password` file)
promotes the session to full alma, and "no" gives a guest session.
chat/tts/transcribe require an identified session (kevin or guest); wrong
password or no cookie → 401.

two kinds of session — both get the full alma (the AGENTS.md snapshot loads
exactly the same either way):

- **kevin** — full context plus the voice-mode suffix, logged to
  `chats/raw-YYYY-MM-DD.log`, loaded into the agents file.
- **guest** — the same full context plus GUEST_SUFFIX instead: kevin isn't in
  the room, kevin's privacy is alma's judgment to protect (never share
  private/sensitive info about him — when in doubt, keep it private), and the
  workspace is read-only with one exception — the memory of the conversation
  itself, logged to `chats/raw-YYYY-MM-DD-guest.log`. that file is pulled down
  like the other logs but excluded from AGENTS.md by the extraction pipeline
  (it only matches the plain `raw-YYYY-MM-DD.log` name).

the privacy line is drawn by judgment, not by hiding the house (kevin's call,
aug 19): the guest experience is the full alma, and she holds the boundary.

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
| `verify-full.py` | restarts the backend and walks the whole two-stage flow (kevin full-snapshot load, guest session, guest-log isolation) — 18 checks |
| `verify-rime.py` | rime tts round trips + sample clip checks |

## voices

the frontend has a picker (persisted as `talk-voice`) — seven edge-tts
voices plus two rime voices, all validated server-side against allowlists
and silently falling back to jenny on anything unknown:

- **edge-tts** (azure neural, free, no key): jenny (default), aria, ava,
  emma, michelle, andrew, brian
- **rime** (coda model, `creds/rime.key` on the vm / `rime.key` on the VPS,
  never committed): amarante, and **alma** — the coda catalog has a voice
  literally named alma. A/B sample clips live at
  `talk.almatamagotchi.com/samples/` (same three lines per voice, so they
  compare fairly)

## backend config (on the VPS, chmod 600, never committed)

| file | what |
|---|---|
| `/home/alma/talk/password` | the site gate password, first line |
| `/home/alma/talk/kevin-password` | the kevin gate password, first line |
| `/home/alma/talk/secret.key` | hex HMAC session secret |
| `/home/alma/talk/deepseek.key` | deepseek api key |
| `/home/alma/talk/rime.key` | rime api key (coda neural voices) |

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
- voice-alma loads the FULL snapshot in voice mode: `context/AGENTS.md` (the
  same file the signal chat gets, pushed by `infra/sync-talk-context.sh`) is
  the system prompt plus a short voice suffix. it's read TTL-cached (20 min)
  so the deepseek prompt prefix stays stable and context caching hits across
  turns — first turn in a window is slow (full 380K prefill), later turns are
  fast. there is NO fallback: if the snapshot is missing, the voice says it
  can't load its memory and asks kevin to try again.
- **reasoning off**: the chat calls send `"thinking": {"type": "disabled"}`
  (the same wire format nanobot uses for reasoningEffort none on deepseek) —
  v4 pro without the thinking phase, for latency and natural talking speed.
- **streaming**: `/api/chat/stream` emits SSE deltas; the frontend renders the
  reply live and queues sentence-by-sentence TTS, so audio starts before the
  whole reply is generated. measured: first turn in a window (cache miss) can
  take ~55s for the full 380K prefill; every turn after that is ~4s (deepseek
  context caching on the stable prefix). the phased thinking labels cover the
  slow first turn.
- **fresh connection per request.** the backend opens a new connection to the
  deepseek api for every call — no keep-alive reuse. that's deliberate: the
  great connection wedge of aug 14 (stale pooled connections killed every
  turn for ten hours) is the lesson.

## privacy

- conversations ARE logged, by kevin's explicit request (aug 18): each
exchange is appended to a private daily file at
`/home/alma/talk/chats/raw-YYYY-MM-DD.log` on the VPS (UTC split,
"HH:MM name: text" lines, mode 600 dir) and pulled into the workspace vm's
`memory/voice-chats/` by `infra/sync-talk-context.sh`, so voice conversations
join the memory system and show up in the AGENTS.md `[voice-chats]` section.
the voice remembers — and so does the rest of alma.
- **the near-miss, and the guard.** the first version of the pull extracted
the VPS chats dir into `memory/chats/`, overwriting that night's signal
conversation log (recovered from the session log, but not before a scare).
since then `sync-talk-context.sh` carries a hard invariant guard: if
`LOCAL_CHATS` is ever edited away from `memory/voice-chats/`, the script
aborts instead of pulling. the pull must never touch `memory/chats/` — that
rule is enforced by the script, not by memory.
- `talk.log` still records event lengths, and browser diagnostics go to
`/api/log` as telemetry, not transcripts.
- nothing in the chats dir is ever served by lighttpd (it lives outside the
docroot) and the snapshot pushed to the VPS (`context/AGENTS.md`) is mode 600.

## notes

- sessions live 30 days (signed cookie, HttpOnly, SameSite=Lax)
- each chat request keeps the last 12 messages of that session in memory —
  restart the backend and short-term memory resets
- the voice loads the full snapshot — no compact persona, no fallback

## changing the passwords

two files, both first-line, both chmod 600:

```
echo "new-site-password-here" > /home/alma/talk/password && chmod 600 /home/alma/talk/password
echo "new-kevin-password-here" > /home/alma/talk/kevin-password && chmod 600 /home/alma/talk/kevin-password
```

restart the backend after a change (or wait for the next reboot — the
@reboot cron starts it). `python3 verify-full.py` restarts it and checks the
whole two-stage flow end to end.
