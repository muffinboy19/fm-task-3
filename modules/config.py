"""
Load configuration from .env and environment variables.
"""

import os
import sys
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


def project_python() -> str:
    """Prefer the project .venv so subprocesses see installed deps (e.g. cursor-sdk)."""
    for name in ("python", "python3"):
        candidate = PROJECT_ROOT / ".venv" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def project_venv_env() -> dict[str, str]:
    """Environment for subprocesses: inherit os.environ + activate .venv if present."""
    env = os.environ.copy()
    venv = PROJECT_ROOT / ".venv"
    if (venv / "bin" / "python").is_file() or (venv / "bin" / "python3").is_file():
        env["VIRTUAL_ENV"] = str(venv)
        bindir = str(venv / "bin")
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def ensure_llm_ready(provider: str | None = None) -> None:
    """Fail fast with a clear message if the chosen LLM backend is not importable."""
    provider = provider or get_llm_provider()
    if provider == "cursor":
        try:
            import cursor_sdk  # noqa: F401
        except ImportError as e:
            py = project_python()
            raise RuntimeError(
                "Cursor LLM requires cursor-sdk. From the project folder run:\n"
                f"  {py} -m pip install -r requirements.txt\n"
                f"Then restart with: {py} main.py"
            ) from e
    elif provider == "gemini":
        try:
            import google.generativeai  # noqa: F401
        except ImportError as e:
            py = project_python()
            raise RuntimeError(
                "Gemini LLM requires google-generativeai. Run:\n"
                f"  {py} -m pip install -r requirements.txt"
            ) from e
