"""
Module 3 — Code Reasoning Agent

Uses the LLM once to produce a fix plan from issue + surgical context.
"""

from typing import Optional

from modules.llm import LLMClient, load_prompt
from modules.convention import format_conventions_block


class CodeReasoningAgent:
    def __init__(self, api_key: Optional[str] = None):
        self.llm = LLMClient(api_key=api_key)

    def plan(self, issue: dict, context: dict) -> str:
        system = load_prompt("plan.txt")
        user = self._build_user_prompt(issue, context)
        return self.llm.complete(
            system=system, user=user, max_tokens=2048, module="code_reasoning"
        )

    def _build_user_prompt(self, issue: dict, context: dict) -> str:
        return f"""## GitHub Issue

**URL:** {issue['url']}
**Title:** {issue['title']}
**Labels:** {', '.join(issue['labels']) or 'none'}

### Body
{issue['body']}

### Structured intake
- **Type:** {(issue.get('understanding') or {}).get('type', 'unknown')}
- **Symptom:** {(issue.get('understanding') or {}).get('symptom', issue['title'])}
- **Expected:** {(issue.get('understanding') or {}).get('expected', 'unknown')}
- **Actual:** {(issue.get('understanding') or {}).get('actual', 'unknown')}

### Curated grep terms (files in scope below)
{', '.join(issue.get('grep_terms') or issue['search_terms']) or 'none'}

---

{format_conventions_block(context.get("convention_snapshot", ""))}

---

## Repository context (surgical slice)

{context['raw_context_str']}

---

Produce the fix plan now.
"""
