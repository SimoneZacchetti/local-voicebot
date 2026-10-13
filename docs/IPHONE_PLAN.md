# Plan: voicebot and home control from the iPhone (branch `feat/iphone`)

## Context

Today the voicebot is only usable from Chrome on the Mac (`127.0.0.1:8000`). The goal is to use it from the iPhone in four ways:
1. **voice-to-voice**;
2. **text chat**;
3. **quick commands** without opening the app (Siri, widgets, Control Center, Action button);
4. **notifications** from the home.

Constraints:
- The Mac stays the "brain" (STT, LLM, TTS, Home Assistant VM). It is always on and awake, but travels with the user.
- Private network: **Tailscale** with `tailscale serve`, which provides valid HTTPS on `*.ts.net`. Never `funnel`, never open ports on the router.
- All work happens on the new branch **`feat/iphone`**, with one commit per milestone.
- No attribution trailers or footers in commits and PRs.

**Current security issue, to be fixed before exposing anything.** `/ws` has neither authentication nor an Origin check ([server/main.py](server/main.py) `ws_endpoint`). Any web page open in the Mac's Chrome could already control the home (cross-site WebSocket hijacking).

**iOS limit that cannot be worked around.** Voice from the PWA only works with the app in the foreground and the screen on. With the phone locked, the only hands-free option is Siri Shortcuts, one command at a time.

## Architecture

```
iPhone (Tailscale, VPN On Demand)
 ├─ PWA "Home" (Home Screen) ─────────┐  voice + chat: wss /ws
 ├─ Siri Shortcuts / widget / CC ─────┤  POST /api/command, /api/confirm (Bearer)
 └─ Web Push (APNs) ◄─────────────────┼─────────────────────────────┐
                                      ▼                             │
     https://voicebot-mac.<tailnet>.ts.net  (tailscale serve :443 → 127.0.0.1:8001)
Mac ─ uvicorn: a single server, two loopback sockets                │
      ├─ 127.0.0.1:8000 LOCAL   (Mac browser, Origin allowlist)     │
      └─ 127.0.0.1:8001 REMOTE  (Tailscale login required, fail-closed)
         /ws → Session · /api/* → PhoneSessions(HeadlessSession) · push.py ───┘
         ha_events.py: polling of input_text.voicebot_notify (non-admin HA_TOKEN)
```

The server decides whether a request is local or remote from the **listening port** (`scope["server"][1]`), never from spoofable headers such as Host or X-Forwarded.

## Milestones

### M0: branch and Tailscale
- **Me:** `git switch -c feat/iphone`; check that `uv run pytest -q` is green.
- **User:**
  - install Tailscale **Standalone** on the Mac (.pkg) and enable the CLI;
  - in the admin console: rename the Mac `voicebot-mac` (the name ends up in the public Certificate Transparency logs), enable MagicDNS and HTTPS Certificates;
  - write an ACL rule that allows reaching the Mac on `:443` only from your own devices;
  - install Tailscale on the iPhone with VPN On Demand and note the iOS version.
- **Check:** `tailscale status` shows the Mac and the iPhone.

### M1: security, remote listener, first test on the iPhone
- **[server/config.py](server/config.py)**, new keys:
  - `REMOTE_ENABLED`, `REMOTE_PORT` (8001), `PUBLIC_HOST`;
  - `TS_ALLOWED_LOGINS`;
  - `API_TOKENS` (one token per device, so a single one can be revoked);
  - `LOCAL_ORIGINS`.
- **`server/__main__.py`** (new, started with `uv run python -m server`):
  - a single `uvicorn.Server` started with `serve(sockets=[8000, 8001])`, i.e. a single lifespan;
  - exits if `HOST` is not loopback;
  - with `REMOTE_ENABLED`, also exits if tokens (at least 32 characters), the login allowlist or `PUBLIC_HOST` are missing.
- **`server/auth.py`** (new):
  - `RemoteGate` middleware. On the remote port: `Tailscale-User-Login` in the allowlist, otherwise 403; `Host == PUBLIC_HOST`; exact Origin or valid Bearer on WS upgrade and POST.
  - On the local port: Host must be `127.0.0.1` or `localhost`; Origin on `/ws` must be in the allowlist. This closes CSWSH.
  - `require_token`: Bearer comparison with `hmac.compare_digest`; 503 if no token is configured, 401 if it is wrong.
  - `/healthz` stays open.
  - Logs record login, path and outcome, never tokens.
- **[server/main.py](server/main.py):** adds the middleware; in `ws_endpoint` the check happens before `accept()`, with `close(1008)` on failure.
- **[server/session.py](server/session.py):** malformed JSON no longer closes the session (try/except, `_int` helper); limit of about 30 s on audio frames.
- **Other files:**
  - `scripts/gen_secrets.py`: prints tokens and VAPID keys to stdout;
  - [scripts/check_env.py](scripts/check_env.py): `.env` untracked and with 600 permissions, `serve` points only to 8001, no active funnel;
  - `.env.example`;
  - `.gitignore`: adds `data/`.
- **Tests:**
  - shared fixtures (FakeHome, ToolLlm, GoogleHome) move to `tests/conftest.py`;
  - `tests/test_auth.py`: 403 without login, foreign Origin closed with 1008, wrong Host, local CSWSH blocked, malformed JSON, `/healthz`.
- **User:** `tailscale serve --bg 8001` (**not** 8000).
- **Check:**
  - `curl 127.0.0.1:8001/` returns 403;
  - from the iPhone on cellular, `https://voicebot-mac…ts.net` loads and the text chat replies (proves that the WS goes through `serve`);
  - with the VPN off the domain does not respond.

### M2: reliable voice on iOS Safari
- **[web/app.js](web/app.js):**
  - in the Start tap, before any `await`: create **a single** `AudioContext` at the hardware sample rate (removing `sampleRate: 24000`), call `resume()`, set `navigator.audioSession.type='play-and-record'` if it exists, unlock `speechSynthesis`;
  - pass `audioContext` to `MicVAD.new`;
  - mute with `track.enabled` instead of `vad.pause()`;
  - if the microphone ends (`track.onended`), reacquire it;
  - "Tap to resume" overlay when the state is `interrupted`;
  - on iOS hide "Speaker mode";
  - barge-in with `speechSynthesis.cancel()`.
- **[web/player-worklet.js](web/player-worklet.js):** linear resampling 24 kHz → context sample rate; positions stay in source samples, so server-side `heard_seq` does not change.
- **[web/style.css](web/style.css) and [web/index.html](web/index.html):**
  - 16px textarea (avoids zoom), `:hover` only inside `@media (hover:hover)`, `svh`, targets of at least 44px;
  - `enterkeyhint="send"`;
  - "Type" button that opens the chat without the microphone.
- **Check:**
  - on the Mac, `scripts/e2e_browser.py` and `e2e_ws.py` with no regressions;
  - on the iPhone: greeting, question and spoken answer, barge-in in about 300 ms, mute and unmute, a phone call during the session shows the overlay.

### M3: lifecycle and installable PWA
- **[web/app.js](web/app.js):**
  - WS reconnection with backoff from 0.5 to 10 s, with a "Reconnecting…" or "Mac unreachable" pill;
  - handling of `visibilitychange` and `pageshow` (resumes context, microphone and WS);
  - `wakeLock` while voice is active;
  - `client_id` in `localStorage`, sent in `hello`;
  - registration of `sw.js`.
- **Server:** `app.state.histories` registry (`client_id` → history, TTL 900 s). After a reconnection the history is restored, the pending (outstanding confirmation) is **not**.
- **New files:**
  - `web/manifest.webmanifest` ("Home", standalone);
  - `web/icons/` generated from `favicon.svg` (opaque background), Apple meta tags in `index.html`;
  - `web/sw.js`: cache-first for `/vendor/*` (versioned), network-first for the shell with a "Mac unreachable" offline page, never cache `/ws` and `/api`.
- **Check:**
  - pytest test: after a reconnection with the same `client_id` the history is there;
  - on the iPhone: lock and unlock with memory preserved; airplane mode and recovery; server off shows the offline page; the 11 MB wasm is not downloaded again.

### M4: API for quick commands
- **[server/session.py](server/session.py):**
  - extract `run_text(text)` from `handle_text`, without the lock (the caller takes it);
  - new `HeadlessSession(Session)` with `ws=None`, which collects events per turn.
- **`server/phone.py`** (new): `PhoneSessions`.
  - Lock per session, TTL 600 s, at most 8 sessions.
  - `command()` clears the pending (a "yes" sent to `/command` never executes risky actions). Waits for the turn up to `API_TIMEOUT_S` (20 s); beyond that it returns `running` without cancelling the turn, and the answer then arrives via push.
  - Risky actions: with `API_ALLOW_RISKY=false` (default) the response is `blocked` ("use the app"). With `true` it is `needs_confirmation`, followed by `/api/confirm` with a PIN within 60 s.
  - An expired or unknown confirm returns 410 and **never reaches the LLM**.
  - `_resolve_pending` and `yes_no` from [server/tools.py](server/tools.py) are reused unchanged.
- **`server/api.py`** (new):
  - `POST /api/command`;
  - `POST /api/confirm`;
  - `GET /api/status` (route, tools, provider, warnings);
  - router included before the static mount.
- **`tests/test_api.py`:** 401/503, `done` with a call in FakeHome, `blocked`, PIN confirmation, 410 without LLM, `running`, Google route replying "sent", no regression on WS chat.
- **Check:** `curl` with Bearer to `https://$PUBLIC_HOST/api/command`, latency under 8 s.

### M5: Siri, widgets, Control Center (documentation)
- **`docs/IPHONE.md`** (new):
  - **"Home assistant"** Shortcut: Dictate Text, POST `/api/command`, branch for `needs_confirmation`, Speak the response, "Mac unreachable" error branch;
  - "Message home";
  - fixed buttons ("Turn off all lights");
  - how to add them to widgets, Control Center and the Action button;
  - **never** locks or doors from the Lock Screen;
  - do not share the Shortcuts (the token is in plain text);
  - lost phone procedure.
- Update [README.md](README.md) and [docs/GUIDE.md](docs/GUIDE.md) with a pointer.
- **Check:** "Hey Siri, Home assistant" → "turn off the light" gets a spoken answer; a risky command replies `blocked`; with the server off you hear "unreachable".

### M6: notifications (Web Push)
- **Dependency:** `pywebpush` in `pyproject.toml` and `uv.lock`.
- **Config:** `VAPID_*` (`mailto:` subject required), `PUSH_STORE=data/push_subscriptions.json`, `HA_NOTIFY_ENTITY`.
- **`server/push.py`** (new): `PushHub`.
  - File with 0600 permissions, at most 10 subscriptions, only host `web.push.apple.com` (anti-SSRF).
  - Subscriptions that respond 404 or 410 are removed.
  - Also sends a toast to open WS sessions.
- **`server/ha_events.py`** (new): polling of `input_text.voicebot_notify` every 10 s with a non-admin `HA_TOKEN`, push when it changes, no replay at startup.
- **[server/ha.py](server/ha.py):** `state(entity_id)` method.
- **[server/tools.py](server/tools.py):** `notify_phone` tool ("send a notification now"), modeled on `ANNOUNCE_TOOL`, active in home and away.
- **Endpoints:** `/api/push/{key,subscribe,unsubscribe,test}` (login plus Origin) and `/api/notify` (Bearer).
- **Status push:** when HA goes down or comes back up, with a 120 s debounce.
- **Web:** `push` and `notificationclick` handlers in `sw.js`; "Phone notifications" button (standalone only) with Test and Disable; banner for warnings.
- **`tests/test_push.py`:** `webpush` replaced by a mock.
- **User:** in HA create the `input_text.voicebot_notify` helper and the automations that write to it.
- **Check:** `curl /api/notify` reaches a locked iPhone; a change of the helper arrives in about 15 s; "send to my phone: buy milk" produces a push.

### M7: Mac always reachable
- **`scripts/serve.sh`:** `caffeinate -is uv run python -m server`, with a warning if `serve` does not point to 8001.
- Optional **LaunchAgent**, as an example in `docs/launchd/`.
- **[scripts/check_env.py](scripts/check_env.py):** checks Tailscale Running, `pmset`, `/healthz` reachable on ts.net, VAPID keys.
- **`docs/IPHONE.md`:**
  - "Mac always on" section: lid open on power, or clamshell; warning about `disablesleep`;
  - troubleshooting table.

## Security (summary)
- **Exposure:** loopback only; only 8001 via `serve`; Tailscale ACLs; `check_env` fails if there is a funnel or if `serve` points to 8000.
- **Authentication:** fail-closed Tailscale login; Origin and Host checked; per-device Bearer for `/api`; the PWA contains no secrets.
- **Risky actions:** the deterministic confirmation stays as is; disabled by default from the API.
- **Secrets:** in `.env` (600 permissions) and `data/` (gitignored). The server does not use `HA_ADMIN_TOKEN`.

## Risks and limits
- Voice does not work with the screen locked, only Siri. Siri's behavior with the phone locked and its timeout need to be measured.
- WebSocket through `serve` is not officially documented: verified in M1.
- If the Mac sleeps, nothing works, notifications included.
- Away from home the Google route only confirms the send. HA events only arrive from cloud integrations.
- Wake lock in standalone requires iOS 18.4 or later. Web Push requires iOS 16.4 or later and the app installed on the Home Screen.
- LLM and TTS are shared between phone and desktop: only one voice session at a time should be used.

## End-to-end verification
1. `uv run pytest -q` is all green and `check_env.py` shows no FAIL. On the Mac, `e2e_browser.py` and `e2e_ws.py` pass.
2. **Security:** VPN off → unreachable; login not allowed → 403; `/ws` from a foreign origin → 1008; `/api` without Bearer → 401.
3. **iPhone on cellular, away from home:**
   - voice from the PWA (barge-in, screen lock);
   - chat;
   - Siri;
   - risky command → `blocked`;
   - widgets;
   - push (curl, `notify_phone` tool, HA automation);
   - server off → "Mac unreachable".
4. `git diff main --stat`, then a PR from `feat/iphone`, with no attribution.
