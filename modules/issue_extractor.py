"""
Issue Extractor — fetch GitHub issue + deterministic anchors (no LLM).

Output is the raw issue bundle used by IssueUnderstandingEngine.
"""

import json
import os
import re
import urllib.request
from typing import Optional

from modules.agent_logger import get_logger

_IDENTIFIER_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]*(?:\.[A-Z][a-zA-Z0-9]*)*)\b")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_ERROR_STRING_RE = re.compile(
    r'(?:panic|error|Error|err)[:\s]+["\']([^"\']+)["\']', re.IGNORECASE
)
_GO_PATH_RE = re.compile(
    r"(?:^|[\s\"'(\[])([\w./-]+\.go)(?:[\s\"')\],:]|$)", re.MULTILINE
)
_LINKED_ISSUE_RE = re.compile(
    r"(?:#(\d+)|github\.com/[^/]+/[^/]+/issues/(\d+))"
)
_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)
_STACK_FRAME_RE = re.compile(
    r"^\s+([\w./-]+\.go:\d+)", re.MULTILINE
)


class IssueExtractor:
    def __init__(self, github_token: Optional[str] = None):
        self.token = github_token or os.environ.get("GITHUB_TOKEN", "")

    def extract(self, issue_url: str) -> dict:
        log = get_logger()
        log.debug(f"Extracting issue URL: {issue_url}")

        owner, repo, number = self._parse_url(issue_url)
        log.info(f"Fetching issue {owner}/{repo}#{number}")

        raw_issue = self._fetch_issue(owner, repo, number)
        comments = self._fetch_comments(owner, repo, number)
        log.info(f"Fetched issue + {len(comments)} comment(s)")

        comment_bodies = [
            c.get("body", "") or "" if isinstance(c, dict) else str(c)
            for c in comments
        ]
        full_text = self._combine_text(raw_issue, comment_bodies)

        identifiers = self._extract_identifiers(full_text)
        error_strings = self._extract_error_strings(full_text)
        backtick_terms = self._extract_backtick_terms(full_text)
        paths = self._extract_paths(full_text)
        linked_issues = self._extract_linked_issues(full_text, owner, repo, number)
        code_snippets = self._extract_code_fences(full_text)
        stack_frames = self._extract_stack_frames(full_text)

        anchors = {
            "identifiers": identifiers,
            "backtick_terms": backtick_terms,
            "error_strings": error_strings,
            "paths": paths,
            "linked_issues": linked_issues,
            "stack_frames": stack_frames,
        }
        search_terms = list(
            dict.fromkeys(
                identifiers
                + backtick_terms
                + error_strings
                + [p for p in paths if len(p) >= 3]
                + [f.split(":")[0] for f in stack_frames if ":" in f]
            )
        )

        result = {
            "url": issue_url,
            "owner": owner,
            "repo": repo,
            "number": number,
            "title": raw_issue.get("title", ""),
            "body": raw_issue.get("body", "") or "",
            "labels": [l["name"] for l in raw_issue.get("labels", [])],
            "state": raw_issue.get("state", ""),
            "comments": comment_bodies,
            "full_text": full_text,
            "anchors": anchors,
            "code_snippets": code_snippets,
            "search_terms": search_terms,
            # Back-compat fields (same keys downstream modules expect)
            "identifiers": identifiers,
            "error_strings": error_strings,
            "backtick_terms": backtick_terms,
        }

        log.kv("Title", result["title"])
        log.kv("Labels", result["labels"])
        log.kv("Identifiers", identifiers)
        log.kv("Paths", paths)
        log.kv("Error strings", error_strings)
        log.kv("Linked issues", linked_issues)
        log.kv("Search terms", search_terms)
        log.block("Issue body", result["body"] or "(empty)")

        return result

    def _parse_url(self, url: str):
        pattern = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")
        m = pattern.search(url)
        if not m:
            raise ValueError(f"Cannot parse GitHub issue URL: {url}")
        return m.group(1), m.group(2), int(m.group(3))

    def _fetch_issue(self, owner: str, repo: str, number: int) -> dict:
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
        return self._api_get(url)

    def _fetch_comments(self, owner: str, repo: str, number: int) -> list:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments"
        )
        try:
            return self._api_get(url)
        except Exception:
            return []

    def _api_get(self, url: str):
        get_logger().debug(f"GitHub API GET: {url}")
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "open-source-issue-solver/1.0")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def _combine_text(self, issue: dict, comments: list[str]) -> str:
        parts = [issue.get("title", ""), issue.get("body", "") or ""]
        parts.extend(comments)
        return "\n".join(parts)

    def _extract_identifiers(self, text: str) -> list[str]:
        found = _IDENTIFIER_RE.findall(text)
        stop = {
            "The", "This", "When", "If", "It", "In", "For", "Is", "Go",
            "Error", "Panic", "Note", "TODO", "Fix", "See", "Use",
            "New", "Get", "Set", "Has", "Add", "JSON", "HTTP", "API",
        }
        cleaned = [f for f in found if f not in stop and len(f) > 1]
        return list(dict.fromkeys(cleaned))

    def _extract_error_strings(self, text: str) -> list[str]:
        return list(dict.fromkeys(_ERROR_STRING_RE.findall(text)))

    def _extract_backtick_terms(self, text: str) -> list[str]:
        terms = _BACKTICK_RE.findall(text)
        result = []
        for t in terms:
            t = t.strip()
            if "\n" not in t and len(t) < 80:
                result.append(t)
        return list(dict.fromkeys(result))

    def _extract_paths(self, text: str) -> list[str]:
        found = _GO_PATH_RE.findall(text)
        return list(dict.fromkeys(p.strip() for p in found if p.strip()))

    def _extract_linked_issues(
        self, text: str, owner: str, repo: str, current: int
    ) -> list[str]:
        refs = []
        for m in _LINKED_ISSUE_RE.finditer(text):
            num = m.group(1) or m.group(2)
            if num and int(num) != current:
                refs.append(f"{owner}/{repo}#{num}")
        return list(dict.fromkeys(refs))

    def _extract_code_fences(self, text: str) -> list[str]:
        blocks = _CODE_FENCE_RE.findall(text)
        return [b.strip()[:2000] for b in blocks if b.strip()][:5]

    def _extract_stack_frames(self, text: str) -> list[str]:
        return list(dict.fromkeys(_STACK_FRAME_RE.findall(text)))
