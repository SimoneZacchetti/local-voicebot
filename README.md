<div align="center">

# 🎙️ Local Voicebot

**Assistente vocale voce-a-voce, 100% locale su Apple Silicon. Gratis, veloce, interrompibile.**

[![CI](https://github.com/SimoneZacchetti/local-voicebot/actions/workflows/ci.yml/badge.svg)](https://github.com/SimoneZacchetti/local-voicebot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-WebSocket-009688?logo=fastapi&logoColor=white)
![MLX](https://img.shields.io/badge/Apple%20Silicon-MLX-000000?logo=apple&logoColor=white)
![Ollama](https://img.shields.io/badge/LLM-Ollama-black)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-MCP-41BDF5?logo=homeassistant&logoColor=white)

</div>

Parli, lui risponde a voce nella tua lingua in **~0,5 s**. Puoi interromperlo in qualsiasi momento, scrivergli in chat e, se vuoi, fargli comandare la casa.

```
Browser                               Server (localhost)
mic → Silero VAD ──── PCM16 ────►  Parakeet STT → lingua → Qwen 3.6 (Ollama)
player ◄────────────── PCM16 ────  frasi → Pocket TTS (streaming)
```

## ✨ Funzioni

- 🗣️ **Voce-a-voce in streaming**: STT ~70 ms, primo audio in ~0,4–0,8 s
- ✋ **Barge-in**: parlagli sopra e si ferma, ricordando solo ciò che hai sentito
- 🌍 **7 lingue con voce locale** (it, en, fr, de, es, pt, nl), rilevamento automatico
- 💬 **Chat scritta** nella stessa conversazione della voce
- 🏠 **Smart home** via Home Assistant MCP, con conferma vocale per serrature e allarmi
- 🌐 **Anche fuori casa** via Google Assistant, o Tailscale se Home Assistant resta a casa
- 📊 **Latenza sotto controllo**: pagina `/stats` con p50/p95 di ogni fase ([docs/GUIDA.md](docs/GUIDA.md#latenza-stats))
- ☁️ **Groq opzionale** (free tier) con fallback automatico ai modelli locali

## 🚀 Avvio rapido

Requisiti: Mac Apple Silicon (≥ 32 GB RAM), [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com), Chrome.

```bash
ollama pull qwen3.6:35b-a3b-nvfp4
uv sync
cp .env.example .env                      # opzionale: ogni valore ha un default
uv run python scripts/check_env.py        # verifica setup e scarica i modelli
uv run uvicorn server.main:app --host 127.0.0.1 --port 8000
```

Apri <http://localhost:8000> e premi **Inizia a parlare**.

## 🧪 Test

```bash
uv run pytest
```

## 📚 Approfondimenti

Setup di Home Assistant, Groq, configurazione completa, script E2E e struttura del codice: **[docs/GUIDA.md](docs/GUIDA.md)**.

## 🔒 Sicurezza

I segreti (`GROQ_API_KEY`, `HA_TOKEN`) vivono solo in `.env`, che è escluso da git. Il server ascolta su `127.0.0.1`: non esporlo in rete.

## 📄 Licenze

Parakeet (CC-BY-4.0), Kyutai Pocket TTS (MIT / CC-BY-4.0), Silero VAD e onnxruntime-web (MIT). Dettagli in [docs/GUIDA.md](docs/GUIDA.md#licenze-e-attribuzioni).
