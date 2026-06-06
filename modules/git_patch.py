"""
Apply LLM file edits in a clean repo and export a real `git diff --cached` patch.
"""

import re
import subprocess
from pathlib import Path


def ensure_clean_repo(repo_path: Path) -> None:
    """Reset working tree to HEAD (does not fetch)."""
    repo_path = repo_path.resolve()
    if not (repo_path / ".git").exists():
        raise ValueError(f"Not a git repo: {repo_path}")
    subprocess.run(["git", "checkout", "--", "."], cwd=repo_path, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_path, check=False)


def parse_file_edits(text: str) -> dict[str, str]:
    """
    Parse LLM output into {repo-relative path: full file content}.

    Supported formats:
      FILE: path/to/file.go
      ```go
      ...
      ```

      ### path/to/file.go
      ```go
      ...
      ```
    """
    edits: dict[str, str] = {}

    for m in re.finditer(
        r"FILE:\s*([^\n`]+)\s*\n```(?:go|golang|text)?\s*\n(.*?)```",
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        path = _norm_path(m.group(1))
        if path:
            edits[path] = m.group(2)

    for m in re.finditer(
        r"###\s*([^\n`]+)\s*\n```(?:go|golang|text)?\s*\n(.*?)```",
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        path = _norm_path(m.group(1))
        if path and path not in edits:
            edits[path] = m.group(2)

    return edits


def _norm_path(raw: str) -> str:
    path = raw.strip().strip("'\"")
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            path = path[2:]
    return path.lstrip("./")


def write_edits(repo_path: Path, edits: dict[str, str]) -> list[str]:
    """Write edited files; return paths written."""
    repo_path = repo_path.resolve()
    written: list[str] = []
    for rel, content in edits.items():
        rel = _norm_path(rel)
        if not rel:
            continue
        dest = repo_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = content if content.endswith("\n") else content + "\n"
        dest.write_text(text, encoding="utf-8")
        written.append(rel)
    return written


def export_git_diff(repo_path: Path) -> str:
    """
    Stage all changes, capture `git diff --cached`, then revert working tree.
    Returns a unified diff with real index hashes and line numbers.
    """
    repo_path = repo_path.resolve()
    subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True, capture_output=True)
    r = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    patch = r.stdout or ""
    subprocess.run(["git", "reset", "HEAD"], cwd=repo_path, check=False, capture_output=True)
    subprocess.run(["git", "checkout", "--", "."], cwd=repo_path, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_path, check=False)
    if not patch.strip():
        return ""
    return patch if patch.endswith("\n") else patch + "\n"


def apply_edits_and_diff(repo_path: Path, edits: dict[str, str]) -> tuple[str, list[str]]:
    """Clean repo → write files → git diff → revert. Returns (patch, paths)."""
    ensure_clean_repo(repo_path)
    written = write_edits(repo_path, edits)
    if not written:
        ensure_clean_repo(repo_path)
        return "", []
    patch = export_git_diff(repo_path)
    return patch, written
