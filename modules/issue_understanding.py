"""
Module 1 — Issue Understanding (facade)

  1. IssueExtractor     — GitHub fetch + deterministic anchors (raw)
  2. IssueUnderstandingEngine — structured intake (LLM JSON + rules fallback)

Returns merged issue dict with `raw` and `understanding` plus back-compat fields.
"""

from typing import Optional

from modules.agent_logger import get_logger
from modules.issue_extractor import IssueExtractor
from modules.issue_understanding_engine import IssueUnderstandingEngine
from modules.context_search import curated_grep_terms, curated_error_strings


class IssueUnderstanding:
    def __init__(
        self,
        github_token: Optional[str] = None,
        api_key: Optional[str] = None,
        use_llm: bool = True,
    ):
        self.extractor = IssueExtractor(github_token=github_token)
        self.engine = IssueUnderstandingEngine(api_key=api_key, use_llm=use_llm)

    def parse(self, issue_url: str) -> dict:
        log = get_logger()
        log.info("Phase 1a: extracting raw issue from GitHub...")
        raw = self.extractor.extract(issue_url)

        log.info("Phase 1b: structured intake (understanding engine)...")
        understanding = self.engine.understand(raw)

        grep_terms = curated_grep_terms({"understanding": understanding, "title": raw["title"]})
        error_strings = curated_error_strings(
            {"understanding": understanding, "error_strings": raw.get("error_strings")}
        )
        search_terms = list(dict.fromkeys(grep_terms + error_strings))

        merged = {
            **{k: raw[k] for k in (
                "url", "owner", "repo", "number", "title", "body",
                "labels", "comments", "full_text",
                "identifiers", "error_strings", "backtick_terms",
            )},
            "search_terms": search_terms,
            "grep_terms": grep_terms,
            "raw": raw,
            "understanding": understanding,
        }

        log.artifact("Issue raw bundle", "see issue_raw.json")
        log.kv("Curated grep terms", grep_terms)
        log.artifact("Issue understanding", "see issue_understanding.json")
        return merged
