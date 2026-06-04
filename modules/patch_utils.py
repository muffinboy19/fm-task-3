"""
Diagnose why a unified diff fails `git apply --check`.
"""

import re
import subprocess
from pathlib import Path


def analyze_patch(patch: str, repo_path: Path) -> dict:
    """Return structured diagnosis (not token-related unless response is tiny)."""
    lines = patch.splitlines()
    diagnosis = {
        "patch_lines": len(lines),
        "patch_chars": len(patch),
        "ends_with_newline": patch.endswith("\n"),
        "diff_file_count": len(re.findall(r"^diff --git ", patch, re.MULTILINE)),
        "hunk_count": len(re.findall(r"^@@ ", patch, re.MULTILINE)),
        "issues": [],
    }

    if not patch.strip():
        diagnosis["issues"].append("EMPTY_PATCH")
        return diagnosis

    if not patch.lstrip().startswith("diff --git"):
        diagnosis["issues"].append("MISSING_DIFF_GIT_HEADER")

    # Unclosed markdown fence in raw storage
    if "```" in patch:
        diagnosis["issues"].append("MARKDOWN_FENCE_LEAKED_INTO_PATCH")

    # Each hunk should end before next diff --git; check last lines of each section
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    for i, section in enumerate(sections):
        if not section.strip():
            continue
        hunks = list(re.finditer(r"^@@ .+ @@", section, re.MULTILINE))
        for j, hunk_m in enumerate(hunks):
            start = hunk_m.end()
            end = hunks[j + 1].start() if j + 1 < len(hunks) else len(section)
            body = section[start:end].splitlines()
            plus = sum(1 for ln in body if ln.startswith("+") and not ln.startswith("+++"))
            minus = sum(1 for ln in body if ln.startswith("-") and not ln.startswith("---"))
            ctx = sum(1 for ln in body if ln.startswith(" "))
            # Parse @@ -old,old_len +new,new_len @@
            hdr = hunk_m.group(0)
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", hdr)
            if m:
                old_len = int(m.group(2) or 1)
                new_len = int(m.group(4) or 1)
                if minus != old_len:
                    diagnosis["issues"].append(
                        f"HUNK_{i}_{j}_OLD_COUNT_MISMATCH: header says -{old_len} lines, found {minus} '-' lines"
                    )
                if plus + ctx != new_len and plus != new_len:
                    # unified diff: new side = context + plus lines
                    new_side = plus + ctx
                    if new_side != new_len:
                        diagnosis["issues"].append(
                            f"HUNK_{i}_{j}_NEW_COUNT_MISMATCH: header says +{new_len} lines, "
                            f"found {new_side} on new side ({plus} '+', {ctx} ' ')"
                        )

        # Truncated section: ends with + line but no following context / next hunk closed
        last_line = section.strip().splitlines()[-1] if section.strip() else ""
        if last_line.startswith("+") or last_line.startswith("-"):
            diagnosis["issues"].append(
                f"SECTION_{i}_ENDS_ON_CHANGE_LINE: hunk likely truncated (last line: {last_line[:60]})"
            )

    if not diagnosis["ends_with_newline"]:
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


def packages_to_test(patch: str) -> list[str]:
    """Go package paths to test based on changed files (e.g. ./render)."""
    pkgs = set()
    for path in patch_files_changed(patch):
        parent = str(Path(path).parent)
        pkgs.add("./" if parent == "." else f"./{parent}")
    return sorted(pkgs) or ["./..."]
