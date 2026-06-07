"""
Module 4 — Code Generator

Produces an applyable unified diff:
  1. LLM unified diff (fast path)
  2. On failure: retry with apply errors
  3. Fallback: LLM full-file edits → write to repo → `git diff --cached`
"""

import json
import re
from pathlib import Path
from typing import Optional

from modules.llm import LLMClient, extract_unified_diff
from modules.patch_utils import (
    analyze_patch,
    patch_applies,
    patch_includes_tests,
    patch_integrity_issues,
    patch_passes_integrity,
    planned_files_from_plan,
    planned_production_files,
)
from modules.git_patch import apply_edits_and_diff, parse_file_edits, reject_aider_markers
from modules.agent_logger import get_logger
from modules.repo_resolver import default_output_dir, effective_repo_path
from modules.convention import format_conventions_block, build_system_prompt
from modules.repo_hints import scope_guidance
from modules.fix_preflight import format_fix_state_block
from modules.retry_hints import build_retry_prompt


MINIMAL_RETRY = (
    "\n\nCRITICAL: Previous patch was too large or rewrote whole files. "
    "Make the SMALLEST possible change — a few lines in existing functions only. "
    "Do NOT replace entire files. Do NOT delete large sections. "
    "Edit surgically; keep all unrelated code identical."
)

MAX_FULL_FILE_LINES = 400
_FUNC_HEADER_RE = re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(")


class CodeGenerator:
    MAX_TOKENS = 32768
    MAX_ATTEMPTS = 4

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
        user_extra = build_retry_prompt(
            (error_output or "").split(";"),
            fix_state=issue.get("fix_state"),
            patch_excerpt=previous_patch[:8000],
            include_minimal=True,
        )
        user_extra = f"""

---

## Previous patch (failed validation)

```diff
{previous_patch[:12000]}
```

## Validation error output

```
{error_output[-8000:]}
```

{user_extra}

Fix the implementation so the resulting patch applies cleanly and passes validation.
"""
        return self._generate_patch(
            issue,
            context,
            plan,
            module="code_generator_retry",
            user_extra=user_extra,
            prefer_edit_mode=True,
        )

    def _generate_patch(
        self,
        issue: dict,
        context: dict,
        plan: str,
        module: str,
        user_extra: str = "",
        prefer_edit_mode: bool = False,
    ) -> str:
        log = get_logger()
        out_dir = default_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

        feedback = user_extra
        last_patch = ""
        last_diag: dict = {}
        issue_type = (issue.get("understanding") or {}).get("type", "")
        required_prod = planned_production_files(plan)
        fix_state = issue.get("fix_state") or {}
        if fix_state.get("test_only_ok"):
            required_prod = []

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            use_edit = prefer_edit_mode or attempt >= 3
            if attempt == 2 and last_diag and not patch_applies(last_diag):
                # After one failed diff, try edit mode if diff had apply/format errors
                use_edit = True

            mode = "edit+git diff" if use_edit else "unified diff"
            log.info(f"Patch generation attempt {attempt}/{self.MAX_ATTEMPTS} ({mode})")

            try:
                if use_edit:
                    raw, patch = self._generate_via_git_diff(
                        issue, context, plan, module, feedback, attempt
                    )
                else:
                    raw = self._llm_diff_raw(issue, context, plan, module, feedback, attempt)
                    patch = self._normalize_patch(extract_unified_diff(raw))
            except RuntimeError as e:
                log.warning(f"Attempt {attempt} error: {e}")
                feedback = str(e)
                continue

            raw_path = out_dir / f"{module}_raw.txt" if attempt == 1 else out_dir / f"{module}_attempt{attempt}_raw.txt"
            raw_path.write_text(raw, encoding="utf-8")

            diag = analyze_patch(patch, self.repo_path)
            last_patch, last_diag = patch, diag

            log.info(
                f"Patch stats: {diag['patch_lines']} lines, "
                f"{diag['hunk_count']} hunks, {diag['diff_file_count']} files"
            )
            if diag.get("issues"):
                log.warning(f"Patch diagnosis: {diag['issues'][:6]}")

            integrity = patch_integrity_issues(
                patch,
                issue_type=issue_type,
                repo_path=self.repo_path,
                required_files=required_prod,
                plan=plan,
            )
            if integrity:
                log.warning(f"Patch integrity: {integrity[:4]}")
                diag["integrity_issues"] = integrity
                feedback = self._format_retry_feedback(diag, patch, integrity, issue)
                log.warning(f"Attempt {attempt} failed integrity check — retrying")
                continue

            diag_path = out_dir / f"{module}_diagnosis.json"
            if attempt > 1:
                diag_path = out_dir / f"{module}_attempt{attempt}_diagnosis.json"
            diag_path.write_text(json.dumps({**diag, "attempt": attempt, "mode": mode}, indent=2), encoding="utf-8")

            if patch_applies(diag) and patch_passes_integrity(
                patch,
                issue_type=issue_type,
                repo_path=self.repo_path,
                required_files=required_prod,
                plan=plan,
            ):
                log.info("Patch passes apply + integrity checks")
                if not patch_includes_tests(patch):
                    log.warning("Patch has no *_test.go changes")
                return patch

            feedback = self._format_retry_feedback(diag, patch, integrity, issue)
            log.warning(f"Attempt {attempt} failed apply check — retrying")

        log.warning("All patch attempts failed apply check; returning last patch")
        return last_patch

    def _llm_diff_raw(
        self,
        issue: dict,
        context: dict,
        plan: str,
        module: str,
        user_extra: str,
        attempt: int = 1,
    ) -> str:
        log = get_logger()
        system = build_system_prompt("generate", include_retry=attempt > 1)
        user = self._build_diff_user_prompt(issue, context, plan) + user_extra
        raw = self.llm.complete(system=system, user=user, max_tokens=self.MAX_TOKENS, module=module)
        log.info(f"Raw LLM diff response ({len(raw)} chars)")
        return raw

    def _generate_via_git_diff(
        self,
        issue: dict,
        context: dict,
        plan: str,
        module: str,
        user_extra: str,
        attempt: int,
    ) -> tuple[str, str]:
        log = get_logger()
        system = build_system_prompt("generate_edit", include_retry=attempt > 1)
        user = self._build_edit_user_prompt(issue, context, plan) + user_extra
        raw = self.llm.complete(
            system=system,
            user=user,
            max_tokens=self.MAX_TOKENS,
            module=f"{module}_edit" if attempt == 1 else f"{module}_edit_{attempt}",
        )
        log.info(f"Raw LLM file-edit response ({len(raw)} chars)")

        if raw.strip().startswith("ERROR:"):
            raise RuntimeError(raw.strip())

        try:
            reject_aider_markers(raw)
            edits = parse_file_edits(raw)
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        if not edits:
            raise RuntimeError("No FILE: ... ```go blocks parsed from edit-mode response")

        _, _, blocked = self._read_repo_files(self._plan_file_paths(plan, context), plan, context)
        illegal = [p for p in edits if p in blocked]
        if illegal:
            raise RuntimeError(
                f"BLOCKED_LARGE_TEST_FILE: do not output FILE blocks for {illegal}; "
                "add tests in a new small *_test.go file instead"
            )

        log.info(f"Applying {len(edits)} file edit(s) and exporting git diff")
        patch, written = apply_edits_and_diff(self.repo_path, edits)
        if not patch.strip():
            raise RuntimeError(f"git diff empty after writing: {written}")

        log.info(f"git diff exported ({len(patch.splitlines())} lines) for: {written}")
        return raw, patch

    def _plan_file_paths(self, plan: str, context: dict) -> list[str]:
        paths = set(planned_files_from_plan(plan))
        if not paths:
            paths = set(re.findall(r"`([a-zA-Z0-9_./-]+\.go)`", plan))
        for f in context.get("candidate_files") or []:
            f = str(f)
            if f.endswith(".go") and not f.endswith("_test.go"):
                paths.add(f)
        return sorted(paths)

    def _read_repo_files(
        self, paths: list[str], plan: str, context: dict
    ) -> tuple[dict[str, str], dict[str, str], list[str]]:
        """Returns (full_file_contents, excerpt_only, do_not_output_paths)."""
        full: dict[str, str] = {}
        excerpts: dict[str, str] = {}
        blocked: list[str] = []
        for rel in paths:
            p = self.repo_path / rel
            if not p.is_file():
                continue
            content = p.read_text(encoding="utf-8", errors="replace")
            line_count = len(content.splitlines())
            if line_count > MAX_FULL_FILE_LINES and rel.endswith("_test.go"):
                excerpts[rel] = self._slice_go_file(content, rel, plan, context)
                blocked.append(rel)
            else:
                full[rel] = content
        return full, excerpts, blocked

    def _slice_go_file(
        self, content: str, rel: str, plan: str, context: dict
    ) -> str:
        lines = content.splitlines()
        needles: set[str] = set()
        for m in re.finditer(r"`(Test[A-Za-z0-9_]+)`", plan):
            needles.add(m.group(1))
        for term in context.get("grep_terms_used") or []:
            if str(term).startswith("Test"):
                needles.add(str(term))
        for fn in context.get("candidate_functions") or []:
            if fn.get("file") == rel:
                needles.add(fn["name"])

        # Package/imports header (until first func)
        header_end = 0
        for i, line in enumerate(lines):
            if _FUNC_HEADER_RE.match(line.strip()):
                header_end = i
                break
        header = lines[:header_end] if header_end else lines[: min(40, len(lines))]

        func_ranges: list[tuple[int, int, str]] = []
        i = header_end
        while i < len(lines):
            m = _FUNC_HEADER_RE.match(lines[i].strip())
            if m:
                name = m.group(1)
                start = i
                depth = 0
                end = i
                for j in range(i, min(i + 300, len(lines))):
                    depth += lines[j].count("{") - lines[j].count("}")
                    if depth <= 0 and j > i:
                        end = j
                        break
                else:
                    end = min(i + 80, len(lines) - 1)
                func_ranges.append((start, end, name))
                i = end + 1
            else:
                i += 1

        picked: list[str] = []
        for start, end, name in func_ranges:
            if needles and name not in needles and not any(n in name for n in needles):
                continue
            picked.extend(lines[start : end + 1])
            picked.append("")

        if not picked and func_ranges:
            # Default: last test function in file (common append site)
            start, end, _ = func_ranges[-1]
            picked = lines[start : end + 1]

        body = "\n".join(picked).strip()
        total = len(lines)
        return (
            "\n".join(header)
            + "\n\n"
            + body
            + f"\n\n// ... file `{rel}` truncated ({total} lines total — "
            "do NOT output a FILE block for this file; add new tests in a small new *_test.go file)\n"
        )

    def _conventions_for_issue(self, issue: dict, context: dict) -> str:
        return format_conventions_block(
            context.get("convention_snapshot", ""),
            extra_must_follow=scope_guidance(issue),
        )

    def _build_diff_user_prompt(self, issue: dict, context: dict, plan: str) -> str:
        fix_block = context.get("fix_state_block") or format_fix_state_block(
            issue.get("fix_state") or {}
        )
        return f"""## Issue
**{issue['title']}** ({issue['url']})

{issue['body']}

{fix_block}
## Fix plan
{plan}

{self._conventions_for_issue(issue, context)}

## Code context
{context['raw_context_str']}

Generate the unified diff now.

REQUIRED:
- Minimal surgical diff only — change the fewest lines possible.
- Include changes to at least one *_test.go file with tests that verify the fix.
- Use real context from the repo — do NOT use placeholder index hashes like 1111111.
- Every `diff --git` section MUST include an `index <hash>..<hash> 100644` line.
- Tests must be runnable via `go test` in the package you changed.
"""

    def _build_edit_user_prompt(self, issue: dict, context: dict, plan: str) -> str:
        paths = self._plan_file_paths(plan, context)
        full, excerpts, blocked = self._read_repo_files(paths, plan, context)
        files_block = ""
        for rel, content in full.items():
            cap = content if len(content.splitlines()) <= MAX_FULL_FILE_LINES else content[:16000]
            files_block += f"\n### Current `{rel}` (output full FILE block for this file)\n```go\n{cap}\n```\n"
        for rel, content in excerpts.items():
            files_block += (
                f"\n### Excerpt from `{rel}` (context only — do NOT output FILE block for this file)\n"
                f"```go\n{content[:12000]}\n```\n"
            )

        blocked_note = ""
        if blocked:
            blocked_note = (
                "\nDo NOT output FILE blocks for these large test files: "
                + ", ".join(f"`{p}`" for p in blocked)
                + ". Add new test cases in a new small `*_test.go` file instead.\n"
            )

        fix_block = context.get("fix_state_block") or format_fix_state_block(
            issue.get("fix_state") or {}
        )
        return f"""## Issue
**{issue['title']}** ({issue['url']})

{issue['body']}

{fix_block}
## Fix plan
{plan}

{self._conventions_for_issue(issue, context)}

## Files to change (from plan)
{', '.join(paths) or 'see plan'}
{blocked_note}
{files_block}

## Code context (sliced functions)
{context['raw_context_str'][:12000]}

Output FILE: path blocks for production files and any small test files listed above.
Use FILE: blocks only — never <<<<<<< SEARCH / >>>>>>> REPLACE markers.
Include at least one *_test.go file with tests that verify the fix.
Keep each file as close to the original as possible — minimal edits only.
"""

    def _format_retry_feedback(
        self, diag: dict, patch: str, integrity: list[str] | None = None, issue: dict | None = None
    ) -> str:
        issues = list(diag.get("issues") or []) + list(integrity or [])
        return build_retry_prompt(
            issues,
            fix_state=(issue or {}).get("fix_state"),
            stderr=diag.get("git_apply_stderr") or "",
            patch_excerpt=patch[:8000],
        )

    def _normalize_patch(self, patch: str) -> str:
        if patch.startswith("ERROR:"):
            raise RuntimeError(patch)

        if not patch.startswith("diff ") and not patch.startswith("--- "):
            m = re.search(r"(^diff --git.*)", patch, re.MULTILINE | re.DOTALL)
            if m:
                patch = patch[m.start() :]

        lines = [line.rstrip() for line in patch.splitlines()]
        return "\n".join(lines) + "\n"
