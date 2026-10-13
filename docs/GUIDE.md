# Complete guide

A voice-to-voice chatbot that runs entirely locally on Apple Silicon, for free. You talk, it answers out loud in your language. You can interrupt it at any time.

```
Browser (Chrome)                               Server (FastAPI, localhost)
┌────────────────────────────┐   WebSocket   ┌──────────────────────────────────────────┐
│ mic → Silero VAD (vad-web) ├──── PCM16 ───►│ Parakeet STT (MLX) → lingua (lingua-py)  │
│                            │               │ → Ollama Qwen 3.6 (streaming)            │
│ AudioWorklet player  ◄─────┼──── PCM16 ────┤ → sentence chunker → Pocket TTS (stream) │
│ barge-in, UI               │◄──── JSON ────┤   (one model per language, CPU)          │
└────────────────────────────┘               └──────────────────────────────────────────┘
```

Latency measured on an M5 Pro: **~0.4–0.8 s** from the moment the server receives the utterance to the first audio (STT ~70 ms, LLM TTFT ~50–450 ms, TTS first chunk ~25 ms), plus the VAD silence wait (550 ms).

## Requirements

- Apple Silicon Mac, ≥ 32 GB of RAM (required by Ollama's MLX backend; with less, use `gemma4:e4b-mlx`, which is less accurate)
- [uv](https://docs.astral.sh/uv/), Homebrew, Google Chrome desktop

## Installation

```bash
brew install --cask ollama                 # the app, not the formula
launchctl setenv OLLAMA_KEEP_ALIVE -1      # keeps the model in memory; then restart the Ollama app
open -a Ollama
ollama pull qwen3.6:35b-a3b-nvfp4          # 23 GB, MoE with 3B active parameters: fast and reliable with tools

uv sync                                    # creates .venv with Python 3.12 and the dependencies
cp .env.example .env                       # optional: every value has a default
uv run python scripts/check_env.py         # checks everything (downloads the STT/TTS models on first run)
```

## Running

```bash
uv run uvicorn server.main:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000> in Chrome, press "Start talking" and speak. The sphere in the middle follows the voice in real time: yours when you speak, the bot's when it answers. Its color changes with the state (blue: listening/speaking, purple: thinking, gray: microphone off). Subtitles scroll below the sphere; the full history is in the panel at the top right. Space bar or the microphone button: mute/unmute the microphone. The first run downloads the models (~4 GB); after that, the server is ready in ~6 s.

- **Interrupting**: talk over the bot. The audio pauses immediately; if it really is speech (not echo or noise) the answer is discarded and the bot listens. Only the part you actually heard stays in the history.
- **Silencing it**: tap the circle while it speaks. It stops generating and remembers only the part you heard.
- **Switching language**: just speak in the other language, or ask for it ("let's switch to Italian", "speak French").
- **Pauses mid-sentence**: if you resume talking within ~1.2 s of the bot starting to answer, the two parts are merged into a single question.
- **Languages**: the language is detected from the transcribed text. Short phrases ("yes", "ok") keep the current language. Local voices for Italian, English, French, German, Spanish, Portuguese and Dutch; the other European languages recognized by Parakeet use the browser's voice.
- **Text chat**: the speech bubble icon at the top opens the panel with a text field (Enter to send, Shift+Enter for a new line). It is the same conversation as voice: with voice active the bot also answers out loud, otherwise only in writing. You can type without ever pressing "Start talking", and switch to voice at any time without losing context. "End" turns off the microphone and audio but keeps the conversation; "New conversation" resets it. A message typed while the bot is answering interrupts it just like voice, but is never merged with the previous message.
- **Speakers**: everything is more robust with headphones. Without headphones Chrome cancels the echo; if the bot interrupts itself, turn on "Speaker mode" at the bottom.

## Home control (Google Home devices)

Google offers no free API for controlling devices linked to Google Home from a server (the Home APIs are Android/iOS apps only; the Google Home MCP is paid and US-only). That is why the voicebot goes through **Home Assistant** (free, local), which connects to the same devices, and uses it through its MCP server. The LLM picks the right tool ("turn off the kitchen light" → `HassTurnOff`), Home Assistant executes it, and the bot confirms out loud.

What you can do: turn on/off/adjust lights and plugs, thermostats, blinds, fans, vacuums; ask for state ("is the bedroom light on?", "what's the temperature?"); control speakers and TVs (pause, next, volume, search and play if you have Music Assistant); lists (shopping); Home Assistant scripts (your "routines"); announcements on speakers. Locks, doors, gates, garage doors and alarms always ask for confirmation ("Do you really want to unlock the door?" → "yes"): the question is built by code from the actual action, and the "yes" is verified without the LLM. You can answer by voice or with the Yes/No buttons.

What you **cannot** do (no API): start routines created in the Google Home app, timers/alarms on Nest speakers, Gemini for Home features. Recreate your routines as Home Assistant scripts.

### Setup (one time)

1. **Install Home Assistant OS** in a VM on the Mac with [UTM](https://mac.getutm.app) (network in *bridged* mode, so it sees the devices at home), or on a mini-PC / Raspberry / Home Assistant Green. Don't use Docker on the Mac: without host networking, device discovery doesn't work. In UTM also add a second **Host Only** network adapter: bridged over the Mac's Wi-Fi intermittently drops the Mac ↔ VM link, Host Only doesn't (use its IP, e.g. `192.168.128.2`, for the browser and the voicebot). Set the language to Italian.
2. **Bring your devices into Home Assistant**:
   - **Matter** devices already in Google Home: Google Home app → device → *Device sharing / Linked Matter apps & services* → Home Assistant (requires the Home Assistant app on your phone);
   - other brands (Hue, TP-Link/Tapo, Tuya/Smart Life, Shelly, IKEA…): add their integration in *Settings → Devices & services*;
   - Nest speakers, Chromecast, Google TV: **Google Cast** integration (discovered automatically).
   - Give devices and rooms clear Italian names ("Luce cucina", area "Cucina"): the bot will use them. If you add synonyms (aliases) to an entity, include the main name among the aliases too: when aliases are present, Home Assistant only recognizes those.
   - Music (Spotify Premium): install the **Music Assistant** add-on, link Spotify from its page and expose its players to the bot; with `HA_DEFAULT_PLAYER=Speaker Salotto` the bot knows where to play when you don't name the room.
3. *Settings → Voice assistants → Expose*: choose which devices the bot can control.
4. Add the **Model Context Protocol Server** integration (API: Assist).
5. Create a dedicated **non-administrator** user (e.g. "voicebot"), log in as that user and in *Profile → Security* create a **long-lived access token**.
6. (Optional) **Google Assistant SDK** integration: adds the `google_assistant` tool for commands that only exist in Google (e.g. "turn on the living room TV") and announcements on speakers. It is an experimental API that Google is phasing out: it may stop working.
7. In the voicebot's `.env`:

   ```dotenv
   HOME_BACKEND=ha
   HA_URL=http://192.168.128.2          # VM in UTM: use the IP of the "Host Only" adapter (stable Mac-VM link)
   HA_TOKEN=eyJ...
   HA_ANNOUNCE_PLAYERS=media_player.cucina,media_player.salotto   # optional, announcements via tts.speak
   ```

8. `uv run python scripts/check_env.py` checks the connection, tools, exposed devices and announcements. Restart the server: the logs show `home tools: …`.

The server only listens on `127.0.0.1`: don't expose it to the network, anyone could control your home. To use it from an iPhone there is a secure way (Tailscale, login and token): see [IPHONE.md](IPHONE.md).

## Home control while away (Google Assistant)

If Home Assistant runs in the VM on the Mac and you take the Mac out of the house, the local devices (Cast, Matter, Music Assistant) are no longer reachable. In that case the voicebot sends commands to **Google's cloud**, which executes them on your home devices: lights, plugs, TVs, speakers, Spotify, thermostat, Google Home routines.

1. In Home Assistant add the **Google Assistant SDK** integration (*Settings → Devices & services → Add*). It requires a Google Cloud project with OAuth credentials: Home Assistant's setup wizard explains the steps. Sign in with the same Google account as Google Home.
2. In `.env`:

   ```dotenv
   HOME_ROUTE=auto                 # at home: local tools; away: Google
   HOME_PROBE=192.168.1.50:8009    # IP of a Google Home/Chromecast at home (Google Home app → device → Settings → Information)
   ```

   With `HOME_ROUTE=google` it always uses Google, even at home. With `HOME_ROUTE=ha` (default) it only uses the local tools.
3. `uv run python scripts/check_env.py` shows the current mode (`home route: home` or `away`). Restart the server.

With `auto` the bot checks every 30 s (`HOME_PROBE_TTL_S`) whether the Google Home responds: if so you're at home, otherwise it switches to Google. The switch happens automatically, even mid-conversation.

Limitations: Google doesn't confirm the outcome, so the bot says "command sent", not "done"; Google, not Music Assistant, decides what to play; it is an experimental API that Google may shut down. Via Google too, commands that mention locks, doors, gates, alarms or a device of that kind ask for confirmation.

## Home control while away with Home Assistant at home (Tailscale)

With [Tailscale](https://tailscale.com) (free for personal use) the voicebot controls your home from any network, as if you were at home. No ports opened on the router: the Mac and Home Assistant join an encrypted private network.

**Prerequisite:** Home Assistant must stay **at home and always on** (Home Assistant Green, Raspberry Pi, mini-PC or a desktop Mac). If it runs in a VM on the laptop you take with you, it can no longer see the devices when you're away.

1. **In Home Assistant:** *Settings → Add-ons → Store* → **Tailscale** → Install → Start → *Open web UI* and sign in with your Tailscale account. Enable *Start on boot*.
2. **On the Mac:** `brew install --cask tailscale`, open the app from the menu bar and sign in with **the same account**.
3. In the [Tailscale console](https://login.tailscale.com/admin/machines) check that both appear (Home Assistant usually as `homeassistant`). Under *DNS* leave **MagicDNS** on. On the Home Assistant machine disable *Key expiry*, so it doesn't disconnect after 180 days.
4. In the voicebot's `.env`:

   ```dotenv
   HA_URL=http://homeassistant:8123     # MagicDNS name; alternatively the 100.x.y.z IP shown in the console
   ```

5. `uv run python scripts/check_env.py`: it must say `HA_URL … via Tailscale`. Then test from your phone's hotspot.

The same address works at home and away: at home Tailscale connects directly over the local network, with no noticeable slowdown. If Tailscale isn't connected yet when you start the server, the bot starts without home tools and retries on its own every `HA_RETRY_S` seconds (default 30). Music and announcements still play from the home speakers: Music Assistant runs alongside Home Assistant.

Traffic is encrypted by Tailscale, so `http://` is fine. **Don't** expose Home Assistant to the internet by opening ports on the router.

## Groq (optional, free tier)

To use free cloud models instead of the local ones, create a key at <https://console.groq.com> and in `.env`:

```dotenv
GROQ_API_KEY=gsk_...
LLM_PROVIDER=groq        # qwen/qwen3.8-27b
STT_PROVIDER=groq        # whisper-large-v3-turbo
```

If Groq returns an error or rate limit (free tier: ~8K tokens/min for the LLM, 20 requests/min for STT) the server automatically falls back to the local models for 60 s and shows it in the UI.

## Configuration

All keys are in [.env.example](../.env.example). The most useful ones:

| Key | Default | Notes |
|---|---|---|
| `LLM_MODEL` | `qwen3.6:35b-a3b-nvfp4` | `gemma4:26b-nvfp4`: better at arithmetic, but Ollama drops ~10% of its tool calls (the bot retries). `scripts/bench_tools.py` compares models on home commands |
| `VAD_REDEMPTION_MS` | `550` | silence that ends the turn; lower = more responsive but cuts you off during pauses |
| `CONTINUATION_S` | `1.2` | window for merging an utterance resumed right after the answer starts |
| `DEFAULT_LANG` | `it` | initial language |
| `VOICE_IT` … `VOICE_NL` | `giovanni`, `alba`, … | default Pocket TTS voices |
| `STATS_ENABLED` | `true` | records each turn's timings for the `/stats` page (never what was said or typed) |
| `STT_PROVIDER` | `parakeet` | `whisper-mlx` (requires `uv sync --extra whisper`) for non-European languages |

## Latency (`/stats`)

<http://localhost:8000/stats> shows p50 and p95 for each stage (STT, LLM first token, first audio, full turn), the daily trend and the last 20 turns. You can filter by period and by source (voice or chat). First audio turns orange above 1 s and red above 2 s.

The data lives in `data/stats.db` (SQLite, excluded from git) and data older than `STATS_RETENTION_DAYS` is deleted at startup. For each turn only the timings, language, source, provider and number of home actions are stored, never the text. Raw data is at `/stats.json?hours=24&source=voice`.

## Tests

```bash
uv run ruff check .                            # lint (version pinned in the dev dependencies)
uv run pytest                                  # unit tests + WebSocket protocol with fake providers (also runs in CI)
uv run python scripts/bench_llm.py             # model TTFT
uv run python scripts/e2e_ws.py                # E2E without a browser: synthetic voice → answer → transcript
uv run python scripts/e2e_browser.py           # E2E in Chrome with a fake microphone (real VAD)
uv run python scripts/e2e_browser.py --barge   # as above, with interruption and split utterance
HA_TOKEN=... uv run python scripts/e2e_home.py # real voice commands against Home Assistant (server with HOME_BACKEND=ha)
```

GitHub Actions ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) runs `ruff check` and `pytest` on a macOS Apple Silicon runner on every pull request and every push to `main`. The E2E scripts and benchmarks stay manual: they need the real models, the microphone and Home Assistant.

## Structure

| File | Role |
|---|---|
| [server/session.py](../server/session.py) | WebSocket protocol, turns (voice and text chat), barge-in, history, STT→LLM→TTS pipeline |
| [server/stt.py](../server/stt.py), [server/llm.py](../server/llm.py), [server/tts.py](../server/tts.py) | providers with automatic Groq → local fallback |
| [server/langid.py](../server/langid.py) | language detection with hysteresis |
| [server/chunker.py](../server/chunker.py) | splits the LLM stream into speakable sentences (first chunk sent early) |
| [server/tools.py](../server/tools.py), [server/ha.py](../server/ha.py) | home tools (Home Assistant MCP), confirmations for risky actions |
| [server/stats.py](../server/stats.py), [web/stats.html](../web/stats.html) | per-turn timing log (SQLite) and `/stats` page |
| [server/textclean.py](../server/textclean.py) | markdown/emoji cleanup, hallucination and echo filters |
| [web/app.js](../web/app.js), [web/player-worklet.js](../web/player-worklet.js) | microphone, VAD, gapless playback, interruptions, text chat |

## Licenses and attributions

- STT model [nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) (CC-BY-4.0), MLX port [parakeet-mlx](https://github.com/senstella/parakeet-mlx).
- TTS [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts) (MIT code, CC-BY-4.0 weights; the voices have their own licenses, see [kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices)).
- LLM [Gemma 4](https://ai.google.dev/gemma) via [Ollama](https://ollama.com).
- VAD [@ricky0123/vad-web](https://github.com/ricky0123/vad) (Silero VAD, MIT) and [onnxruntime-web](https://github.com/microsoft/onnxruntime) (MIT), bundled in `web/vendor/`.
