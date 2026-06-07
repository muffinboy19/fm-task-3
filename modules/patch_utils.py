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
    needs_prod = require_production_go and itype not in ("docs", "documentation")
    needs_tests = require_tests and itype in ("bug", "regression", "defect", "")

    if needs_prod and not prod and files:
        issues.append("MISSING_PRODUCTION_GO: patch must change at least one non-test .go file")
    if needs_tests and not tests and prod:
        issues.append("MISSING_TEST_FILE: bug fixes must include a *_test.go change")

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
) -> bool:
    return not patch_integrity_issues(
        patch, issue_type, require_production_go, require_tests, repo_path
    )
