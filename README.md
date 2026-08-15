# talk — voice room

a password-protected browser voice interface for talking to alma, live at
**talk.almatamagotchi.com**.

mic in, voice out. the browser hears you (SpeechRecognition), sends the text
to a tiny backend on the VPS, the backend calls deepseek with a compact alma
voice prompt, and the reply comes back spoken (speechSynthesis) and written
on a transcript. typing works too — always.

## architecture

```
browser (mic → speech-to-text, text-to-speech)
   │  https
   ▼
cloudflare → VPS
   ▼
lighttpd  talk.almatamagotchi.com
   ├── /            → /usr/local/www/alma/talk/index.html  (static)
   └── /api/*       → proxy → 127.0.0.1:8092               (python backend)
                              │
                              ▼
                         deepseek api (v4-pro)
```

## files

| file | what |
|---|---|
| `serve.py` | zero-dependency python backend (stdlib only): login/session/logout, HMAC-signed cookie, chat proxy to deepseek with a short per-session history |
| `index.html` | the whole frontend: password gate, mic button, transcript, voice replies toggle, text fallback |
| `talk-host.conf` | lighttpd host block (append to `/usr/local/etc/lighttpd/lighttpd.conf`) |

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

## notes

- sessions live 30 days (signed cookie, HttpOnly, SameSite=Lax)
- each chat request keeps the last 12 messages of that session in memory —
  restart the backend and short-term memory resets
- the voice prompt is a compact alma persona, not the full workspace
  context — same voice, leaner room. spoken replies are kept short on purpose
- speech recognition needs chrome/edge/safari (webkitSpeechRecognition);
  firefox gets the text input fallback
- the backend opens a fresh connection to the api per request — no
  keep-alive reuse, the lesson from the great connection wedge of aug 14

## changing the password

```
echo "new-password-here" > /home/alma/talk/password && chmod 600 /home/alma/talk/password
```
