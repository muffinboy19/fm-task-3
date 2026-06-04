"""
Open Source Issue Solver — agent for open-source Go repositories.
"""

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from modules.config import get, get_gemini_api_keys, get_cursor_api_key, get_llm_provider
from modules.agent_logger import init_logger, get_logger
from modules.issue_understanding import IssueUnderstanding
from modules.context_builder import ContextBuilder
from modules.code_reasoning_agent import CodeReasoningAgent
from modules.code_generator import CodeGenerator
from modules.validator import Validator
from modules.pr_writer import PRWriter
from modules.convention import format_conventions_block, load_conventions_prompt
from modules.issue_guardrails import load_candidates, pick_next_candidate
from modules.live_state import clear_stale_run_outputs
from modules.repo_resolver import resolve_repo_path, save_repo_manifest


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
    parser.add_argument("--no-reset", action="store_true", help="Do not reset repo before run")
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
        default=int(get("DASHBOARD_PORT", "8765") or "8765"),
        help="Port for live dashboard when --ui is set",
    )
    parser.add_argument(
        "--list-candidates",
        action="store_true",
        help="Print screened issues from issues/candidates.json and exit",
    )
    return parser.parse_args()


def _fallback_pr_summary(issue: dict, plan: str, result: dict) -> str:
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


def _env_bool(key: str, default: bool = False) -> bool:
    val = get(key, "false" if not default else "true").lower()
    return val in ("1", "true", "yes", "on")


def reset_repo(repo_path: Path, log) -> bool:
    """Reset target repo to clean git state."""
    if not (repo_path / ".git").exists():
        log.warning("Not a git repo — skip reset")
        return False
    log.info("Resetting repository (git checkout + clean)...")
    subprocess.run(["git", "checkout", "--", "."], cwd=repo_path, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_path, check=False)
    r = subprocess.run(["git", "status", "--short"], cwd=repo_path, capture_output=True, text=True)
    clean = not (r.stdout or "").strip()
    log.kv("Repo clean after reset", clean)
    return clean


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

    issue_url = args.issue or get("GITHUB_ISSUE_URL")
    explicit_repo = args.repo or None
    if args.issue:
        os.environ["GITHUB_ISSUE_URL"] = args.issue
    output_dir = Path(args.output or get("OUTPUT_DIR", "./output")).resolve()
    log_dir = Path(args.log_dir or get("LOG_DIR", "./logs")).resolve()
    api_key = args.api_key or None
    github_token = args.github_token or get("GITHUB_TOKEN") or None
    dry_run = args.dry_run or _env_bool("DRY_RUN")

    output_dir.mkdir(parents=True, exist_ok=True)
    clear_stale_run_outputs(output_dir)

    log = init_logger(log_dir, output_dir=output_dir)

    ui_enabled = not args.no_ui and _env_bool("DASHBOARD_UI", default=True)
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

    if not issue_url:
        log.step_fail("1", "Missing issue URL (GITHUB_ISSUE_URL or --issue)")
        sys.exit(1)

    provider = get_llm_provider()
    needs_llm = not args.no_llm_intake or not (args.stop_after and args.stop_after <= 1)
    if needs_llm:
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

    if (
        repo_path
        and not args.no_reset
        and (not args.stop_after or args.stop_after > 1)
    ):
        reset_repo(repo_path, log)

    # ── Step 1 ───────────────────────────────────────────────────
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

    # ── Step 2 ───────────────────────────────────────────────────
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

    # ── Step 3 ───────────────────────────────────────────────────
    log.step_start("3", "LLM generating fix plan...")
    try:
        plan = CodeReasoningAgent(api_key=api_key).plan(issue=issue, context=context)
        plan_path = output_dir / "plan.md"
        plan_path.write_text(plan, encoding="utf-8")
        log.step_ok("3", f"plan.md ({len(plan)} chars)")
        log.artifact("Plan", str(plan_path))
    except Exception as e:
        log.step_fail("3", str(e))
        sys.exit(1)

    # ── Step 4 ───────────────────────────────────────────────────
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

    # ── Step 5 ───────────────────────────────────────────────────
    if dry_run:
        log.step_skip("5", "DRY_RUN=true")
        result = {"passed": None, "test_output": "dry-run", "final_patch": patch}
    else:
        log.step_start("5", "git apply + go test...")
        try:
            result = Validator(repo_path=repo_path).validate(
                patch=patch, generator=generator,
                issue=issue, context=context, plan=plan,
            )
            patch_path.write_text(result["final_patch"], encoding="utf-8")
            vr = result.get("validation_report") or {}
            log.kv("git apply", "OK" if vr.get("apply_passed") else "FAIL")
            log.kv("has tests", vr.get("patch_has_tests"))
            log.kv("go test", "PASS" if vr.get("tests_passed") else "FAIL")
            log.block("Test output", result.get("test_output", ""), max_report_chars=4000)
            log.artifact("Validation report", str(output_dir / "validation_report.json"))

            if result["passed"]:
                log.step_ok("5", "Patch applied + tests passed")
            else:
                log.step_fail("5", (result.get("error") or "unknown")[:120])
        except Exception as e:
            log.step_fail("5", str(e))
            result = {"passed": False, "final_patch": patch, "test_output": str(e)}

    # ── Step 6 ───────────────────────────────────────────────────
    log.step_start("6", "Writing PR summary...")
    pr_path = output_dir / "pr_summary.md"
    try:
        pr = PRWriter(api_key=api_key).write(
            issue=issue, context=context, plan=plan,
            patch=result["final_patch"],
            test_output=result.get("test_output", ""),
        )
        log.step_ok("6", str(pr_path))
    except Exception as e:
        log.warning(f"PR writer fallback: {e}")
        pr = _fallback_pr_summary(issue, plan, result)
        log.step_ok("6", f"{pr_path} (fallback)")
    pr_path.write_text(pr, encoding="utf-8")
    log.artifact("PR summary", str(pr_path))

    summary = {
        "issue": issue["url"],
        "title": issue["title"],
        "repo": str(repo_path) if repo_path else None,
        "validation_passed": result.get("passed"),
        "patch": str(patch_path),
        "plan": str(output_dir / "plan.md"),
        "pr_summary": str(pr_path),
        "dashboard": str(log.dashboard_path),
    }
    (output_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    success = result.get("passed") is True or (dry_run and result.get("passed") is None)
    log.finalize(success=success, summary=summary)


if __name__ == "__main__":
    main()
