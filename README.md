<div align="center">

# 🎙️ Local Voicebot

**Voice-to-voice assistant that runs 100% locally on an Apple Silicon Mac or a Windows PC. Free, fast, interruptible.**

[![CI](https://github.com/SimoneZacchetti/local-voicebot/actions/workflows/ci.yml/badge.svg)](https://github.com/SimoneZacchetti/local-voicebot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-WebSocket-009688?logo=fastapi&logoColor=white)
![MLX](https://img.shields.io/badge/Apple%20Silicon-MLX-000000?logo=apple&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white)
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

### macOS

Requirements: Apple Silicon Mac (≥ 32 GB RAM) and Chrome. Everything else is installed for you.

```bash
git clone https://github.com/SimoneZacchetti/local-voicebot.git
cd local-voicebot
./start.sh
```

`start.sh` installs what is missing ([uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com), the Python dependencies), downloads the models (~27 GB, first run only), creates `.env` with working defaults, checks the setup, starts the server and opens <http://localhost:8000>. Press **Start talking**.

Next runs take ~10 s: just run `./start.sh` again. `./start.sh --check` re-runs the setup check, `--no-open` skips the browser. Manual steps are in [docs/GUIDE.md](docs/GUIDE.md#installation).

### Windows

Requirements: Windows 10 22H2 or Windows 11 (x64), ≥ 16 GB RAM (32 GB for the default model), Chrome or Edge. An NVIDIA or AMD GPU is recommended: without one the LLM runs on the CPU and answers more slowly.

```bat
git clone https://github.com/SimoneZacchetti/local-voicebot.git
cd local-voicebot
.\start.cmd
```

No git? `winget install -e --id Git.Git` (then open a new terminal), or use Code > Download ZIP, extract it to `C:\src\local-voicebot` and run `.\start.cmd` from that folder (or double-click `start.cmd`).

`.\start.cmd` works in both cmd and PowerShell and does the same as `start.sh`: it installs what is missing with `winget` (uv, Ollama, the Visual C++ runtime), picks an LLM that fits your RAM and GPU, downloads the models, creates `.env`, checks the setup, starts the server and opens the browser. `.\start.cmd -Check` re-runs the setup check, `.\start.cmd -NoOpen` skips the browser. Clone into a short path outside OneDrive (e.g. `C:\src\local-voicebot`). Manual steps and Windows troubleshooting are in [docs/GUIDE.md](docs/GUIDE.md#windows).

### What runs where

| | macOS (Apple Silicon) | Windows |
|---|---|---|
| STT | Parakeet on MLX ([parakeet-mlx](https://github.com/senstella/parakeet-mlx)) | Parakeet on ONNX Runtime ([onnx-asr](https://github.com/istupakov/onnx-asr), CPU) |
| LLM (Ollama) | `qwen3.6:35b-a3b-nvfp4` (MLX) | `qwen3.6:35b-a3b` (GGUF); `qwen3.5:9b` or `qwen3.5:4b` on smaller PCs, chosen by `start.cmd` |
| TTS | Pocket TTS (CPU) | Pocket TTS (CPU) |

The backend is picked automatically (`STT_BACKEND=auto`); see [docs/GUIDE.md](docs/GUIDE.md#configuration).

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

Parakeet (CC-BY-4.0), Kyutai Pocket TTS (MIT / CC-BY-4.0), onnx-asr and onnxruntime (MIT), Silero VAD and onnxruntime-web (MIT). Details in [docs/GUIDE.md](docs/GUIDE.md#licenses-and-attributions).
