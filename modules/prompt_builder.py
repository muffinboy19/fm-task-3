"""
Layered prompt assembly (context-engineering pattern).

Hierarchy (most persistent → most transient):
  1. Role          — who the model is for this stage
  2. Craft         — non-negotiable minimal-change rules (craft.txt)
  3. Contract      — output format for this stage (contracts/*.txt)
  4. Conventions   — repo style + detected snapshot (user message)
  5. Task context  — issue, plan, code slices, retry feedback (user message)

Inspired by: Aider (role + format split), SWE-agent (system/instance templates),
Addy Osmani context hierarchy, HumanLayer research→plan→implement compaction.
"""

from __future__ import annotations

from typing import Literal

from modules.llm import load_prompt

Stage = Literal["plan", "generate", "generate_edit"]

_ROLE_FILES: dict[Stage, str] = {
    "plan": "roles/plan.txt",
    "generate": "roles/generate.txt",
    "generate_edit": "roles/generate_edit.txt",
}

_CONTRACT_FILES: dict[Stage, str] = {
    "plan": "contracts/plan.txt",
    "generate": "contracts/diff.txt",
    "generate_edit": "contracts/edit.txt",
}


def load_craft_prompt() -> str:
    return load_prompt("craft.txt")


def load_conventions_prompt() -> str:
    return load_prompt("conventions.txt")


def build_system_prompt(stage: Stage, *, include_retry: bool = False) -> str:
    """System message: role + craft + stage output contract."""
    parts = [
        load_prompt(_ROLE_FILES[stage]).strip(),
        "## Master craft (non-negotiable)\n" + load_craft_prompt().strip(),
        load_prompt(_CONTRACT_FILES[stage]).strip(),
    ]
    if include_retry:
        parts.append(load_prompt("contracts/retry.txt").strip())
    return "\n\n".join(parts)


def format_context_block(
    convention_snapshot: str = "",
    extra_must_follow: list[str] | None = None,
) -> str:
    """
    User-message layer: project conventions + per-issue scope hints.
    Craft lives in system prompt; this is repo-specific style + dynamic routing.
    """
    parts = [
        "## Project conventions\n",
        load_conventions_prompt(),
    ]
    snapshot = (convention_snapshot or "").strip()
    if snapshot and snapshot != "Standard Go conventions":
        parts.append("\n### Detected in this repo\n")
        parts.append(snapshot)
    if extra_must_follow:
        parts.append("\n### Scope for this issue\n")
        parts.append("\n".join(f"- {item}" for item in extra_must_follow if item))
    return "\n".join(parts)


def format_conventions_block(
    convention_snapshot: str = "",
    extra_must_follow: list[str] | None = None,
) -> str:
    return format_context_block(convention_snapshot, extra_must_follow)
