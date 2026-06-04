"""
Module 4 — Code Generator

Produces a unified diff patch from issue + context + plan.
Supports regeneration when validation fails.
"""

import re
from pathlib import Path
from typing import Optional

from modules.llm import LLMClient, load_prompt, extract_fence
from modules.patch_utils import analyze_patch
from modules.agent_logger import get_logger
from modules.repo_resolver import default_output_dir, effective_repo_path
from modules.convention import format_conventions_block


class CodeGenerator:
    MAX_TOKENS = 32768

    def __init__(self, api_key: Optional[str] = None, repo_path: Optional[Path] = None):
        self.llm = LLMClient(api_key=api_key)
        self.repo_path = repo_path or self._resolve_repo()

    def _resolve_repo(self) -> Path:
        return effective_repo_path()

    def generate(self, issue: dict, context: dict, plan: str) -> str:
        return self._generate_patch(issue, context, plan, module="code_generator")

    def regenerate(
        self,
        issue: dict,
        context: dict,
        plan: str,
        previous_patch: str,
        error_output: str,
    ) -> str:
        user_extra = f"""

---

## Previous patch (failed validation)

```diff
{previous_patch}
```

## Validation error output

```
{error_output[-8000:]}
```

Fix the patch so it applies cleanly and passes `go test`.
CRITICAL: Complete every hunk with trailing context lines. Match @@ line counts exactly.
Output ONLY the corrected unified diff.
"""
        return self._generate_patch(
            issue, context, plan, module="code_generator_retry", user_extra=user_extra
        )

    def _generate_patch(
        self,
        issue: dict,
        context: dict,
        plan: str,
        module: str,
        user_extra: str = "",
    ) -> str:
        log = get_logger()
        system = load_prompt("generate.txt")
        user = self._build_user_prompt(issue, context, plan) + user_extra

        raw = self.llm.complete(
            system=system, user=user, max_tokens=self.MAX_TOKENS, module=module
        )

        out_dir = default_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = out_dir / f"{module}_raw.txt"
        raw_path.write_text(raw, encoding="utf-8")
        log.info(f"Raw LLM response saved ({len(raw)} chars) → {raw_path}")

        patch = self._normalize_patch(extract_fence(raw, "diff"))
        diag = analyze_patch(patch, self.repo_path)

        log.info(f"Patch stats: {diag['patch_lines']} lines, {diag['patch_chars']} chars")
        log.info(f"Hunks: {diag['hunk_count']}, files: {diag['diff_file_count']}")
        if diag.get("issues"):
            log.warning(f"Patch diagnosis: {diag['issues']}")
        if diag.get("git_apply_stderr"):
            log.warning(f"git apply preview: {diag['git_apply_stderr']}")

        from modules.patch_utils import patch_includes_tests
        if not patch_includes_tests(patch):
            log.warning("Generated patch has NO *_test.go changes — validation will likely fail")

        (out_dir / f"{module}_diagnosis.json").write_text(
            __import__("json").dumps(diag, indent=2), encoding="utf-8"
        )

        return patch

    def _build_user_prompt(self, issue: dict, context: dict, plan: str) -> str:
        return f"""## Issue
**{issue['title']}** ({issue['url']})

{issue['body']}

## Fix plan
{plan}

{format_conventions_block(context.get("convention_snapshot", ""))}

## Code context
{context['raw_context_str']}

Generate the unified diff now.

REQUIRED:
- Include changes to at least one *_test.go file with tests that verify the fix.
- Tests must be runnable via `go test` in the package you changed.

Each hunk MUST include 3 unchanged context lines after the last +/- line.
The @@ hunk header line counts MUST match the actual +/- lines in the hunk.
"""

    def _normalize_patch(self, patch: str) -> str:
        if patch.startswith("ERROR:"):
            raise RuntimeError(patch)

        if not patch.startswith("diff ") and not patch.startswith("--- "):
            m = re.search(r"(^diff --git.*)", patch, re.MULTILINE | re.DOTALL)
            if m:
                patch = patch[m.start() :]

        lines = [line.rstrip() for line in patch.splitlines()]
        return "\n".join(lines) + "\n"
