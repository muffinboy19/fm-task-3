"""
Classify patch failures and return targeted retry prompts (verify-fix loop pattern).

References: Aider harness (alternate retries), SWE-agent max_requeries,
JoyCode failure attribution → targeted retry.
"""

from __future__ import annotations

import re


def classify_failure_issues(issues: list[str]) -> str:
    """Map integrity/apply issue codes to a failure category."""
    text = " ".join(issues).upper()
    if "MISSING_PRODUCTION_GO" in text or "MISSING_PLANNED_FILE" in text:
        return "missing_production"
    if "PLANNED_SYMBOL_NOT_TOUCHED" in text:
        return "symbol_not_touched"
    if "GIT_APPLY" in text or "PATCH FAILED" in text or "HUNK_" in text:
        return "apply_failed"
    if "PATCH_TOO_LARGE" in text or "FILE_TOO_LARGE" in text or "FILE_DELETION" in text:
        return "patch_too_large"
    if "MISSING_TEST" in text:
        return "missing_tests"
    if "EMPTY_PATCH" in text:
        return "empty_patch"
    return "generic"


_HINTS: dict[str, str] = {
    "missing_production": """
## Failure: missing production changes

Your patch only changed test files (or omitted a planned production file).

If the preflight block says **production fix already present**, this is EXPECTED:
- Do NOT edit production `.go` files — they are already fixed on this branch.
- Extend existing tests named in the plan (modify their function bodies).
- Add any new regression test functions the plan lists.

If preflight does NOT say already fixed:
- Edit the planned production file(s) with the smallest comma-ok / guard change.
- Include matching test updates in the same patch.
""",
    "symbol_not_touched": """
## Failure: planned symbol not modified

The plan assigns a specific function or test to change. You added code nearby but
did not edit the required symbol.

- Open the existing `Test...` or production function from the plan.
- Change lines INSIDE that function — not only add a new function below it.
- Output the full FILE block for that file with the function body updated.
""",
    "apply_failed": """
## Failure: patch does not apply

Use FILE: blocks with exact current file content from the repo context.
Do not invent line numbers or truncate hunks. Match whitespace and imports exactly.
""",
    "patch_too_large": """
## Failure: patch too large

Reduce to the minimum lines inside the failing function only.
Do NOT replace whole files. Do NOT delete unrelated code.
""",
    "missing_tests": """
## Failure: no test file changes

Bug fixes must include `*_test.go` changes that prove the fix (or lock regression).
""",
    "empty_patch": """
## Failure: empty patch

Output FILE blocks for at least one test file and any production file still needing edits.
""",
    "generic": """
## Failure: previous patch rejected

Read each issue line. Fix that specific problem with a smaller, more precise edit.
""",
}


def build_retry_prompt(
    issues: list[str],
    *,
    fix_state: dict | None = None,
    stderr: str = "",
    patch_excerpt: str = "",
    include_minimal: bool = True,
) -> str:
    category = classify_failure_issues(issues)
    parts = [_HINTS.get(category, _HINTS["generic"]).strip()]

    fs = fix_state or {}
    if fs.get("retry_hint"):
        parts.insert(0, fs["retry_hint"].strip())
    if fs.get("test_only_ok"):
        parts.append(
            "Remember: TEST-ONLY patch is correct here — skip production files listed as already fixed."
        )

    parts.append("\nIssues detected:")
    parts.extend(f"- {i}" for i in issues[:12])

    if stderr.strip():
        parts.append(f"\ngit apply stderr:\n```\n{stderr[:3000]}\n```")

    if patch_excerpt.strip():
        parts.append(f"\nPrevious patch (truncated):\n```diff\n{patch_excerpt[:6000]}\n```")

    if include_minimal and category in ("patch_too_large", "apply_failed", "generic"):
        parts.append(
            "\nMake the SMALLEST possible change — a few lines in existing functions only."
        )

    return "\n\n".join(parts)
