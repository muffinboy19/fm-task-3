"""
Project convention hints for plan and code generation.

Loads prompts/conventions.txt and combines with per-run signals from ContextBuilder.
"""

from modules.llm import load_prompt

_PROMPT_NAME = "conventions.txt"
_GENERIC_ONLY = "Standard Go conventions"


def load_conventions_prompt() -> str:
    return load_prompt(_PROMPT_NAME)


def format_conventions_block(
    convention_snapshot: str = "",
    extra_must_follow: list[str] | None = None,
) -> str:
    """Markdown block injected into plan/generate user prompts."""
    parts = [
        "## Project conventions (required)\n",
        load_conventions_prompt(),
    ]
    snapshot = (convention_snapshot or "").strip()
    if snapshot and snapshot != _GENERIC_ONLY:
        parts.append("\n### Detected in this repo\n")
        parts.append(snapshot)
    if extra_must_follow:
        parts.append("\n### Must follow\n")
        parts.append("\n".join(f"- {item}" for item in extra_must_follow if item))
    return "\n".join(parts)
