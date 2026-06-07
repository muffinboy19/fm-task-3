"""
Repo-specific search hints so context discovery finds the right packages.
"""

from __future__ import annotations

import re
from pathlib import Path

# Slug → extra grep roots (relative to repo root) and optional path prefixes
REPO_HINTS: dict[str, dict] = {
    "golangci-lint": {
        "grep_dirs": ["pkg/golinters", "pkg/commands", "pkg/config"],
        "path_prefixes": ["pkg/golinters/", "pkg/commands/"],
    },
    "gin": {
        "grep_dirs": ["binding", "."],
        "path_prefixes": ["binding/", "render/"],
    },
    "cobra": {
        "grep_dirs": [".", "doc"],
        "path_prefixes": ["doc/"],
    },
    "validator": {
        "grep_dirs": ["."],
        "path_prefixes": [],
    },
}

_LINTER_ISSUE_RE = re.compile(
    r'(?:add|enable)\s+["\']?(\w+)["\']?\s+linter|linter:\s*(\w+)|"(\w+)"\s+linter',
    re.I,
)
_DOCS_ISSUE_RE = re.compile(
    r"\b(documentation|docs only|doc\.md|readme)\b", re.I
)


def repo_slug(owner: str, repo: str) -> str:
    return repo


def hint_config(owner: str, repo: str) -> dict:
    return REPO_HINTS.get(repo_slug(owner, repo), {})


def hint_grep_dirs(repo: Path, owner: str, repo_name: str) -> list[Path]:
    cfg = hint_config(owner, repo_name)
    dirs: list[Path] = []
    for rel in cfg.get("grep_dirs") or []:
        p = (repo / rel).resolve()
        if p.is_dir():
            dirs.append(p)
    return dirs


def linter_name_from_issue(issue: dict) -> str | None:
    blob = f"{issue.get('title', '')}\n{issue.get('body', '')}"
    m = _LINTER_ISSUE_RE.search(blob)
    if not m:
        return None
    return next(g for g in m.groups() if g)


def linter_search_paths(repo: Path, owner: str, repo_name: str, issue: dict) -> list[Path]:
    """For golangci-lint: pkg/golinters/<name> when issue mentions a linter."""
    if repo_name != "golangci-lint":
        return []
    name = linter_name_from_issue(issue)
    if not name:
        return []
    candidates = [
        repo / "pkg" / "golinters" / name,
        repo / "pkg" / "golinters" / f"{name}.go",
    ]
    return [p for p in candidates if p.exists()]


def is_docs_issue(issue: dict) -> bool:
    u = issue.get("understanding") or {}
    itype = (u.get("type") or "").lower()
    if itype in ("docs", "documentation"):
        return True
    title = issue.get("title") or ""
    labels = [lb.lower() for lb in issue.get("labels") or []]
    if "documentation" in labels:
        return True
    return bool(_DOCS_ISSUE_RE.search(title))


def is_enhancement_issue(issue: dict) -> bool:
    u = issue.get("understanding") or {}
    itype = (u.get("type") or "").lower()
    if itype in ("enhancement", "feature"):
        return True
    labels = [lb.lower() for lb in issue.get("labels") or []]
    return "enhancement" in labels or "feature" in labels


def reference_pr_paths(issue: dict) -> list[str]:
    """Paths from a reference PR diff (eval mode) or explicit issue field."""
    return list(issue.get("reference_pr_files") or [])


def scope_guidance(issue: dict) -> list[str]:
    """Extra convention bullets based on issue type."""
    lines: list[str] = []
    if is_docs_issue(issue):
        lines.append(
            "DOCS-ONLY: change documentation/markdown only (e.g. docs/, README). "
            "Do NOT modify production .go files unless the issue explicitly requires it."
        )
    if is_enhancement_issue(issue):
        lines.append(
            "ENHANCEMENT: stay within files explicitly named in the issue or anchors. "
            "Do not add large new subsystems; prefer extending existing patterns."
        )
    u = issue.get("understanding") or {}
    paths = (u.get("anchors") or {}).get("paths") or []
    if paths:
        lines.append(f"Prefer these paths from the issue: {', '.join(paths[:8])}")
    ref = reference_pr_paths(issue)
    if ref:
        lines.append(f"Reference PR touched: {', '.join(ref[:8])}")
    return lines
