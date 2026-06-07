"""
Preflight: detect when a bug fix is already present on the checked-out branch.

SWE-bench / Aider-style agents assume a pinned pre-fix commit. When users run on
master after a merge, production files need no edits — only regression tests.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from modules.patch_utils import planned_production_files, planned_symbols_by_file


def _git_log_mentions_issue(repo_path: Path, issue_number: int) -> bool:
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "-50", f"--grep=#{issue_number}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _func_body(content: str, func_name: str) -> str:
    pat = re.compile(rf"^func\s+(?:\([^)]+\)\s+)?{re.escape(func_name)}\s*\(", re.M)
    m = pat.search(content)
    if not m:
        return ""
    start = m.start()
    depth = 0
    for i, line in enumerate(content[m.start() :].splitlines(), start=0):
        depth += line.count("{") - line.count("}")
        if depth <= 0 and i > 0:
            end = m.start() + sum(len(l) + 1 for l in content[m.start() :].splitlines()[: i + 1])
            return content[start:end]
    return content[start : start + 4000]


def _has_safe_hijack_pattern(content: str) -> bool:
    hijack = _func_body(content, "Hijack")
    if not hijack:
        return False
    return ", ok :=" in hijack and "ErrNotSupported" in hijack


def _has_safe_close_notify(content: str) -> bool:
    body = _func_body(content, "CloseNotify")
    if not body:
        return True  # no method — N/A
    if ", ok :=" in body:
        return True
    if ".(http.CloseNotifier)" in body and ", ok" not in body:
        return False
    return "CloseNotifier" not in body or ", ok :=" in body


def _has_cron_anchor_fix(content: str) -> bool:
    return "/,#L-" in content or "[A-Za-z0-9*?][A-Za-z0-9*?/,#L-]" in content


def _prod_file_already_fixed(path: Path, plan_blob: str) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "unreadable"

    name = path.name
    blob = plan_blob.lower()

    if "hijack" in blob or "closenotify" in blob or "errnotsupported" in blob:
        if name.endswith("response_writer.go") or "response_writer" in str(path):
            if _has_safe_hijack_pattern(content) and _has_safe_close_notify(content):
                return True, "safe Hijack/CloseNotify already present"

    if "cronregexstring" in blob or ("cron" in blob and "regex" in blob):
        if "regexes.go" in name and _has_cron_anchor_fix(content):
            return True, "cron regex anchor already present"

    # Plan explicitly says production change not needed
    if re.search(rf"`{re.escape(name)}`.*already (?:fixed|contains|has)", plan_blob, re.I):
        return True, "plan notes file already fixed"

    return False, ""


def assess_fix_state(
    repo_path: Path | None,
    plan: str = "",
    issue: dict | None = None,
) -> dict:
    """
    Returns:
      test_only_ok: production fix redundant — test-only patch is valid
      already_fixed_files: prod paths that need no edit
      warnings: user-facing messages
      retry_hint: inject into code generator on failure
    """
    issue = issue or {}
    repo_path = repo_path or Path(".")
    plan_blob = plan or ""
    warnings: list[str] = []
    already: list[str] = []
    reasons: list[str] = []

    for rel in planned_production_files(plan_blob) if plan_blob else []:
        fixed, why = _prod_file_already_fixed(repo_path / rel, plan_blob)
        if fixed:
            already.append(rel)
            reasons.append(f"{rel}: {why}")

    # Cron special-case (legacy)
    if planned_symbols_by_file(plan_blob):
        for path, syms in planned_symbols_by_file(plan_blob).items():
            if path.endswith("_test.go") or "cronRegexString" not in syms:
                continue
            fixed, why = _prod_file_already_fixed(repo_path / path, plan_blob)
            if fixed and path not in already:
                already.append(path)
                reasons.append(f"{path}: {why}")

    num = issue.get("number")
    state = (issue.get("state") or "").lower()
    if state == "closed" and num and repo_path.is_dir():
        if _git_log_mentions_issue(repo_path, int(num)):
            warnings.append(
                f"Issue #{num} is closed and recent git history mentions it — "
                "the fix may already be merged on this branch."
            )

    prod_planned = planned_production_files(plan_blob) if plan_blob else []
    test_only_ok = bool(prod_planned) and len(already) >= len(prod_planned)

    retry_hint = ""
    if test_only_ok:
        retry_hint = (
            "PRODUCTION FIX ALREADY IN REPO on this branch. "
            f"Do NOT edit: {', '.join(already)}. "
            "Output a TEST-ONLY patch: extend or add the regression tests from the plan. "
            "If the plan says to extend an existing test function, modify that function's body — "
            "do not only append a new test below it."
        )

    return {
        "test_only_ok": test_only_ok,
        "already_fixed_files": already,
        "already_fixed_reasons": reasons,
        "warnings": warnings,
        "retry_hint": retry_hint,
    }


def format_fix_state_block(fix_state: dict) -> str:
    if not fix_state:
        return ""
    lines = ["### Fix preflight (repo state)"]
    if fix_state.get("test_only_ok"):
        lines.append(
            "- **Production fix already present** on this checkout. "
            "Plan and patch should focus on **tests only** — do not re-edit production files."
        )
        for r in fix_state.get("already_fixed_reasons") or []:
            lines.append(f"  - {r}")
    for w in fix_state.get("warnings") or []:
        lines.append(f"- **Warning:** {w}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"
