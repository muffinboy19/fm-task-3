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
)
from modules.git_patch import apply_edits_and_diff, parse_file_edits
from modules.agent_logger import get_logger
from modules.repo_resolver import default_output_dir, effective_repo_path
from modules.convention import format_conventions_block, build_system_prompt
from modules.repo_hints import scope_guidance


MINIMAL_RETRY = (
    "\n\nCRITICAL: Previous patch was too large or rewrote whole files. "
    "Make the SMALLEST possible change — a few lines in existing functions only. "
    "Do NOT replace entire files. Do NOT delete large sections. "
    "Edit surgically; keep all unrelated code identical."
)


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

Fix the implementation so the resulting patch applies cleanly and passes `go test`.
"""
        return self._generate_patch(
            issue,
            context,
            plan,
            module="code_generator_retry",
            user_extra=user_extra + MINIMAL_RETRY,
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
                patch, issue_type=issue_type, repo_path=self.repo_path
            )
            if integrity:
                log.warning(f"Patch integrity: {integrity[:4]}")
                diag["integrity_issues"] = integrity
                feedback = self._format_retry_feedback(diag, patch, integrity)
                log.warning(f"Attempt {attempt} failed integrity check — retrying")
                continue

            diag_path = out_dir / f"{module}_diagnosis.json"
            if attempt > 1:
                diag_path = out_dir / f"{module}_attempt{attempt}_diagnosis.json"
            diag_path.write_text(json.dumps({**diag, "attempt": attempt, "mode": mode}, indent=2), encoding="utf-8")

            if patch_applies(diag) and patch_passes_integrity(
                patch, issue_type=issue_type, repo_path=self.repo_path
            ):
                log.info("Patch passes apply + integrity checks")
                if not patch_includes_tests(patch):
                    log.warning("Patch has no *_test.go changes")
                return patch

            feedback = self._format_retry_feedback(diag, patch, integrity)
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

        edits = parse_file_edits(raw)
        if not edits:
            raise RuntimeError("No FILE: ... ```go blocks parsed from edit-mode response")

        log.info(f"Applying {len(edits)} file edit(s) and exporting git diff")
        patch, written = apply_edits_and_diff(self.repo_path, edits)
        if not patch.strip():
            raise RuntimeError(f"git diff empty after writing: {written}")

        log.info(f"git diff exported ({len(patch.splitlines())} lines) for: {written}")
        return raw, patch

    def _plan_file_paths(self, plan: str, context: dict) -> list[str]:
        paths = set(re.findall(r"`([a-zA-Z0-9_./-]+\.go)`", plan))
        for f in context.get("candidate_files") or []:
            if str(f).endswith(".go"):
                paths.add(str(f))
        return sorted(paths)

    def _read_repo_files(self, paths: list[str]) -> dict[str, str]:
        contents: dict[str, str] = {}
        for rel in paths:
            p = self.repo_path / rel
            if p.is_file():
                contents[rel] = p.read_text(encoding="utf-8", errors="replace")
        return contents

    def _conventions_for_issue(self, issue: dict, context: dict) -> str:
        return format_conventions_block(
            context.get("convention_snapshot", ""),
            extra_must_follow=scope_guidance(issue),
        )

    def _build_diff_user_prompt(self, issue: dict, context: dict, plan: str) -> str:
        return f"""## Issue
**{issue['title']}** ({issue['url']})

{issue['body']}

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
        current = self._read_repo_files(paths)
        files_block = ""
        for rel, content in current.items():
            files_block += f"\n### Current `{rel}`\n```go\n{content[:16000]}\n```\n"

        return f"""## Issue
**{issue['title']}** ({issue['url']})

{issue['body']}

## Fix plan
{plan}

{self._conventions_for_issue(issue, context)}

## Files to change (from plan)
{', '.join(paths) or 'see plan'}

{files_block}

## Code context (sliced functions)
{context['raw_context_str'][:12000]}

Output complete updated files using FILE: path blocks.
Include at least one *_test.go file with tests that verify the fix.
Keep each file as close to the original as possible — minimal edits only.
"""

    def _format_retry_feedback(
        self, diag: dict, patch: str, integrity: list[str] | None = None
    ) -> str:
        issues = list(diag.get("issues") or []) + list(integrity or [])
        stderr = diag.get("git_apply_stderr") or ""
        return f"""

---

## Previous attempt failed

Issues:
{chr(10).join(f'- {i}' for i in issues[:12])}

git apply stderr:
```
{stderr[:4000]}
```

Previous patch (truncated):
```diff
{patch[:8000]}
```

{MINIMAL_RETRY}

Use FILE: blocks with complete file contents so we can export a real git diff.
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
