"""Central configuration. Values come from environment variables or the .env file."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

try:  # python-dotenv is optional at import time
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE)
except Exception:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


# --- Paths -----------------------------------------------------------------
WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).expanduser()
DECK_DIR = DATA_DIR / "decks"

# --- Server ----------------------------------------------------------------
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _int("PORT", 8765)
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 300)


# --- AI provider -----------------------------------------------------------
# LLM_PROVIDER: auto | free | anthropic | openai | ollama | none
#   free = Groq, then Gemini, then OpenRouter, then Ollama (see FREE_CHAIN)
def llm_settings() -> dict:
    """Read fresh each time so the in-app Settings panel can change them."""
    return {
        "provider": os.getenv("LLM_PROVIDER", "auto").strip().lower(),
        "anthropic_model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "openai_model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        "openai_base_url": os.getenv("OPENAI_BASE_URL") or None,
        "ollama_model": os.getenv("OLLAMA_MODEL", "qwen3.5:9b"),
        "ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
        "ollama_num_ctx": _int("OLLAMA_NUM_CTX", 16384),
        # free chain
        "free_chain": tuple(x.strip().lower() for x in os.getenv("FREE_CHAIN", "groq,gemini,openrouter,ollama")
                            .split(",") if x.strip()),
        "groq_model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        "openrouter_model": os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        # how much document text each free service is sent per request (characters)
        "groq_input_chars": _int("GROQ_INPUT_CHARS", 12000),      # Groq's free plan allows ~8,000 tokens/minute
        "gemini_input_chars": _int("GEMINI_INPUT_CHARS", 160000),
        "openrouter_input_chars": _int("OPENROUTER_INPUT_CHARS", 60000),
    }


FREE_WAIT_SECONDS = _int("FREE_WAIT_SECONDS", 12)  # wait out short per-minute limits instead of switching
FREE_TIMEOUT = _int("FREE_TIMEOUT", 120)            # seconds before giving up on a free service


# --- Lesson generation -----------------------------------------------------
MAX_DEPTH = _int("MAX_DEPTH", 4)               # levels you can drill below the overview
LANGUAGE = os.getenv("EXPLAIN_LANGUAGE", "English")
PREFETCH_CHILDREN = _int("PREFETCH_CHILDREN", 0)  # pre-generate N child slides in background
PASSAGE_SPLIT_CHARS = _int("PASSAGE_SPLIT_CHARS", 4000)  # split long sections into finer passages
FOCUS_CHARS = _int("FOCUS_CHARS", 60000)       # max source text sent for one slide
VISION_PAGE_LIMIT = _int("VISION_PAGE_LIMIT", 40)  # scanned pages / images read with AI vision
READ_REPO_MAX_FILES = _int("READ_REPO_MAX_FILES", 400)
READ_MAX_CHARS = _int("READ_MAX_CHARS", 3_000_000)
OPEN_BROWSER = _bool("OPEN_BROWSER", True)
