"""
Module 6 — PR Writer

Generates a pull request title and body from issue, plan, patch, and test output.
"""

from typing import Optional

from modules.llm import LLMClient, load_prompt


class PRWriter:
    def __init__(self, api_key: Optional[str] = None):
        self.llm = LLMClient(api_key=api_key)

    def write(
        self,
        issue: dict,
        context: dict,
        plan: str,
        patch: str,
        test_output: str = "",
    ) -> str:
        system = load_prompt("pr.txt")
        files = list({f["file"] for f in context.get("candidate_functions", [])})
        user = f"""## Issue
**{issue['title']}** — {issue['url']}

{issue['body'][:2000]}

## Fix plan
{plan[:3000]}

## Files in scope
{', '.join(files) or 'see patch'}

## Patch (summary)
```diff
{patch[:6000]}
```

## Test output
```
{test_output[-4000:] if test_output else 'Not run (dry-run mode)'}
```

Write the PR description now.
"""
        return self.llm.complete(
            system=system, user=user, max_tokens=2048, module="pr_writer"
        )
