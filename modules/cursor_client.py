"""
Cursor API client via official cursor-sdk (Agent.prompt).
"""

import os
import time
from pathlib import Path
from typing import Optional

from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

from modules.agent_logger import get_logger
from modules.config import get_env, get_cursor_api_key
from modules.repo_resolver import effective_repo_path


class CursorLLMClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_cursor_api_key()
        if not self.api_key:
            raise ValueError(
                "Cursor API key required. Set CURSOR_API_KEY in .env "
                "(or cursor_api_key)."
            )
        os.environ["CURSOR_API_KEY"] = self.api_key
        self.model = get_env("CURSOR_MODEL", "composer-2.5")
        self.repo_cwd = str(effective_repo_path())

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        module: str = "llm",
    ) -> str:
        log = get_logger()
        log.info(f"Cursor API model={self.model} cwd={self.repo_cwd}")

        message = f"""=== SYSTEM ===
{system}

=== USER ===
{user}

IMPORTANT: Follow the system instructions exactly. Reply in the requested format only.
Do not modify repository files unless explicitly asked to apply changes."""

        start = time.time()
        try:
            result = Agent.prompt(
                message,
                AgentOptions(
                    api_key=self.api_key,
                    model=self.model,
                    local=LocalAgentOptions(cwd=self.repo_cwd),
                ),
            )
        except Exception as e:
            raise RuntimeError(f"Cursor API call failed: {e}") from e

        elapsed = time.time() - start
        text = (result.result or "").strip()

        if result.status in ("error", "cancelled", "expired"):
            hint = ""
            if result.status == "error" and not text:
                hint = (
                    " (empty error — try CURSOR_MODEL=default in .env; "
                    f"composer-2.5 local runs often fail silently)"
                )
            raise RuntimeError(
                f"Cursor agent finished with status={result.status}: {text[:500]}{hint}"
            )
        if not text:
            raise RuntimeError("Empty response from Cursor API")

        log.llm_call(
            module=module,
            model=f"cursor/{self.model}",
            system_preview=system[:1500],
            user_preview=user[:3000],
            response=text,
            duration_sec=elapsed,
        )
        return text
