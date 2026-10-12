"""Runtime configuration loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    return int(_env(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(_env(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def _env_list(key: str, default: str) -> list[str]:
    return [x.strip() for x in _env(key, default).split(",") if x.strip()]


# The 25 European languages Parakeet TDT v3 transcribes.
PARAKEET_LANGS = (
    "bg,hr,cs,da,nl,en,et,fi,fr,de,el,hu,it,lv,lt,mt,pl,pt,ro,sk,sl,es,sv,ru,uk"
)

# ISO 639-1 code -> Pocket TTS language config name.
POCKET_LANG_NAMES = {
    "it": "italian",
    "en": "english",
    "fr": "french",
    "de": "german",
    "es": "spanish",
    "pt": "portuguese",
    "nl": "dutch",
}

DEFAULT_VOICES = {
    "it": "giovanni",
    "en": "alba",
    "fr": "estelle",
    "de": "juergen",
    "es": "lola",
    "pt": "rafael",
    "nl": "daan",
}


@dataclass
class Config:
    # Providers
    stt_provider: str = field(default_factory=lambda: _env("STT_PROVIDER", "parakeet"))
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "ollama"))
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "pocket"))
    provider_fallback: bool = field(default_factory=lambda: _env_bool("PROVIDER_FALLBACK", True))

    # STT
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "mlx-community/parakeet-tdt-0.6b-v3"))
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"))
    min_utterance_ms: int = field(default_factory=lambda: _env_int("MIN_UTTERANCE_MS", 200))

    # Language detection
    langid_langs: list[str] = field(default_factory=lambda: _env_list("LANGID_LANGS", PARAKEET_LANGS))
    default_lang: str = field(default_factory=lambda: _env("DEFAULT_LANG", "it"))
    langid_min_words: int = field(default_factory=lambda: _env_int("LANGID_MIN_WORDS", 3))
    langid_min_conf: float = field(default_factory=lambda: _env_float("LANGID_MIN_CONF", 0.5))
    langid_min_margin: float = field(default_factory=lambda: _env_float("LANGID_MIN_MARGIN", 0.3))

    # LLM (Ollama, OpenAI-compatible)
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "http://localhost:11434"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "qwen3.6:35b-a3b-nvfp4"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", "ollama"))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 300))
    llm_empty_retries: int = field(default_factory=lambda: _env_int("LLM_EMPTY_RETRIES", 2))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.7))
    history_max_turns: int = field(default_factory=lambda: _env_int("HISTORY_MAX_TURNS", 12))
    history_keep_turns: int = field(default_factory=lambda: _env_int("HISTORY_KEEP_TURNS", 6))

    # TTS
    pocket_preload: list[str] = field(default_factory=lambda: _env_list("POCKET_PRELOAD", "it,en,fr,de,es,pt,nl"))
    pocket_quantize: bool = field(default_factory=lambda: _env_bool("POCKET_QUANTIZE", False))
    edge_voices: dict[str, str] = field(
        default_factory=lambda: {
            "it": _env("EDGE_VOICE_IT", "it-IT-ElsaNeural"),
            "en": _env("EDGE_VOICE_EN", "en-US-AriaNeural"),
        }
    )
    voices: dict[str, str] = field(
        default_factory=lambda: {lang: _env(f"VOICE_{lang.upper()}", v) for lang, v in DEFAULT_VOICES.items()}
    )

    # Groq (optional, free tier)
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY", ""))
    groq_base_url: str = field(default_factory=lambda: _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
    groq_stt_model: str = field(default_factory=lambda: _env("GROQ_STT_MODEL", "whisper-large-v3-turbo"))
    groq_llm_model: str = field(default_factory=lambda: _env("GROQ_LLM_MODEL", "qwen/qwen3.8-27b"))
    groq_history_turns: int = field(default_factory=lambda: _env_int("GROQ_HISTORY_TURNS", 4))
    groq_cooldown_s: int = field(default_factory=lambda: _env_int("GROQ_COOLDOWN_S", 60))

    # Turn-taking (sent to the browser)
    vad_redemption_ms: int = field(default_factory=lambda: _env_int("VAD_REDEMPTION_MS", 550))
    # If the user resumes talking within this many seconds after the bot started speaking,
    # treat it as the continuation of the same sentence (merge, drop the reply).
    continuation_s: float = field(default_factory=lambda: _env_float("CONTINUATION_S", 1.2))
    prefill_on_speech: bool = field(default_factory=lambda: _env_bool("PREFILL_ON_SPEECH", True))

    # Home control (Home Assistant)
    home_backend: str = field(default_factory=lambda: _env("HOME_BACKEND", "none"))  # none | ha
    ha_url: str = field(default_factory=lambda: _env("HA_URL", "http://homeassistant.local:8123"))
    ha_token: str = field(default_factory=lambda: _env("HA_TOKEN", ""))
    # Where home commands go: "ha" (local tools), "google" (Google Assistant cloud only, works away from home),
    # "auto" (local when HOME_PROBE answers, i.e. the Mac is on the home network; Google otherwise)
    home_route: str = field(default_factory=lambda: _env("HOME_ROUTE", "ha"))
    home_probe: str = field(default_factory=lambda: _env("HOME_PROBE", ""))  # host:port on the home LAN
    home_probe_ttl_s: float = field(default_factory=lambda: _env_float("HOME_PROBE_TTL_S", 30.0))
    ha_retry_s: float = field(default_factory=lambda: _env_float("HA_RETRY_S", 30.0))  # 0 = no retry
    ha_mcp_path: str = field(default_factory=lambda: _env("HA_MCP_PATH", "/api/mcp"))
    ha_tool_allowlist: list[str] = field(default_factory=lambda: _env_list("HA_TOOL_ALLOWLIST", ""))
    ha_announce_players: list[str] = field(default_factory=lambda: _env_list("HA_ANNOUNCE_PLAYERS", ""))
    ha_default_player: str = field(default_factory=lambda: _env("HA_DEFAULT_PLAYER", ""))
    ha_tts_entity: str = field(default_factory=lambda: _env("HA_TTS_ENTITY", ""))
    google_fallback: bool = field(default_factory=lambda: _env_bool("GOOGLE_FALLBACK", True))
    tool_max_rounds: int = field(default_factory=lambda: _env_int("TOOL_MAX_ROUNDS", 3))
    tool_timeout_s: float = field(default_factory=lambda: _env_float("TOOL_TIMEOUT_S", 8.0))
    tool_result_max_chars: int = field(default_factory=lambda: _env_int("TOOL_RESULT_MAX_CHARS", 2000))
    home_context_max_chars: int = field(default_factory=lambda: _env_int("HOME_CONTEXT_MAX_CHARS", 12000))
    confirm_risky: bool = field(default_factory=lambda: _env_bool("CONFIRM_RISKY", True))
    confirm_ttl_s: float = field(default_factory=lambda: _env_float("CONFIRM_TTL_S", 30.0))
    risky_domains: list[str] = field(default_factory=lambda: _env_list("RISKY_DOMAINS", "lock,alarm_control_panel,valve,siren"))

    # Server
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    warmup: bool = field(default_factory=lambda: _env_bool("WARMUP", True))
    # Browser pages allowed to open /ws on the local port (blocks cross-site WebSocket hijacking)
    local_hosts: list[str] = field(default_factory=lambda: _env_list("LOCAL_HOSTS", "127.0.0.1,localhost"))
    local_origins: list[str] = field(default_factory=lambda: _env_list(
        "LOCAL_ORIGINS", ",".join(f"http://{h}:{_env('PORT', '8000')}" for h in ("127.0.0.1", "localhost"))))

    # Seconds a closed client's conversation is kept for its reconnection (screen lock, network change)
    client_history_ttl_s: float = field(default_factory=lambda: _env_float("CLIENT_HISTORY_TTL_S", 900.0))

    # Remote access from the phone: a second loopback port published on the tailnet by
    # `tailscale serve` (never funnel). Every request on it must carry an allowed Tailscale login.
    remote_enabled: bool = field(default_factory=lambda: _env_bool("REMOTE_ENABLED", False))
    remote_port: int = field(default_factory=lambda: _env_int("REMOTE_PORT", 8001))
    # e.g. voicebot-mac.tailXXXX.ts.net, plus ":<port>" when `tailscale serve --https=<port>` is not 443
    public_host: str = field(default_factory=lambda: _env("PUBLIC_HOST", "").lower())
    ts_allowed_logins: list[str] = field(default_factory=lambda: [x.lower() for x in _env_list("TS_ALLOWED_LOGINS", "")])
    api_tokens: list[str] = field(default_factory=lambda: _env_list("API_TOKENS", ""))  # one per device (Shortcuts)
    # /api/command (Siri Shortcuts): wait at most this long for the reply, then answer "running"
    api_timeout_s: float = field(default_factory=lambda: _env_float("API_TIMEOUT_S", 20.0))
    phone_session_ttl_s: float = field(default_factory=lambda: _env_float("PHONE_SESSION_TTL_S", 600.0))
    # Locks/doors/alarms from the API: refused unless API_ALLOW_RISKY, then confirmed with /api/confirm
    api_allow_risky: bool = field(default_factory=lambda: _env_bool("API_ALLOW_RISKY", False))
    api_confirm_ttl_s: float = field(default_factory=lambda: _env_float("API_CONFIRM_TTL_S", 60.0))
    api_confirm_pin: str = field(default_factory=lambda: _env("API_CONFIRM_PIN", ""))


cfg = Config()
