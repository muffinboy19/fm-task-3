"""
Load configuration from .env and environment variables.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def require_env(key: str) -> str:
    value = get_env(key)
    if not value:
        raise ValueError(
            f"Missing {key}. Add it to your .env file (see .env.example)."
        )
    return value


def get_gemini_api_keys() -> list[str]:
    keys = []
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY2", "GEMINI_API_KEY3"):
        k = get_env(name)
        if k and k not in keys:
            keys.append(k)
    extra = get_env("GEMINI_API_KEYS")
    if extra:
        for k in extra.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


def get_cursor_api_key() -> str:
    return get_env("CURSOR_API_KEY") or get_env("cursor_api_key")


def get_llm_provider() -> str:
    explicit = get_env("LLM_PROVIDER").lower()
    if explicit in ("cursor", "gemini"):
        return explicit
    if get_cursor_api_key():
        return "cursor"
    if get_gemini_api_keys():
        return "gemini"
    return "gemini"
