import argparse
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from modules.config import get_env, get_gemini_api_keys, get_cursor_api_key, get_llm_provider, ensure_llm_ready
from modules.agent_logger import init_logger, get_logger
from modules.issue_understanding import IssueUnderstanding
from modules.context_builder import ContextBuilder
from modules.code_reasoning_agent import CodeReasoningAgent
from modules.code_generator import CodeGenerator
from modules.plan_adherence_checker import PlanAdherenceChecker
from modules.validator import Validator
from modules.pr_writer import PRWriter
from modules.fix_preflight import assess_fix_state, format_fix_state_block
from modules.retry_hints import build_retry_prompt
from modules.convention import format_conventions_block, load_conventions_prompt
from modules.issue_guardrails import load_candidates, pick_next_candidate
from modules.live_state import clear_stale_run_outputs
from modules.repo_resolver import resolve_repo_path, save_repo_manifest
from modules.repo_reset import reset_repo, should_reset_repo


def parse_args():
    parser = argparse.ArgumentParser(
        description="Open Source Issue Solver for Go repositories"
    )
    parser.add_argument("--issue", default=None, help="GitHub issue URL")
    parser.add_argument("--repo", default=None, help="Local cloned Go repo path")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--github-token", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Do not reset test_repo clone before or after the run",
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=0,
        metavar="N",
        help="Exit after pipeline step N (1=issue understanding only)",
    )
    parser.add_argument(
        "--no-llm-intake",
        action="store_true",
        help="Skip LLM structured intake; use rules-only understanding",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Do not start the live HTML dashboard",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=int(get_env("DASHBOARD_PORT", "8765") or "8765"),
        help="Port for live dashboard when --ui is set",
    )
    parser.add_argument(
        "--validation-full",
        action="store_true",
        help="Run go test ./... instead of package-scoped tests",
    )
    parser.add_argument(
        "--list-candidates",
        action="store_true",
        help="Print screened issues from issues/candidates.json and exit",
    )
    return parser.parse_args()


def build_fallback_pr_summary(issue: dict, plan: str, result: dict) -> str:
    status = "passed" if result.get("passed") else "failed or skipped"
    return f"""# Fix: {issue['title']}

## Summary
Automated fix for {issue['url']}. Validation: **{status}**.

## Plan
{plan[:2000]}

## Test output
```
{(result.get('test_output') or 'n/a')[:3000]}
```

## Closes
{issue['url']}
"""


def build_draft_pr_summary(issue: dict, plan: str, result: dict) -> str:
    err = result.get("error") or "Go validation failed"
    return f"""# [DRAFT — VALIDATION FAILED] {issue['title']}

> **Do not open this PR.** The generated patch did not pass `git apply` and/or `go test`.

## Issue
{issue['url']}

## Validation error
```
{err[:2000]}
```

## Test output
```
{(result.get('test_output') or 'n/a')[:3000]}
```

## Plan (for reference)
{plan[:1500]}

## Next steps
- Re-run the agent or fix the patch manually before submitting.
"""


def parse_env_bool(key: str, default: bool = False) -> bool:
    val = get_env(key, "false" if not default else "true").lower()
    return val in ("1", "true", "yes", "on")


def main():
    args = parse_args()

    if args.list_candidates:
        data = load_candidates()
        print("Guardrails:", ", ".join(data["guardrails"]["avoid"][:4]), "...")
        print("\nRecommended:")
        for item in data.get("recommended", []):
            print(f"  [{item.get('size')}] {item['repo']} — {item['title']}")
            print(f"       {item['url']}  ({item.get('status')})")
        nxt = pick_next_candidate(data)
        if nxt:
            print(f"\nNext to run: {nxt['url']}")
        print("\nAvoid examples:")
        for item in data.get("avoid_examples", [])[:5]:
            print(f"  {item['repo']}: {item['reason']}")
        return

    if args.issue is None:
        from modules.dashboard_server import run_serve_mode

        try:
            ensure_llm_ready()
        except RuntimeError as e:
            print(f"LLM setup error:\n{e}")
            sys.exit(1)
        run_serve_mode(port=args.ui_port, open_browser=not args.no_ui)
        return

    issue_url = args.issue
    explicit_repo = args.repo or None
    os.environ["GITHUB_ISSUE_URL"] = args.issue
    output_dir = Path(args.output or get_env("OUTPUT_DIR", "./output")).resolve()
    log_dir = Path(args.log_dir or get_env("LOG_DIR", "./logs")).resolve()
    os.environ["OUTPUT_DIR"] = str(output_dir)
    os.environ["LOG_DIR"] = str(log_dir)
    api_key = args.api_key or None
    github_token = args.github_token or get_env("GITHUB_TOKEN") or None
    dry_run = args.dry_run or parse_env_bool("DRY_RUN")

    output_dir.mkdir(parents=True, exist_ok=True)
    clear_stale_run_outputs(output_dir)

    log = init_logger(log_dir, output_dir=output_dir)

    ui_enabled = not args.no_ui and parse_env_bool("DASHBOARD_UI", default=True)
    if ui_enabled:
        from modules.dashboard_server import start_dashboard_server

        start_dashboard_server(port=args.ui_port, log_dir=log_dir, output_dir=output_dir)
        dashboard_url = f"http://127.0.0.1:{args.ui_port}/"
        log.html_dashboard_url = dashboard_url
        log.artifact("Live dashboard", dashboard_url)
        print(f"Dashboard: {dashboard_url}")
        webbrowser.open(dashboard_url, new=2)

    log.section("Open Source Issue Solver started")
    log.kv("Issue URL", issue_url)
    log.kv("Output dir", str(output_dir))
    log.kv("LLM provider", get_llm_provider())
    log.kv("Dry run", dry_run)

    provider = get_llm_provider()
    needs_llm = not args.no_llm_intake or not (args.stop_after and args.stop_after <= 1)
    if needs_llm:
        try:
            ensure_llm_ready(provider)
        except RuntimeError as e:
            log.error(str(e))
            sys.exit(1)
        if provider == "cursor" and not api_key and not get_cursor_api_key():
            log.error("Missing CURSOR_API_KEY")
            sys.exit(1)
        if provider == "gemini" and not api_key and not get_gemini_api_keys():
            log.error("Missing GEMINI_API_KEY")
            sys.exit(1)

    needs_repo = not (args.stop_after and args.stop_after <= 1)
    if needs_repo:
        try:
            repo_path = resolve_repo_path(issue_url, explicit=explicit_repo, log=log)
            manifest_path = save_repo_manifest(output_dir, issue_url, repo_path)
        except Exception as e:
            log.error(f"Failed to resolve/clone repo: {e}")
            sys.exit(1)
        log.kv("Repo path", str(repo_path))
        log.artifact("Repo manifest", str(manifest_path))
    else:
        repo_path = None

    reset_after_run = should_reset_repo(args.no_reset, args.stop_after)

    if repo_path and reset_after_run:
        reset_repo(repo_path, log, when="before run")

    try:
        _run_issue_pipeline(
            args=args,
            issue_url=issue_url,
            output_dir=output_dir,
            log=log,
            repo_path=repo_path,
            api_key=api_key,
            github_token=github_token,
            dry_run=dry_run,
        )
    finally:
        if repo_path and reset_after_run:
            reset_repo(repo_path, log, when="after run")


def _run_issue_pipeline(
    *,
    args,
    issue_url: str,
    output_dir: Path,
    log,
    repo_path: Path | None,
    api_key,
    github_token,
    dry_run: bool,
) -> None:
    log.step_start("1", "Extract raw issue + structured intake...")
    try:
        issue = IssueUnderstanding(
            github_token=github_token,
            api_key=api_key,
            use_llm=not args.no_llm_intake,
        ).parse(issue_url)
        raw_path = output_dir / "issue_raw.json"
        understanding_path = output_dir / "issue_understanding.json"
        raw_path.write_text(
            json.dumps(issue["raw"], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        understanding_path.write_text(
            json.dumps(issue["understanding"], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        u = issue["understanding"]
        ref_files_path = output_dir / "reference_pr_files.json"
        if ref_files_path.exists():
            issue["reference_pr_files"] = json.loads(ref_files_path.read_text(encoding="utf-8"))
        issue_type = (u.get("type") or "").lower()
        log.step_ok(
            "1",
            f"{issue['title'][:50]}... | {u.get('type')} | confidence={u.get('confidence')}",
        )
        log.artifact("Issue URL", issue_url)
        log.artifact("Raw issue JSON", str(raw_path))
        log.artifact("Understanding JSON", str(understanding_path))
    except Exception as e:
        log.step_fail("1", str(e))
        sys.exit(1)

    if args.stop_after and args.stop_after <= 1:
        summary = {
            "issue": issue["url"],
            "title": issue["title"],
            "issue_raw": str(output_dir / "issue_raw.json"),
            "issue_understanding": str(output_dir / "issue_understanding.json"),
            "understanding_source": issue["understanding"].get("source"),
            "confidence": issue["understanding"].get("confidence"),
            "dashboard": str(log.dashboard_path),
        }
        (output_dir / "run_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        log.finalize(success=True, summary=summary)
        return

    log.step_start("2", "path anchors + curated grep + slice...")
    try:
        context = ContextBuilder(repo_path=repo_path).build(issue)
        context_path = output_dir / "context.json"
        conventions_path = output_dir / "conventions.md"
        conventions_path.write_text(
            format_conventions_block(context.get("convention_snapshot", "")),
            encoding="utf-8",
        )
        (output_dir / "conventions_prompt.txt").write_text(
            load_conventions_prompt(), encoding="utf-8"
        )
        context_path.write_text(
            json.dumps(
                {
                    "anchor_paths_used": context.get("anchor_paths_used"),
                    "grep_terms_used": context.get("grep_terms_used"),
                    "candidate_files": context.get("candidate_files"),
                    "candidate_function_names": [
                        f["name"] for f in context["candidate_functions"]
                    ],
                    "convention_snapshot": context.get("convention_snapshot"),
                    "conventions_file": str(conventions_path),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        n = len(context["candidate_functions"])
        files = context.get("candidate_files") or list(
            {f["file"] for f in context["candidate_functions"]}
        )
        log.step_ok("2", f"{n} functions in {len(files)} file(s)")
        early_fix = assess_fix_state(repo_path, issue=issue)
        for w in early_fix.get("warnings") or []:
            log.warning(w)
        context["fix_preflight_warnings"] = early_fix.get("warnings") or []
        log.kv("Anchor paths", context.get("anchor_paths_used"))
        log.kv("Grep terms", context.get("grep_terms_used"))
        log.kv("Files in scope", ", ".join(files[:8]))
        log.artifact("Context manifest", str(context_path))
        log.artifact("Conventions", str(conventions_path))
    except Exception as e:
        log.step_fail("2", str(e))
        sys.exit(1)

    if args.stop_after and args.stop_after <= 2:
        summary = {
            "issue": issue["url"],
            "title": issue["title"],
            "issue_raw": str(output_dir / "issue_raw.json"),
            "issue_understanding": str(output_dir / "issue_understanding.json"),
            "context": str(output_dir / "context.json"),
            "candidate_files": context.get("candidate_files"),
            "dashboard": str(log.dashboard_path),
        }
        (output_dir / "run_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        log.finalize(success=True, summary=summary)
        return

    log.step_start("3", "LLM generating fix plan...")
    try:
        plan = CodeReasoningAgent(api_key=api_key).plan(issue=issue, context=context)
        fix_state = assess_fix_state(repo_path, plan=plan, issue=issue)
        issue["fix_state"] = fix_state
        context["fix_state_block"] = format_fix_state_block(fix_state)
        if fix_state.get("test_only_ok"):
            plan = (
                plan.rstrip()
                + "\n\n## Repo state (auto-detected)\n"
                + "Production fix is **already present** on this branch. "
                + "Implement **tests only** — do not edit production `.go` files.\n"
            )
            log.warning(
                "Production fix already in repo — test-only patch expected: "
                + ", ".join(fix_state.get("already_fixed_files") or [])
            )
        for w in fix_state.get("warnings") or []:
            log.warning(w)
        plan_path = output_dir / "plan.md"
        plan_path.write_text(plan, encoding="utf-8")
        log.step_ok("3", f"plan.md ({len(plan)} chars)")
        log.artifact("Plan", str(plan_path))
    except Exception as e:
        log.step_fail("3", str(e))
        sys.exit(1)

    log.step_start("4", "LLM generating unified diff...")
    try:
        generator = CodeGenerator(api_key=api_key, repo_path=repo_path)
        patch = generator.generate(issue=issue, context=context, plan=plan)
        patch_path = output_dir / "fix.patch"
        patch_path.write_text(patch, encoding="utf-8")
        log.diff_summary(patch, str(patch_path))
        log.step_ok("4", str(patch_path))
        log.artifact("Patch", str(patch_path))
    except Exception as e:
        log.step_fail("4", str(e))
        sys.exit(1)

    if args.stop_after and args.stop_after <= 4:
        summary = {
            "issue": issue["url"],
            "title": issue["title"],
            "patch": str(patch_path),
            "plan": str(output_dir / "plan.md"),
            "dashboard": str(log.dashboard_path),
        }
        (output_dir / "run_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        log.finalize(success=True, summary=summary)
        return

    plan_aligned = True
    plan_check_result: dict = {}
    if dry_run:
        log.step_skip("5", "DRY_RUN=true")
    else:
        log.step_start("5", "Checking patch matches plan...")
        try:
            plan_check_result = PlanAdherenceChecker(api_key=api_key).check(
                plan=plan, patch=patch
            )
            plan_aligned = bool(plan_check_result.get("plan_aligned"))
            pc = plan_check_result.get("plan_check") or {}
            log.kv("Plan aligned", plan_aligned)
            log.kv("Confidence", pc.get("confidence"))
            log.artifact("Plan check", str(output_dir / "plan_check.json"))

            summary_text = (plan_check_result.get("summary") or "")[:120]
            if plan_check_result["passed"]:
                log.step_ok("5", summary_text or "Patch matches plan")
            else:
                log.step_warn("5", summary_text[:200])
                deviations = (pc.get("deviations") or [])[:8]
                if deviations:
                    log.info("Regenerating patch to align with plan...")
                    try:
                        regen = generator.regenerate(
                            issue,
                            context,
                            plan,
                            patch,
                            "Plan adherence failed. Fix ALL of the following:\n"
                            + "\n".join(f"- {d}" for d in deviations)
                            + f"\n\nSummary: {summary_text}",
                        )
                        patch_path.write_text(regen, encoding="utf-8")
                        patch = regen
                        log.diff_summary(patch, str(patch_path))
                        plan_check_result = PlanAdherenceChecker(api_key=api_key).check(
                            plan=plan, patch=patch
                        )
                        plan_aligned = bool(plan_check_result.get("plan_aligned"))
                        if plan_check_result["passed"]:
                            log.step_ok("5", "Aligned after regeneration")
                        else:
                            log.step_warn(
                                "5",
                                (plan_check_result.get("summary") or "still not aligned")[:200],
                            )
                    except Exception as regen_err:
                        log.warning(f"Plan-alignment regeneration failed: {regen_err}")
        except Exception as e:
            plan_aligned = False
            log.step_fail("5", str(e))
            plan_check_result = {"passed": False, "error": str(e)}

    if args.stop_after and args.stop_after <= 5:
        summary = {
            "issue": issue["url"],
            "title": issue["title"],
            "plan_aligned": plan_aligned,
            "patch": str(patch_path),
            "plan": str(output_dir / "plan.md"),
            "plan_check": str(output_dir / "plan_check.json"),
            "dashboard": str(log.dashboard_path),
        }
        (output_dir / "run_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        log.finalize(success=plan_aligned if not dry_run else True, summary=summary)
        return

    validation_full = args.validation_full or parse_env_bool("VALIDATION_FULL")
    result: dict = {
        "passed": None,
        "validation_passed": None,
        "plan_aligned": plan_aligned,
        "test_output": "",
        "final_patch": patch,
    }
    if dry_run:
        log.step_skip("6", "DRY_RUN=true")
        result["test_output"] = "dry-run"
    else:
        log.step_start("6", "git apply + go build + scoped go test...")
        patch_before_validation = patch
        try:
            validator = Validator(
                repo_path=repo_path,
                full_suite=validation_full,
            )
            result = validator.validate(patch=patch, plan=plan, issue_type=issue_type)
            vr = result.get("validation_report") or {}
            log.kv("Apply passed", vr.get("apply_passed"))
            log.kv("Build passed", vr.get("build_passed"))
            log.kv("Tests passed", vr.get("tests_passed"))
            if vr.get("integrity_issues"):
                log.kv("Integrity issues", vr["integrity_issues"][:4])
            if vr.get("test_commands"):
                log.kv("Test commands", vr["test_commands"])
            log.artifact("Validation report", str(output_dir / "validation_report.json"))

            if result["passed"]:
                log.step_ok("6", "Patch applies and tests pass")
            else:
                err = result.get("error") or "validation failed"
                log.step_warn("6", err[:200])
                test_out = result.get("test_output") or ""
                build_failed = vr.get("build_passed") is False
                integrity_failed = bool(vr.get("integrity_issues"))
                if test_out or build_failed or integrity_failed:
                    log.info("Regenerating patch after validation failure...")
                    regen_hint = (
                        "Go validation failed. Fix the patch so it applies and tests pass.\n\n"
                        + test_out[-6000:]
                    )
                    if build_failed or integrity_failed:
                        regen_hint = (
                            build_retry_prompt(
                                (err or "").split(";"),
                                fix_state=issue.get("fix_state"),
                                stderr=test_out[-3000:],
                            )
                            + "\n\n"
                            + regen_hint
                        )
                    try:
                        regen = generator.regenerate(
                            issue,
                            context,
                            plan,
                            patch,
                            regen_hint,
                        )
                        patch_path.write_text(regen, encoding="utf-8")
                        patch = regen
                        log.diff_summary(patch, str(patch_path))
                        result = validator.validate(
                            patch=patch, plan=plan, issue_type=issue_type
                        )
                        if result["passed"]:
                            log.step_ok("6", "Tests pass after regeneration")
                        else:
                            log.step_warn(
                                "6",
                                (result.get("error") or "tests still failing")[:200],
                            )
                    except Exception as regen_err:
                        log.warning(f"Test-failure regeneration failed: {regen_err}")
        except Exception as e:
            log.step_fail("6", str(e))
            result = {
                "passed": False,
                "validation_passed": False,
                "plan_aligned": plan_aligned,
                "final_patch": patch,
                "test_output": str(e),
                "error": str(e),
            }

        patch = result.get("final_patch") or patch
        if patch != patch_before_validation or (
            result.get("validation_passed") and not plan_aligned
        ):
            log.info("Re-checking plan adherence on final patch...")
            try:
                plan_check_result = PlanAdherenceChecker(api_key=api_key).check(
                    plan=plan, patch=patch
                )
                plan_aligned = bool(plan_check_result.get("plan_aligned"))
                log.kv("Plan aligned (final)", plan_aligned)
            except Exception as e:
                log.warning(f"Final plan check failed: {e}")

    result["plan_aligned"] = plan_aligned
    result["final_patch"] = patch
    validation_passed = result.get("validation_passed") is True
    overall_passed = validation_passed and plan_aligned
    result["passed"] = overall_passed if not dry_run else result.get("passed")
    result["validation_passed"] = validation_passed

    if args.stop_after and args.stop_after <= 6:
        summary = {
            "issue": issue["url"],
            "title": issue["title"],
            "plan_aligned": plan_aligned,
            "validation_passed": validation_passed,
            "overall_passed": overall_passed,
            "patch": str(patch_path),
            "plan": str(output_dir / "plan.md"),
            "plan_check": str(output_dir / "plan_check.json"),
            "validation": str(output_dir / "validation_report.json"),
            "dashboard": str(log.dashboard_path),
        }
        (output_dir / "run_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        passed = overall_passed if not dry_run else True
        log.finalize(success=passed, summary=summary)
        return

    if not dry_run and (not validation_passed or not plan_aligned):
        reason = []
        if not validation_passed:
            reason.append("validation failed")
        if not plan_aligned:
            reason.append("plan not aligned")
        log.step_start("7", f"Writing PR summary ({', '.join(reason)} — draft only)...")
        pr_path = output_dir / "pr_summary.md"
        pr = build_draft_pr_summary(issue, plan, result)
        pr_path.write_text(pr, encoding="utf-8")
        log.step_warn("7", f"{pr_path} (draft — validation failed)")
        log.artifact("PR summary (draft)", str(pr_path))
    else:
        log.step_start("7", "Writing PR summary...")
        pr_path = output_dir / "pr_summary.md"
        try:
            pr = PRWriter(api_key=api_key).write(
                issue=issue, context=context, plan=plan,
                patch=result["final_patch"],
                test_output=result.get("test_output", ""),
            )
            log.step_ok("7", str(pr_path))
        except Exception as e:
            log.warning(f"PR writer fallback: {e}")
            pr = build_fallback_pr_summary(issue, plan, result)
            log.step_ok("7", f"{pr_path} (fallback)")
        pr_path.write_text(pr, encoding="utf-8")
        log.artifact("PR summary", str(pr_path))

    summary = {
        "issue": issue["url"],
        "title": issue["title"],
        "repo": str(repo_path) if repo_path else None,
        "plan_aligned": plan_aligned,
        "validation_passed": validation_passed,
        "overall_passed": overall_passed,
        "patch": str(patch_path),
        "plan": str(output_dir / "plan.md"),
        "plan_check": str(output_dir / "plan_check.json"),
        "validation": str(output_dir / "validation_report.json"),
        "pr_summary": str(pr_path),
        "dashboard": str(log.dashboard_path),
    }
    (output_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    success = overall_passed or (dry_run and result.get("validation_passed") is None)
    log.finalize(success=success, summary=summary)


if __name__ == "__main__":
    main()
