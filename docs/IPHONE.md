# Voicebot from your iPhone

Use the voicebot from your phone, at home and away:

- **"Home" app** on the Home Screen: voice and chat, just like on the computer.
- **Siri, widgets, Control Center and the Action button**: one command at a time, even with the phone locked.

The computer (Mac or Windows PC) stays the "brain": it must be on and awake. The phone reaches it through **Tailscale**, an encrypted private network. No ports are opened on the router and nothing is exposed to the internet.

```
iPhone ──Tailscale──► https://<host>.<tailnet>.ts.net:9443 ──► computer 127.0.0.1:8001 ("phone" port)
                                                               computer 127.0.0.1:8000 (local browser)
```

## 1. Setup (one time)

### Tailscale

1. **Computer**:
   - **Mac**: install the *Standalone* version from <https://pkgs.tailscale.com/stable/#macos>, log in and enable the CLI (menu bar icon → *Settings* → *CLI integration*).
   - **Windows**: `winget install -e --id Tailscale.Tailscale`, then log in from the tray icon. The `tailscale` command works in a new terminal; if it doesn't, use the full path `& "C:\Program Files\Tailscale\tailscale.exe"`.
2. **iPhone**: install the [Tailscale](https://apps.apple.com/app/tailscale/id1470499037) app, log in with **the same account** and turn on **VPN On Demand**.
3. Give the computer a neutral name, only inside Tailscale. The name ends up in the public HTTPS certificate log. The computer's own name does not change.

   ```bash
   tailscale set --hostname=voicebot-mac
   ```

4. In the [DNS console](https://login.tailscale.com/admin/dns) enable **MagicDNS** and **HTTPS Certificates**.

### Server

1. Generate a token for each device. The script prints the tokens: copy them into `.env` and don't share them.

   ```bash
   uv run python scripts/gen_secrets.py iphone
   ```

2. In `.env`:

   ```dotenv
   REMOTE_ENABLED=true
   REMOTE_PORT=8001
   PUBLIC_HOST=voicebot-mac.tailXXXX.ts.net:9443   # name from `tailscale status`; for :9443 see below
   TS_ALLOWED_LOGINS=tuo.account@gmail.com         # the allowed Tailscale login
   API_TOKENS=<generated token>
   ```

   Then make `.env` readable only by you. Mac: `chmod 600 .env`. Windows (PowerShell):

   ```powershell
   icacls .env /inheritance:r /grant:r "*$([Security.Principal.WindowsIdentity]::GetCurrent().User.Value):(M)"
   ```

3. Start the server with both ports (`./start.sh` or `.\start.cmd` work too):

   ```bash
   uv run python -m server
   ```

4. Publish **only** the phone port:

   ```bash
   tailscale serve --bg --https=9443 8001
   ```

   Port 9443 is used because 443 may already be taken on the computer (for example by Docker's Traefik). If your 443 is free, use `tailscale serve --bg 8001` and remove `:9443` from `PUBLIC_HOST`.

5. Run `uv run python scripts/check_env.py`. Everything must be `OK`: `.env` untracked and readable only by you, `tailscale serve` publishing only `127.0.0.1:8001`, funnel off.

6. **Windows only**: keep the PC awake while it is plugged in, or the phone can't reach it:

   ```powershell
   powercfg /change standby-timeout-ac 0
   ```

> **Never** `tailscale funnel`, never `HOST=0.0.0.0`, never port forwarding on the router: the server controls the house.

## 2. "Home" app (voice and chat)

1. Open Safari on the iPhone and go to `https://voicebot-mac.tailXXXX.ts.net:9443`.
2. Tap **Share** → **Add to Home Screen**.
3. Open the **Home** icon and tap **Start talking**. Allow the microphone: on iOS the installed app may ask again every time it opens.

What to expect:

- **Voice**: works only with the app open and the screen on. During a conversation the screen stays on by itself.
- **Screen lock or network change**: the app reconnects by itself ("Reconnecting…"). The bot remembers the conversation for 15 minutes (`CLIENT_HISTORY_TTL_S`).
- **Calls and Siri**: they interrupt the audio. **"Tap to resume"** appears.
- **Chat**: the speech bubble icon at the top opens the chat, which works without the microphone.
- **Echo**: with the phone speaker at high volume, part of the bot's voice can leak back into the microphone. The bot almost always recognizes it. With AirPods or headphones the problem goes away.
- **Computer off or Tailscale disabled**: the app shows "Server unreachable".

## 3. Siri and Shortcuts

Shortcuts call `POST /api/command`. The bot runs the request with the same model, the same tools and the same safety as voice, and replies with text that Siri reads aloud.

### "Home assistant" (voice, hands-free)

In the **Shortcuts** app tap **+** and add these actions:

1. **Dictate Text**: language *Italian*, *Stop Listening: After Pause*.
2. **Get Contents of URL**:
   - URL: `https://voicebot-mac.tailXXXX.ts.net:9443/api/command`
   - Method: **POST**
   - Headers:
     - `Authorization` = `Bearer <your token>`
     - `Content-Type` = `application/json`
   - Request Body: **JSON**
     - `text` (Text) = *Dictated Text* variable
     - `session_id` (Text) = `phone`
3. **Get Dictionary Value**: key `reply` from *Contents of URL*.
4. **Speak Text**: *Dictionary Value*.

Name it **"Home assistant"**. Avoid plain "Home": it gets confused with Apple's Home app. Then try it: *"Hey Siri, Home assistant"* → *"turn off the kitchen light"*.

For "what's the weather here?", add **Get Current Location** before **Get Contents of URL**, and two more Number fields in the body: `lat` = *Current Location* › Latitude and `lon` = *Current Location* › Longitude. Without them the bot uses `WEATHER_LOCATION` or the Mac's IP location, which is wrong when you are away.

`session_id: "phone"` keeps the conversation for 10 minutes (`PHONE_SESSION_TTL_S`): after *"turn on the kitchen light"*, *"and in the bedroom?"* works.

### "Message home" (text)

Same as "Home assistant", but use **Ask for Input** (type Text) instead of **Dictate Text**. Useful for typing silently.

### Fixed buttons

A shortcut without dictation, with a fixed `text`, for example `turn off all the lights` or `turn on the living room TV`. One tap is enough.

### Where to put them

- **Widget**: long-press the Home Screen → **Edit** → **Add Widget** → **Shortcuts**.
- **Control Center** and **Lock Screen** (iOS 18): **Edit** → **Add a Control** → **Shortcuts** → pick the shortcut.
- **Action button** (iPhone 15 Pro and later): **Settings** → **Action Button** → **Shortcut**.

### Possible responses

| `status` | Meaning |
|---|---|
| `done` | Done: `reply` is the answer. Away from home, with commands via Google, `reply` says "sent". |
| `running` | The bot is taking longer than `API_TIMEOUT_S` (20 s). The command is not cancelled. |
| `blocked` | Locks, doors or alarms: **blocked by default** from Siri. Use the app. |
| `needs_confirmation` | Only with `API_ALLOW_RISKY=true`: a confirmation is required (see below). |
| `error` | Empty message or server error. |

If the computer is unreachable, **Get Contents of URL** fails and Shortcuts shows an error.

### Locks and doors from Siri (not recommended)

They are refused by default. To enable them:

1. In `.env` set `API_ALLOW_RISKY=true` and an `API_CONFIRM_PIN=<pin>` of at least 6 digits (the server refuses to start otherwise).
2. In the shortcut, after **Get Contents of URL** add:
   1. **If** `status` is `needs_confirmation`:
      - **Speak Text** `question`;
      - **Choose from Menu**: Yes / No;
      - **Ask for Input** (PIN);
      - **Get Contents of URL**: `POST /api/confirm` with JSON `{"session_id": "phone", "yes": true|false, "pin": "<input>"}` and the same headers.
   2. You have **60 seconds** (`API_CONFIRM_TTL_S`). An expired confirmation returns `expired` and executes nothing.
   3. A "yes" sent as a normal command never confirms anything.
   4. A wrong PIN cancels the action (`forbidden`): repeat the command to try again.

**Never** attach lock, door or alarm shortcuts to the Lock Screen or Control Center.

## 4. Security

- **Who can connect**: only devices on your tailnet with a login listed in `TS_ALLOWED_LOGINS`. Everyone else gets 403. The login is set by Tailscale and cannot be spoofed from the phone.
- **App origin**: the web app must come exactly from `https://PUBLIC_HOST`. Siri uses the token instead.
- **Tokens**: `API_TOKENS` holds one token per device, so you can revoke just one.
  - The token is stored in plain text inside the shortcut and syncs via iCloud: **don't share** that shortcut.
  - The server never writes tokens to the logs.
- **Local browser**: on the local port, `/ws` only accepts pages from `127.0.0.1` and `localhost`, so any random site open in Chrome cannot control the house.

### Lost phone

1. In the [Tailscale console](https://login.tailscale.com/admin/machines) remove the device.
2. Remove its token from `API_TOKENS` in `.env` and restart the server.

## 5. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| "Server unreachable" | Computer asleep, server stopped or Tailscale off | Wake the computer, start the server (`./start.sh`, `.\start.cmd` or `uv run python -m server`), check Tailscale on the computer and the iPhone. On Windows, `powercfg /change standby-timeout-ac 0` stops the PC from sleeping. |
| The page won't open | `tailscale serve` not active | `tailscale serve status`. If empty, `tailscale serve --bg --https=9443 8001`. |
| 403 | Login not allowed or wrong `PUBLIC_HOST` | Compare `TS_ALLOWED_LOGINS` and `PUBLIC_HOST` with `tailscale status`. The server log gives the reason (`refused … login/host/origin`). |
| Invalid certificate | The computer's 443 is taken by another program | Use `--https=9443` and `PUBLIC_HOST=…:9443`. |
| Siri reports an error | Wrong token (401) or computer unreachable | Check the `Authorization: Bearer …` header. |
| Slow first reply | After inactivity, the OS moved the model to disk | The bot reloads it when the phone connects. Freeing RAM (Docker, heavy apps) helps. |
| The bot interrupts itself | Speaker echo | Lower the volume or use headphones. |
| Home commands ignored away from home | The Google route is not active | `HOME_ROUTE=auto` with `HOME_PROBE`, see [GUIDE.md](GUIDE.md#home-control-while-away-google-assistant). |
