#!/usr/bin/env bash
# One command from a fresh clone to a running voicebot: ./start.sh
#
# Idempotent: installs what is missing (uv, Ollama, the LLM, Python deps), creates .env,
# runs the environment check the first time, then starts the server and opens the browser.
#   ./start.sh            setup if needed, then run
#   ./start.sh --check    also re-run scripts/check_env.py
#   ./start.sh --no-open  do not open the browser
set -euo pipefail
cd "$(dirname "$0")"

CHECK=0
OPEN=1
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --no-open) OPEN=0 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '\033[31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

# --- Hardware ---
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || die "needs an Apple Silicon Mac (MLX models)."
ram_gb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
if (( ram_gb < 32 )); then
  warn "${ram_gb} GB of RAM: the default LLM needs >= 32 GB. Set LLM_MODEL=gemma4:e4b-mlx in .env."
fi

# --- uv ---
if ! command -v uv >/dev/null; then
  step "Installing uv"
  if command -v brew >/dev/null; then
    brew install uv
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

# --- .env ---
if [[ ! -f .env ]]; then
  step "Creating .env from .env.example (every value has a default; edit it later if you want)"
  cp .env.example .env
  chmod 600 .env
  CHECK=1
fi

# --- Python dependencies ---
step "Syncing Python dependencies"
uv sync --locked

# --- Ollama ---
if ! command -v ollama >/dev/null; then
  step "Installing Ollama"
  command -v brew >/dev/null || die "install Ollama from https://ollama.com/download, then run ./start.sh again."
  brew install --cask ollama
fi
read -r provider model llm_url port < <(uv run --no-sync python -c "from server.config import cfg
print(cfg.llm_provider, cfg.llm_model, cfg.llm_base_url.removesuffix('/').removesuffix('/v1'), cfg.port)")
if ! curl -sf "$llm_url/api/version" >/dev/null; then
  step "Starting Ollama"
  open -a Ollama 2>/dev/null || (nohup ollama serve >/dev/null 2>&1 &)
  for _ in $(seq 60); do curl -sf "$llm_url/api/version" >/dev/null && break; sleep 1; done
  curl -sf "$llm_url/api/version" >/dev/null || die "Ollama is not answering at $llm_url."
fi

[[ "$model" == *:* ]] || model="$model:latest"
if [[ "$provider" == ollama ]] && ! ollama list | awk 'NR>1 {print $1}' | grep -qxF "$model"; then
  step "Downloading $model (first time only; the default model is ~23 GB)"
  ollama pull "$model"
fi

# --- Environment check (downloads the STT/TTS models the first time) ---
if (( CHECK )); then
  step "Checking the environment"
  uv run --no-sync python scripts/check_env.py || die "fix the FAIL lines above, then run ./start.sh again."
fi

# --- Run ---
url="http://localhost:$port"
if (( OPEN )); then
  (
    for _ in $(seq 300); do
      if curl -sf "$url/healthz" >/dev/null; then
        open -a "Google Chrome" "$url" 2>/dev/null || open "$url"
        exit 0
      fi
      sleep 1
    done
  ) &
fi
step "Starting the voicebot: $url opens when the models are loaded (Ctrl+C to stop)"
exec uv run --no-sync python -m server
