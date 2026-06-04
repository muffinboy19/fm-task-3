"""
Load configuration from .env and environment variables.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def require(key: str) -> str:
    value = get(key)
    if not value:
        raise ValueError(
            f"Missing {key}. Add it to your .env file (see .env.example)."
        )
    return value


def get_gemini_api_keys() -> list[str]:
    keys = []
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY2", "GEMINI_API_KEY3"):
        k = get(name)
        if k and k not in keys:
            keys.append(k)
    extra = get("GEMINI_API_KEYS")
    if extra:
        for k in extra.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


def get_cursor_api_key() -> str:
    """CURSOR_API_KEY or cursor_api_key from .env."""
    return get("CURSOR_API_KEY") or get("cursor_api_key")


def get_llm_provider() -> str:
    """
    LLM_PROVIDER=cursor | gemini
    Default: cursor if CURSOR_API_KEY is set, else gemini.
    """
    explicit = get("LLM_PROVIDER").lower()
    if explicit in ("cursor", "gemini"):
        return explicit
    if get_cursor_api_key():
        return "cursor"
    if get_gemini_api_keys():
        return "gemini"
    return "gemini"
