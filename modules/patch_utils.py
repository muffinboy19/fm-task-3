"""
Diagnose why a unified diff fails `git apply --check`.
"""

import re
import subprocess
from pathlib import Path

# Placeholder hashes LLMs often invent (git apply may still fail on context).
# 0000000 is excluded — git uses it as the old blob for new files.
_FAKE_HASH_VALUES = frozenset(
    f"{d * 7}" for d in "123456789"
) | frozenset({"1111111", "2222222", "3333333", "4444444", "5555555", "6666666", "7777777", "8888888", "9999999"})


def _is_fake_index_hash(h: str) -> bool:
    h = h.strip().lower()
    if h == "0000000":
        return False
    if h in _FAKE_HASH_VALUES:
        return True
    if len(h) >= 7 and len(set(h)) == 1:
        return True
    return False


def _section_is_new_file(section: str) -> bool:
    return "--- /dev/null" in section or "new file mode" in section


def patch_format_issues(patch: str) -> list[str]:
    """Static format checks (no git invocation)."""
    issues: list[str] = []
    if not patch.strip():
        return ["EMPTY_PATCH"]

    if not patch.lstrip().startswith("diff --git"):
        issues.append("MISSING_DIFF_GIT_HEADER")

    if "```" in patch:
        issues.append("MARKDOWN_FENCE_LEAKED_INTO_PATCH")

    for m in re.finditer(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", patch, re.MULTILINE | re.I):
        old_h, new_h = m.group(1), m.group(2)
        if _is_fake_index_hash(new_h) or _is_fake_index_hash(old_h):
            issues.append(f"FAKE_INDEX_HASH: index {old_h}..{new_h}")

    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    for i, section in enumerate(sections):
        if not section.strip() or not section.lstrip().startswith("diff --git"):
            continue
        header_end = section.find("\n---")
        header = section if header_end == -1 else section[:header_end]
        if "index " not in header:
            issues.append(f"SECTION_{i}_MISSING_INDEX_LINE")

    if not patch.endswith("\n"):
        issues.append("MISSING_FINAL_NEWLINE")

    return issues


_HARD_BLOCK = frozenset({
    "EMPTY_PATCH",
    "MISSING_DIFF_GIT_HEADER",
    "MARKDOWN_FENCE_LEAKED_INTO_PATCH",
})


def patch_applies(diag: dict) -> bool:
    """True when the patch is safe to use (git apply --check is the source of truth)."""
    issues = diag.get("issues") or []
    for issue in issues:
        if issue in _HARD_BLOCK or issue.startswith("GIT_APPLY:"):
            return False

    if diag.get("git_apply_exit") == 0:
        return True

    for issue in issues:
        if issue.startswith("FAKE_INDEX") or "MISSING_INDEX" in issue:
            return False
        if issue == "MISSING_FINAL_NEWLINE":
            return False
        if "MISMATCH" in issue or "ENDS_ON_CHANGE_LINE" in issue:
            return False
    return False


def analyze_patch(patch: str, repo_path: Path) -> dict:
    """Return structured diagnosis (not token-related unless response is tiny)."""
    lines = patch.splitlines()
    diagnosis = {
        "patch_lines": len(lines),
        "patch_chars": len(patch),
        "ends_with_newline": patch.endswith("\n"),
        "diff_file_count": len(re.findall(r"^diff --git ", patch, re.MULTILINE)),
        "hunk_count": len(re.findall(r"^@@ ", patch, re.MULTILINE)),
        "issues": patch_format_issues(patch),
    }

    if "EMPTY_PATCH" in diagnosis["issues"]:
        return diagnosis

    if not diagnosis["issues"] or not any(
        i.startswith("MISSING_DIFF") for i in diagnosis["issues"]
    ):
        pass  # continue with hunk checks
    elif "MISSING_DIFF_GIT_HEADER" in diagnosis["issues"]:
        return diagnosis

    # Each hunk should end before next diff --git; check last lines of each section
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    for i, section in enumerate(sections):
        if not section.strip():
            continue
        is_new = _section_is_new_file(section)
        hunks = list(re.finditer(r"^@@ .+ @@", section, re.MULTILINE))
        for j, hunk_m in enumerate(hunks):
            start = hunk_m.end()
            end = hunks[j + 1].start() if j + 1 < len(hunks) else len(section)
            body = section[start:end].splitlines()
            plus = sum(1 for ln in body if ln.startswith("+") and not ln.startswith("+++"))
            minus = sum(1 for ln in body if ln.startswith("-") and not ln.startswith("---"))
            ctx = sum(1 for ln in body if ln.startswith(" "))
            hdr = hunk_m.group(0)
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", hdr)
            if m and not is_new:
                old_len = int(m.group(2) or 1)
                new_len = int(m.group(4) or 1)
                if minus != old_len:
                    diagnosis["issues"].append(
                        f"HUNK_{i}_{j}_OLD_COUNT_MISMATCH: header says -{old_len} lines, found {minus} '-' lines"
                    )
                new_side = plus + ctx
                if new_side != new_len and plus != new_len:
                    diagnosis["issues"].append(
                        f"HUNK_{i}_{j}_NEW_COUNT_MISMATCH: header says +{new_len} lines, "
                        f"found {new_side} on new side ({plus} '+', {ctx} ' ')"
                    )

        if is_new:
            continue
        last_line = section.strip().splitlines()[-1] if section.strip() else ""
        if last_line.startswith("+") or last_line.startswith("-"):
            diagnosis["issues"].append(
                f"SECTION_{i}_ENDS_ON_CHANGE_LINE: hunk likely truncated (last line: {last_line[:60]})"
            )

    # git apply --check (skip if already clearly invalid)
    format_blockers = {"EMPTY_PATCH", "MISSING_DIFF_GIT_HEADER", "MARKDOWN_FENCE_LEAKED_INTO_PATCH"}
    if format_blockers & set(diagnosis["issues"]):
        return diagnosis

    if not diagnosis["ends_with_newline"]:
        if "MISSING_FINAL_NEWLINE" not in diagnosis["issues"]:
            diagnosis["issues"].append("MISSING_FINAL_NEWLINE")

    # git apply --check
    if repo_path.exists() and (repo_path / ".git").exists():
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
            f.write(patch if patch.endswith("\n") else patch + "\n")
            patch_file = f.name
        try:
            r = subprocess.run(
                ["git", "apply", "--check", patch_file],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnosis["git_apply_exit"] = r.returncode
            diagnosis["git_apply_stderr"] = (r.stderr or r.stdout or "").strip()
            if r.returncode != 0:
                diagnosis["issues"].append(f"GIT_APPLY: {diagnosis['git_apply_stderr']}")
        finally:
            Path(patch_file).unlink(missing_ok=True)

    return diagnosis


def patch_files_changed(patch: str) -> list[str]:
    """Return repo-relative paths from diff --git lines."""
    return re.findall(r"^diff --git a/(\S+) b/\S+", patch, re.MULTILINE)


def patch_includes_tests(patch: str) -> bool:
    """True if patch touches at least one *_test.go file."""
    return any(p.endswith("_test.go") for p in patch_files_changed(patch))


def patch_touches_go(patch: str) -> bool:
    """True if patch modifies or adds any .go source file."""
    return any(p.endswith(".go") for p in patch_files_changed(patch))


def packages_to_test(patch: str) -> list[str]:
    """Go package paths to test based on changed files (e.g. ./render)."""
    pkgs = set()
    for path in patch_files_changed(patch):
        parent = str(Path(path).parent)
        pkgs.add("./" if parent == "." else f"./{parent}")
    return sorted(pkgs) or ["./..."]


MAX_PATCH_LINES = 800
MAX_SINGLE_FILE_LINES = 400
MAX_FILE_DELETION_RATIO = 0.35
MAX_FULL_FILE_LINES = 400


def planned_files_from_plan(plan: str) -> list[str]:
    """Go paths backtick-quoted in the plan's Files to change section (or whole plan)."""
    section = re.search(r"## Files to change\s*\n(.*?)(?:\n## |\Z)", plan, re.DOTALL | re.I)
    blob = section.group(1) if section else plan
    paths = re.findall(r"`([a-zA-Z0-9_./-]+\.go)`", blob)
    if not paths:
        paths = re.findall(r"`([a-zA-Z0-9_./-]+\.go)`", plan)
    seen: list[str] = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen


def planned_production_files(plan: str) -> list[str]:
    return [p for p in planned_files_from_plan(plan) if not p.endswith("_test.go")]


def planned_test_files(plan: str) -> list[str]:
    return [p for p in planned_files_from_plan(plan) if p.endswith("_test.go")]


def plan_allows_test_only(plan: str) -> bool:
    return bool(
        re.search(
            r"only (?:the )?regression test|only test additions|if so, only|already contain",
            plan,
            re.I,
        )
    )


def planned_prod_fix_redundant(plan: str, repo_path: Path | None) -> bool:
    """True when repo already has the planned production change (tests-only patch OK)."""
    if not repo_path or not plan:
        return False
    for path, syms in planned_symbols_by_file(plan).items():
        if path.endswith("_test.go") or "cronRegexString" not in syms:
            continue
        p = repo_path / path
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "/,#L-" in content or "[A-Za-z0-9*?][A-Za-z0-9*?/,#L-]" in content:
            return True
    return False


def planned_symbols_by_file(plan: str) -> dict[str, list[str]]:
    """Symbols the plan says to modify per file (from Files to change + Tests)."""
    by_file: dict[str, set[str]] = {}

    def add(path: str, sym: str) -> None:
        if path and sym and not sym.endswith(".go"):
            by_file.setdefault(path, set()).add(sym)

    section = re.search(r"## Files to change\s*\n(.*?)(?:\n## |\Z)", plan, re.DOTALL | re.I)
    for line in (section.group(1) if section else plan).splitlines():
        m = re.match(r"^-\s*`([^`]+\.go)`\s*—", line)
        if not m:
            continue
        path = m.group(1)
        tail = line.split("—", 1)[-1]
        for sym in re.findall(r"`(\w+)`", tail):
            add(path, sym)

    tests_sec = re.search(r"## Tests\s*\n(.*?)(?:\n## |\Z)", plan, re.DOTALL | re.I)
    if tests_sec:
        for line in tests_sec.group(1).splitlines():
            m = re.match(r"^-\s*`([^`]+\.go)`\s*—\s*`(Test\w+)`", line)
            if m:
                add(m.group(1), m.group(2))

    return {k: sorted(v) for k, v in by_file.items()}


def _patch_file_sections(patch: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = ""
    buf: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            if current:
                sections[current] = "\n".join(buf)
            parts = line.split()
            current = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            buf = [line]
        else:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf)
    return sections


def planned_test_files_required(plan: str, repo_path: Path | None = None) -> list[str]:
    """Planned test files we can require verbatim (skip huge files that use append/new file)."""
    required: list[str] = []
    for f in planned_test_files(plan):
        if repo_path:
            p = repo_path / f
            if p.is_file():
                try:
                    if len(p.read_text(encoding="utf-8", errors="replace").splitlines()) > MAX_FULL_FILE_LINES:
                        continue
                except OSError:
                    pass
        required.append(f)
    return required


def patch_planned_symbol_issues(patch: str, plan: str) -> list[str]:
    """Ensure +/- lines in a file mention symbols the plan assigned to that file."""
    issues: list[str] = []
    symbols = planned_symbols_by_file(plan)
    if not symbols:
        return issues
    sections = _patch_file_sections(patch)
    changed = set(patch_files_changed(patch))
    for path, syms in symbols.items():
        if path not in changed:
            continue
        body = sections.get(path, "")
        delta = "\n".join(
            ln[1:]
            for ln in body.splitlines()
            if (ln.startswith("+") or ln.startswith("-"))
            and not ln.startswith("+++")
            and not ln.startswith("---")
        )
        for sym in syms:
            if sym not in delta:
                issues.append(f"PLANNED_SYMBOL_NOT_TOUCHED: {path} must modify `{sym}`")
    return issues


def _file_change_stats(patch: str) -> dict[str, dict]:
    """Per-file added/deleted line counts from unified diff."""
    stats: dict[str, dict] = {}
    current_file = ""
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current_file = parts[3][2:]
                stats.setdefault(current_file, {"adds": 0, "dels": 0})
            continue
        if not current_file:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            stats[current_file]["adds"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            stats[current_file]["dels"] += 1
    return stats


def patch_integrity_issues(
    patch: str,
    issue_type: str = "",
    require_production_go: bool = True,
    require_tests: bool = True,
    repo_path: Path | None = None,
    required_files: list[str] | None = None,
    plan: str = "",
) -> list[str]:
    """Static gates before accepting a patch (size, destruction, required files)."""
    issues: list[str] = []
    if not patch.strip():
        return ["EMPTY_PATCH"]

    lines = patch.splitlines()
    if len(lines) > MAX_PATCH_LINES:
        issues.append(f"PATCH_TOO_LARGE: {len(lines)} lines (max {MAX_PATCH_LINES})")

    files = patch_files_changed(patch)
    prod = [f for f in files if f.endswith(".go") and not f.endswith("_test.go")]
    tests = [f for f in files if f.endswith("_test.go")]

    itype = (issue_type or "").lower()
    test_only = bool(
        plan and (plan_allows_test_only(plan) or planned_prod_fix_redundant(plan, repo_path))
    )
    needs_prod = require_production_go and itype not in ("docs", "documentation") and not test_only
    needs_tests = require_tests and itype in ("bug", "regression", "defect", "")

    if needs_prod and not prod and files:
        issues.append("MISSING_PRODUCTION_GO: patch must change at least one non-test .go file")
    if needs_tests and not tests:
        issues.append("MISSING_TEST_FILE: bug fixes must include a *_test.go change")

    changed = set(files)
    req_prod = [] if test_only else list(required_files or [])
    for req in req_prod:
        if req not in changed:
            issues.append(f"MISSING_PLANNED_FILE: {req}")

    for tf in planned_test_files_required(plan, repo_path) if plan else []:
        if tf not in changed:
            issues.append(f"MISSING_PLANNED_TEST_FILE: {tf}")

    # Skip symbol checks on files not in patch (e.g. test-only when prod already fixed)
    symbol_issues = patch_planned_symbol_issues(patch, plan) if plan else []
    if test_only:
        symbol_issues = [
            i for i in symbol_issues if not i.startswith("PLANNED_SYMBOL_NOT_TOUCHED: regexes.go")
        ]
    issues.extend(symbol_issues)

    for path, st in _file_change_stats(patch).items():
        total = st["adds"] + st["dels"]
        if total > MAX_SINGLE_FILE_LINES:
            issues.append(
                f"FILE_TOO_LARGE: {path} has {total} changed lines (max {MAX_SINGLE_FILE_LINES})"
            )
        if st["dels"] > 0 and st["adds"] == 0 and st["dels"] > 50:
            issues.append(f"FILE_MASS_DELETE: {path} deletes {st['dels']} lines with no additions")
        if repo_path and st["dels"] > 0:
            orig = repo_path / path
            if orig.is_file():
                try:
                    file_lines = len(orig.read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    file_lines = 0
                if file_lines > 0:
                    ratio = st["dels"] / file_lines
                    if ratio > MAX_FILE_DELETION_RATIO:
                        issues.append(
                            f"FILE_DELETION_RATIO: {path} deletes {ratio:.0%} "
                            f"(max {MAX_FILE_DELETION_RATIO:.0%})"
                        )

    return issues


def patch_passes_integrity(
    patch: str,
    issue_type: str = "",
    require_production_go: bool = True,
    require_tests: bool = True,
    repo_path: Path | None = None,
    required_files: list[str] | None = None,
    plan: str = "",
) -> bool:
    return not patch_integrity_issues(
        patch, issue_type, require_production_go, require_tests, repo_path, required_files, plan
    )
