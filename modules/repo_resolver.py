"""
Resolve and clone the target GitHub repo from an issue URL into test_repo/.
"""

import re
import subprocess
from pathlib import Path

from modules.config import PROJECT_ROOT, get

_ISSUE_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")


def parse_issue_url(url: str) -> tuple[str, str, int]:
    m = _ISSUE_URL_RE.search(url)
    if not m:
        raise ValueError(f"Cannot parse GitHub issue URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def test_repo_dir(owner: str, repo: str) -> Path:
    base = PROJECT_ROOT / get("TEST_REPO_DIR", "test_repo")
    return (base / repo).resolve()


def ensure_repo_clone(issue_url: str, log=None) -> Path:
    """Clone github.com/owner/repo into test_repo/<repo> if missing."""
    owner, repo, _ = parse_issue_url(issue_url)
    dest = test_repo_dir(owner, repo)
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
