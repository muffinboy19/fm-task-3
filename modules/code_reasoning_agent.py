"""
Module 3 — Code Reasoning Agent

Uses the LLM once to produce a fix plan from issue + surgical context.
"""

from typing import Optional

from modules.llm import LLMClient
from modules.convention import format_conventions_block, build_system_prompt
from modules.repo_hints import is_docs_issue, is_enhancement_issue, scope_guidance


class CodeReasoningAgent:
    def __init__(self, api_key: Optional[str] = None):
        self.llm = LLMClient(api_key=api_key)

    def plan(self, issue: dict, context: dict) -> str:
        system = build_system_prompt("plan")
        user = self._build_user_prompt(issue, context)
        return self.llm.complete(
            system=system, user=user, max_tokens=2048, module="code_reasoning"
        )

    def _build_user_prompt(self, issue: dict, context: dict) -> str:
        u = issue.get("understanding") or {}
        itype = (u.get("type") or "unknown").lower()
        scope_block = ""
        if is_docs_issue(issue):
            scope_block = """
### Scope override (docs issue)
Plan **documentation-only** changes (README, docs/*.md, comments if explicitly requested).
Do NOT plan production .go file changes unless the issue body explicitly requires code.
"""
        elif is_enhancement_issue(issue):
            scope_block = """
### Scope override (enhancement)
List only files explicitly named in the issue or anchors below.
Warn in Assumptions if file list is unclear — do not guess new packages.
"""

        return f"""## GitHub Issue

**URL:** {issue['url']}
**Title:** {issue['title']}
**Labels:** {', '.join(issue['labels']) or 'none'}

### Body
{issue['body']}

### Structured intake
- **Type:** {itype}
- **Symptom:** {u.get('symptom', issue['title'])}
- **Expected:** {u.get('expected', 'unknown')}
- **Actual:** {u.get('actual', 'unknown')}
{scope_block}
### Curated grep terms (files in scope below)
{', '.join(issue.get('grep_terms') or issue['search_terms']) or 'none'}

---

{format_conventions_block(context.get("convention_snapshot", ""), extra_must_follow=scope_guidance(issue))}

---

## Repository context (surgical slice)

{context['raw_context_str']}

---

Produce the fix plan now.
"""
