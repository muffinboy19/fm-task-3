"""
Guardrails for picking GitHub issues safe for automated fixing.

Small/medium localized bugs only — no security, rewrites, or vague debates.
"""

import json
import re
from pathlib import Path
from typing import Optional

from modules.config import PROJECT_ROOT

_AVOID_TITLE = re.compile(
    r"(security|cve|advisory|refactor|rewrite|breaking|architecture|"
    r"proposal|design discussion|dependency upgrade|false positive|"
    r"cgo|migration|RFC discussion|help wanted.{0,20}investigation)",
    re.I,
)
_PREFER = re.compile(
    r"(repro|steps to reproduce|expected|actual|failing test|"
    r"should not|incorrect|invalid|panic|round.?trip|validation)",
    re.I,
)


def load_candidates(path: Optional[Path] = None) -> dict:
    p = path or (PROJECT_ROOT / "issues" / "candidates.json")
    return json.loads(p.read_text(encoding="utf-8"))


def score_issue(title: str, body: str, labels: list[str]) -> dict:
    """Higher score = better fit for the agent."""
    text = f"{title}\n{body}"
    label_names = [lb.lower() for lb in labels]
    score = 0
    reasons: list[str] = []

    if "bug" in label_names:
        score += 2
        reasons.append("+bug label")
    if _PREFER.search(text):
        score += 2
        reasons.append("+repro/expected language")
    if "```" in body:
        score += 1
        reasons.append("+code block")
    if len(body) > 200:
        score += 1
        reasons.append("+substantial body")

    if _AVOID_TITLE.search(title) or _AVOID_TITLE.search(body[:500]):
        score -= 10
        reasons.append("-avoid keyword")
    if "type/proposal" in label_names or "enhancement" in label_names:
        if "bug" not in label_names:
            score -= 5
            reasons.append("-proposal/enhancement only")
    if "dependencies" in label_names:
        score -= 4
        reasons.append("-dependencies")
    if "investigation" in label_names:
        score -= 3
        reasons.append("-investigation")

    return {
        "score": score,
        "recommended": score >= 3,
        "reasons": reasons,
    }


def pick_next_candidate(candidates: Optional[dict] = None) -> Optional[dict]:
    data = candidates or load_candidates()
    for item in data.get("recommended", []):
        if item.get("status") in ("next_run", "candidate"):
            return item
    return None
