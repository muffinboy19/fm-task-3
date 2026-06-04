"""
LLM facade: Cursor API (default if key set) or Gemini with key rotation.
"""

import os
import re
from typing import Optional, Union

from modules.config import get_llm_provider

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")


def create_llm_client(api_key: Optional[str] = None) -> Union["CursorLLMClient", "GeminiLLMClient"]:
    provider = get_llm_provider()
    if provider == "cursor":
        from modules.cursor_client import CursorLLMClient
        return CursorLLMClient(api_key=api_key)
    from modules.gemini_client import GeminiLLMClient
    return GeminiLLMClient(api_key=api_key)


# Back-compat alias used by existing modules
class LLMClient:
    def __init__(self, api_key: Optional[str] = None):
        self._client = create_llm_client(api_key=api_key)

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        module: str = "llm",
    ) -> str:
        return self._client.complete(
            system=system, user=user, max_tokens=max_tokens, module=module
        )


def load_prompt(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract_fence(text: str, lang: str = "") -> str:
    # Prefer the largest ```diff block (avoid grabbing a tiny partial fence)
    if lang == "diff":
        blocks = re.findall(r"```diff\s*\n(.*?)```", text, re.DOTALL)
        if blocks:
            return max(blocks, key=len).strip()
    pattern = rf"```{lang}\s*\n(.*?)```"
    m = re.search(pattern, text, re.DOTALL)
    if m:
        return m.group(1).strip()
    pattern_any = r"```\s*\n(.*?)```"
    blocks = re.findall(pattern_any, text, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()
