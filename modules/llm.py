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


def extract_unified_diff(text: str) -> str:
    """
    Extract a unified diff from an LLM response.

    Models often close ```diff early; prefer the longest valid diff segment.
    """
    text = text.strip()
    total_diff_files = len(re.findall(r"^diff --git ", text, re.MULTILINE))
    blocks = re.findall(r"```diff\s*\n(.*?)```", text, re.DOTALL)
    diff_blocks = [b.strip() for b in blocks if "diff --git" in b]
    if diff_blocks:
        best = max(diff_blocks, key=lambda b: (b.count("diff --git"), len(b)))
        # Model closed ``` early but continued the patch outside the fence
        if best.count("diff --git") >= total_diff_files and total_diff_files >= 1:
            return _normalize_diff_text(best)

    # Fence closed early: take everything from first diff --git
    m = re.search(r"^diff --git ", text, re.MULTILINE)
    if m:
        chunk = text[m.start() :]
        chunk = re.sub(r"\n```\s*$", "", chunk.strip())
        return _normalize_diff_text(chunk)

    # Last resort: strip optional outer fence markers
    stripped = re.sub(r"^```(?:diff)?\s*\n", "", text)
    stripped = re.sub(r"\n```\s*$", "", stripped.strip())
    return _normalize_diff_text(stripped)


def _normalize_diff_text(patch: str) -> str:
    lines = [line.rstrip() for line in patch.splitlines()]
    return "\n".join(lines) + "\n" if lines else ""


def extract_fence(text: str, lang: str = "") -> str:
    if lang == "diff":
        return extract_unified_diff(text)
    # Prefer the largest fenced block for other languages
    if lang == "diff_legacy":
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
