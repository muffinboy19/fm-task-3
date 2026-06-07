"""Reset cloned repos under test_repo/ to a clean git state."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from modules.agent_logger import AgentLogger


def reset_repo(repo_path: Path, log: Optional["AgentLogger"] = None, *, when: str = "") -> bool:
    """
    Discard working-tree edits and untracked files in a test clone.

    Used before a run (clean baseline) and after a run (undo patch gen / validation).
    """
    repo_path = Path(repo_path)
    if not (repo_path / ".git").exists():
        if log:
            log.warning(f"Not a git repo — skip reset: {repo_path}")
        return False

    label = f" ({when})" if when else ""
    if log:
        log.info(f"Resetting test_repo{label}: {repo_path}")
    else:
        print(f"Resetting test_repo{label}: {repo_path}")

    subprocess.run(["git", "checkout", "--", "."], cwd=repo_path, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_path, check=False)
    r = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    clean = not (r.stdout or "").strip()
    if log:
        log.kv("Repo clean after reset", clean)
    return clean


def should_reset_repo(no_reset: bool, stop_after: int) -> bool:
    """True when pipeline touches the clone (context, patch gen, validation)."""
    if no_reset:
        return False
    return not stop_after or stop_after > 1
