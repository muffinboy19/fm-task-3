"""
Resolve and clone the target GitHub repo from an issue URL into test_repo/.

Resolved paths are written to output/repo.json so .env does not need GITHUB_REPO_PATH.
"""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from modules.config import PROJECT_ROOT, get_env

_ISSUE_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")


def parse_issue_url(url: str) -> tuple[str, str, int]:
    m = _ISSUE_URL_RE.search(url)
    if not m:
        raise ValueError(f"Cannot parse GitHub issue URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def clone_directory_for_repo(owner: str, repo: str) -> Path:
    base = PROJECT_ROOT / get_env("TEST_REPO_DIR", "test_repo")
    return (base / repo).resolve()


def ensure_repo_clone(issue_url: str, log=None) -> Path:
    """Clone github.com/owner/repo into test_repo/<repo> if missing."""
    owner, repo, _ = parse_issue_url(issue_url)
    dest = clone_directory_for_repo(owner, repo)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if (dest / ".git").is_dir():
        if log:
            log.info(f"Using existing clone: {dest}")
        return dest

    clone_url = f"https://github.com/{owner}/{repo}.git"
    if log:
        log.info(f"Cloning {clone_url} -> {dest}")
    subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(dest)],
        check=True,
    )
    if log:
        log.kv("Cloned repo", str(dest))
    return dest


def default_output_dir() -> Path:
    d = Path(get_env("OUTPUT_DIR", "./output"))
    return d.resolve() if d.is_absolute() else (PROJECT_ROOT / d).resolve()


def save_repo_manifest(output_dir: Path, issue_url: str, repo_path: Path) -> Path:
    """Persist repo location for this issue (read by LLM clients without .env)."""
    owner, repo, issue_number = parse_issue_url(issue_url)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "repo.json"
    payload = {
        "issue_url": issue_url,
        "owner": owner,
        "repo": repo,
        "issue_number": issue_number,
        "clone_url": f"https://github.com/{owner}/{repo}.git",
        "path": str(repo_path.resolve()),
        "resolved_at": datetime.now().isoformat(),
    }
    manifest_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest_path


def load_repo_manifest(output_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    output_dir = output_dir or default_output_dir()
    manifest_path = output_dir / "repo.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def repo_path_from_manifest(
    output_dir: Optional[Path] = None,
    issue_url: Optional[str] = None,
) -> Optional[Path]:
    data = load_repo_manifest(output_dir)
    if not data or not data.get("path"):
        return None
    if issue_url and data.get("issue_url") != issue_url:
        return None
    path = Path(data["path"])
    return path if (path / ".git").is_dir() else None


def effective_repo_path(
    issue_url: Optional[str] = None,
    explicit: str | Path | None = None,
    output_dir: Optional[Path] = None,
    log=None,
) -> Path:
    """
    Repo directory for the current run:
    --repo override, then output/repo.json, else clone from issue URL.
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if (path / ".git").is_dir():
            if log:
                log.info(f"Using repo override: {path}")
            return path

    out = output_dir or default_output_dir()
    url = (issue_url or get_env("GITHUB_ISSUE_URL")).strip()
    cached = repo_path_from_manifest(out, issue_url=url or None)
    if cached:
        if log:
            log.info(f"Using saved repo path: {cached}")
        return cached

    if not url:
        raise ValueError(
            "No GitHub issue URL. Set GITHUB_ISSUE_URL in .env or pass --issue."
        )
    return resolve_repo_path(url, explicit=None, log=log)


def resolve_repo_path(
    issue_url: str,
    explicit: str | None = None,
    log=None,
) -> Path:
    """
    Pick repo directory: explicit path if it exists, else test_repo/<repo> (clone if needed).
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if (path / ".git").is_dir():
            if log:
                log.info(f"Using configured repo path: {path}")
            return path
        if log:
            log.warning(f"Repo path not found ({path}); cloning from issue URL")

    return ensure_repo_clone(issue_url, log=log)
