<div align="center">

# 🎙️ Local Voicebot

**Voice-to-voice assistant that runs 100% locally on Apple Silicon. Free, fast, interruptible.**

[![CI](https://github.com/SimoneZacchetti/local-voicebot/actions/workflows/ci.yml/badge.svg)](https://github.com/SimoneZacchetti/local-voicebot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-WebSocket-009688?logo=fastapi&logoColor=white)
![MLX](https://img.shields.io/badge/Apple%20Silicon-MLX-000000?logo=apple&logoColor=white)
![Ollama](https://img.shields.io/badge/LLM-Ollama-black)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-MCP-41BDF5?logo=homeassistant&logoColor=white)

</div>

You speak, it answers out loud in your language in **~0.5 s**. You can interrupt it at any time, type to it in a chat, and, if you want, let it control your home.

```
Browser                               Server (localhost)
mic → Silero VAD ──── PCM16 ────►  Parakeet STT → language → Qwen 3.6 (Ollama)
player ◄────────────── PCM16 ────  sentences → Pocket TTS (streaming)
```

## ✨ Features

- 🗣️ **Streaming voice-to-voice**: STT in ~70 ms, first audio in ~0.4–0.8 s
- ✋ **Barge-in**: talk over it and it stops, remembering only what you actually heard
- 🌍 **7 languages with a local voice** (it, en, fr, de, es, pt, nl), detected automatically
- 💬 **Text chat** in the same conversation as the voice
- 🏠 **Smart home** via Home Assistant MCP, with spoken confirmation for locks and alarms
- 🌐 **Away from home too** via Google Assistant, or Tailscale if Home Assistant stays at home
- 📱 **From your iPhone**: home-screen app, Siri and widgets, via Tailscale ([docs/IPHONE.md](docs/IPHONE.md))
- 📊 **Latency at a glance**: `/stats` page with p50/p95 for every stage ([docs/GUIDE.md](docs/GUIDE.md#latency-stats))
- ☁️ **Optional Groq** (free tier) with automatic fallback to the local models

## 🚀 Quick start

Requirements: Apple Silicon Mac (≥ 32 GB RAM), [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com), Chrome.

```bash
ollama pull qwen3.6:35b-a3b-nvfp4
uv sync
cp .env.example .env                      # optional: every value has a default
uv run python scripts/check_env.py        # checks the setup and downloads the models
uv run uvicorn server.main:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000> and press **Start talking**.

## 🧪 Tests

```bash
uv run ruff check .
uv run pytest
```

Both run in GitHub Actions on every pull request and every push to `main`.

## 📚 Further reading

Detailed guides:

- **[docs/GUIDE.md](docs/GUIDE.md)**: Home Assistant and Groq setup, full configuration, E2E scripts, code layout
- **[docs/IPHONE.md](docs/IPHONE.md)**: iPhone app, Siri Shortcuts, widgets, security, troubleshooting

## 🔒 Security

Secrets (`GROQ_API_KEY`, `HA_TOKEN`, `API_TOKENS`) live only in `.env`, which git ignores. The server listens only on `127.0.0.1`. The phone reaches it only through `tailscale serve`, with a Tailscale login check and per-device tokens ([docs/IPHONE.md](docs/IPHONE.md#4-security)). Never use `tailscale funnel`, never bind to `0.0.0.0`.

## 📄 Licenses

Parakeet (CC-BY-4.0), Kyutai Pocket TTS (MIT / CC-BY-4.0), Silero VAD and onnxruntime-web (MIT). Details in [docs/GUIDE.md](docs/GUIDE.md#licenses-and-attributions).
