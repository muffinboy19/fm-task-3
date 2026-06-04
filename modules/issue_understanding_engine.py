"""
Issue Understanding Engine — structured intake from raw issue (LLM + rules fallback).

Produces symptom / expected / actual / repro / anchors / type / open_questions.
Does NOT diagnose root cause in code (that is Step 3 with repo context).
"""

import json
import re
from typing import Any, Optional

from modules.agent_logger import get_logger
from modules.llm import LLMClient, load_prompt

_INTAKE_SCHEMA_KEYS = {
    "symptom", "expected", "actual", "repro", "anchors",
    "type", "open_questions", "confidence",
}
_VALID_TYPES = {"bug", "enhancement", "question", "unclear"}
_VALID_CONFIDENCE = {"high", "medium", "low"}


def _parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in LLM response")
    return json.loads(text[start : end + 1])


def _merge_anchor_lists(*lists: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for lst in lists:
        for item in lst or []:
            s = (item or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


class IssueUnderstandingEngine:
    def __init__(self, api_key: Optional[str] = None, use_llm: bool = True):
        self.api_key = api_key
        self.use_llm = use_llm
        self._llm: Optional[LLMClient] = None

    def understand(self, raw: dict) -> dict:
        log = get_logger()
        if self.use_llm:
            try:
                understanding = self._understand_with_llm(raw)
                understanding["source"] = "llm"
                log.info("Structured intake completed (LLM)")
            except Exception as e:
                log.warning(f"LLM intake failed, using rules fallback: {e}")
                understanding = self._understand_with_rules(raw)
                understanding["source"] = "rules_fallback"
        else:
            understanding = self._understand_with_rules(raw)
            understanding["source"] = "rules"

        understanding = self._normalize_understanding(understanding, raw)
        self._log_understanding(understanding)
        return understanding

    def _llm_client(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(api_key=self.api_key)
        return self._llm

    def _understand_with_llm(self, raw: dict) -> dict:
        system = load_prompt("issue_intake.txt")
        anchors = raw.get("anchors") or {}
        user = f"""## Issue

**URL:** {raw['url']}
**Title:** {raw['title']}
**Labels:** {', '.join(raw.get('labels') or []) or 'none'}
**State:** {raw.get('state', 'unknown')}

### Body
{raw.get('body') or '(empty)'}

### Comments ({len(raw.get('comments') or [])})
"""
        for i, c in enumerate(raw.get("comments") or [], 1):
            user += f"\n#### Comment {i}\n{c[:4000]}\n"

        user += f"""
### Pre-extracted anchors (keep and extend; do not drop)
{json.dumps(anchors, indent=2)}

### Code snippets from issue (if any)
"""
        snippets = raw.get("code_snippets") or []
        if snippets:
            for i, snip in enumerate(snippets[:3], 1):
                user += f"\n#### Snippet {i}\n```\n{snip[:1500]}\n```\n"
        else:
            user += "\n(none)\n"

        user += "\nReturn the JSON object now."
        raw_resp = self._llm_client().complete(
            system=system, user=user, max_tokens=1024, module="issue_understanding"
        )
        parsed = _parse_json_object(raw_resp)
        return parsed

    def _understand_with_rules(self, raw: dict) -> dict:
        labels = [lb.lower() for lb in (raw.get("labels") or [])]
        issue_type = "unclear"
        if any("bug" in lb for lb in labels):
            issue_type = "bug"
        elif any("enhancement" in lb or "feature" in lb for lb in labels):
            issue_type = "enhancement"
        elif any("question" in lb for lb in labels):
            issue_type = "question"

        body = raw.get("body") or ""
        has_repro = bool(
            re.search(
                r"(?i)(steps to reproduce|repro|reproduction|expected|actual)",
                body,
            )
        )
        confidence = "medium" if raw.get("title") else "low"
        if has_repro and body.strip():
            confidence = "medium"
        if has_repro and re.search(r"(?i)expected", body) and re.search(
            r"(?i)actual", body
        ):
            confidence = "high"

        anchors = raw.get("anchors") or {}
        open_questions = []
        if issue_type == "unclear":
            open_questions.append("Issue type not clear from labels or text")
        if not has_repro:
            open_questions.append("No explicit reproduction steps in issue body")

        return {
            "symptom": (raw.get("title") or "unknown").strip(),
            "expected": "unknown",
            "actual": "unknown",
            "repro": body[:2000] if has_repro else "unknown",
            "anchors": {
                "identifiers": anchors.get("identifiers", []),
                "paths": anchors.get("paths", []),
                "error_strings": anchors.get("error_strings", []),
                "linked_issues": anchors.get("linked_issues", []),
                "backtick_terms": anchors.get("backtick_terms", []),
            },
            "type": issue_type,
            "open_questions": open_questions,
            "confidence": confidence,
        }

    def _normalize_understanding(self, data: dict, raw: dict) -> dict:
        raw_anchors = raw.get("anchors") or {}
        llm_anchors = data.get("anchors") if isinstance(data.get("anchors"), dict) else {}

        anchors = {
            "identifiers": _merge_anchor_lists(
                raw_anchors.get("identifiers"),
                llm_anchors.get("identifiers"),
                raw.get("identifiers"),
            ),
            "paths": _merge_anchor_lists(
                raw_anchors.get("paths"), llm_anchors.get("paths")
            ),
            "error_strings": _merge_anchor_lists(
                raw_anchors.get("error_strings"),
                llm_anchors.get("error_strings"),
                raw.get("error_strings"),
            ),
            "linked_issues": _merge_anchor_lists(
                raw_anchors.get("linked_issues"),
                llm_anchors.get("linked_issues"),
            ),
            "backtick_terms": _merge_anchor_lists(
                raw_anchors.get("backtick_terms"),
                llm_anchors.get("backtick_terms"),
                raw.get("backtick_terms"),
            ),
            "stack_frames": raw_anchors.get("stack_frames") or [],
        }

        issue_type = (data.get("type") or "unclear").lower().strip()
        if issue_type not in _VALID_TYPES:
            issue_type = "unclear"

        confidence = (data.get("confidence") or "medium").lower().strip()
        if confidence not in _VALID_CONFIDENCE:
            confidence = "medium"

        open_q = data.get("open_questions")
        if not isinstance(open_q, list):
            open_q = [str(open_q)] if open_q else []

        def _str_field(key: str) -> str:
            v = data.get(key)
            if v is None or (isinstance(v, str) and not v.strip()):
                return "unknown"
            if isinstance(v, str):
                return v.strip()
            return str(v).strip()

        return {
            "symptom": _str_field("symptom"),
            "expected": _str_field("expected"),
            "actual": _str_field("actual"),
            "repro": _str_field("repro"),
            "anchors": anchors,
            "type": issue_type,
            "open_questions": [str(q).strip() for q in open_q if str(q).strip()],
            "confidence": confidence,
            "source": data.get("source", "unknown"),
        }

    def _log_understanding(self, u: dict) -> None:
        log = get_logger()
        log.kv("Intake source", u.get("source"))
        log.kv("Type", u.get("type"))
        log.kv("Confidence", u.get("confidence"))
        log.kv("Symptom", (u.get("symptom") or "")[:120])
        log.kv("Expected", (u.get("expected") or "unknown")[:80])
        log.kv("Actual", (u.get("actual") or "unknown")[:80])
        log.kv("Open questions", u.get("open_questions"))
        anchors = u.get("anchors") or {}
        log.kv("Anchor identifiers", anchors.get("identifiers"))
        log.kv("Anchor paths", anchors.get("paths"))
        log.block("Repro (intake)", (u.get("repro") or "unknown")[:2000])
