#!/usr/bin/env python3
"""
Multi-repo batch evaluation against open issues with reference PRs.

Usage:
  python scripts/eval_batch.py run
  python scripts/eval_batch.py compare
  python scripts/eval_batch.py report
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.patch_utils import patch_files_changed

EVAL_DIR = ROOT / "eval"
RUNS_DIR = EVAL_DIR / "multi_runs"
MANIFEST = EVAL_DIR / "multi_run_manifest.json"
COMPARE = EVAL_DIR / "multi_comparisons.json"
REPORT = EVAL_DIR / "MULTI_REPO_EVALUATION.md"

# Open issues with open reference PRs (fix not on main/master).
BENCHMARK = {
    "spf13/cobra": {
        2193: {
            "reference_pr": 2194,
            "size": "small",
            "title": "Incorrect copy of command context parent→child",
            "pr_files_subset": ["command.go"],
        },
        2154: {
            "reference_pr": 2386,
            "size": "medium",
            "title": "Arguments not passed to HelpFunc",
            "pr_files_subset": ["command.go", "command_test.go"],
        },
    },
    "go-playground/validator": {
        1529: {
            "reference_pr": 1531,
            "size": "small",
            "title": "Cron validator accepts invalid expressions",
            "pr_files_subset": ["regexes.go", "validator_test.go"],
        },
        1575: {
            "reference_pr": 1579,
            "size": "medium",
            "title": "validateFn errors not propagated",
            "pr_files_subset": ["validator.go", "errors.go", "validator_test.go"],
        },
    },
    "golangci/golangci-lint": {
        4423: {
            "reference_pr": 4424,
            "size": "small",
            "title": "govet printf unstable with imported types (cache)",
            "pr_files_subset": ["pkg/golinters/goanalysis/runners.go"],
        },
        3983: {
            "reference_pr": 3986,
            "size": "small",
            "title": "lll: ignore long URLs in comments",
            "pr_files_subset": ["pkg/golinters/lll.go"],
        },
    },
}


def _issue_url(repo: str, n: int) -> str:
    return f"https://github.com/{repo}/issues/{n}"


def _run_key(repo: str, issue: int) -> str:
    slug = repo.split("/")[-1]
    return f"{slug}-issue-{issue}"


def fetch_github_json(args: list[str]):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout or "gh failed")
    return json.loads(r.stdout or "[]")


def fetch_github_text(args: list[str]) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout or "gh failed")
    return r.stdout


def file_overlap_score(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def iter_benchmark_issues():
    for repo, issues in BENCHMARK.items():
        for issue, meta in issues.items():
            yield repo, issue, meta


def cmd_run(_args):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for repo, issue, meta in iter_benchmark_issues():
        url = _issue_url(repo, issue)
        key = _run_key(repo, issue)
        out_dir = RUNS_DIR / key
        log_dir = out_dir / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n{'='*60}\n{repo} #{issue} — {meta['title']}\n{'='*60}")
        cmd = [
            sys.executable, str(ROOT / "main.py"),
            "--issue", url,
            "--output", str(out_dir),
            "--log-dir", str(log_dir),
            "--no-ui",
        ]
        r = subprocess.run(cmd, cwd=ROOT)
        summary_path = out_dir / "run_summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        results.append({
            "repo": repo,
            "issue": issue,
            "reference_pr": meta["reference_pr"],
            "url": url,
            "run_key": key,
            "exit_code": r.returncode,
            "summary": summary,
        })
    MANIFEST.write_text(
        json.dumps({"ran_at": datetime.now(timezone.utc).isoformat(), "results": results}, indent=2),
        encoding="utf-8",
    )
    print(f"\nManifest → {MANIFEST}")


def cmd_compare(_args):
    comparisons = []
    for repo, issue, meta in iter_benchmark_issues():
        key = _run_key(repo, issue)
        run_dir = RUNS_DIR / key
        pr_num = meta["reference_pr"]
        agent_patch = (run_dir / "fix.patch").read_text(encoding="utf-8") if (run_dir / "fix.patch").exists() else ""
        agent_files = set(patch_files_changed(agent_patch))

        pr_info = fetch_github_json(
            ["pr", "view", str(pr_num), "--repo", repo,
             "--json", "number,title,state,files,url"]
        )
        pr_diff = fetch_github_text(["pr", "diff", str(pr_num), "--repo", repo])
        pr_files = set(patch_files_changed(pr_diff))
        if pr_info.get("files"):
            pr_files |= {f["path"] for f in pr_info["files"]}

        subset = set(meta.get("pr_files_subset") or [])
        agent_on = agent_files & subset if subset else agent_files
        pr_on = pr_files & subset if subset else pr_files

        run_summary = json.loads((run_dir / "run_summary.json").read_text()) if (run_dir / "run_summary.json").exists() else {}
        plan_check = json.loads((run_dir / "plan_check.json").read_text()) if (run_dir / "plan_check.json").exists() else {}
        validation = json.loads((run_dir / "validation_report.json").read_text()) if (run_dir / "validation_report.json").exists() else {}
        ctx = json.loads((run_dir / "context.json").read_text()) if (run_dir / "context.json").exists() else {}

        entry = {
            "repo": repo,
            "issue": issue,
            "issue_url": _issue_url(repo, issue),
            "title": meta["title"],
            "size": meta["size"],
            "reference_pr": pr_num,
            "pr_url": pr_info.get("url"),
            "pr_title": pr_info.get("title"),
            "pr_state": pr_info.get("state"),
            "agent_files": sorted(agent_files),
            "pr_files": sorted(pr_files),
            "file_overlap_subset": file_overlap_score(agent_on, pr_on),
            "agent_files_on_subset": sorted(agent_on),
            "pr_files_on_subset": sorted(pr_on),
            "context_candidate_files": ctx.get("candidate_files", []),
            "plan_aligned": run_summary.get("plan_aligned"),
            "validation_passed": run_summary.get("validation_passed"),
            "apply_passed": validation.get("apply_passed"),
            "build_passed": validation.get("build_passed"),
            "tests_passed": validation.get("tests_passed"),
            "test_commands": validation.get("test_commands", []),
            "plan_check_summary": plan_check.get("summary"),
            "agent_patch_lines": len(agent_patch.splitlines()) if agent_patch else 0,
            "pr_diff_lines": len(pr_diff.splitlines()) if pr_diff else 0,
            "validation_error": validation.get("error"),
        }
        if pr_diff:
            (run_dir / "reference_pr.diff").write_text(pr_diff, encoding="utf-8")
        comparisons.append(entry)

    COMPARE.write_text(json.dumps(comparisons, indent=2), encoding="utf-8")
    for c in comparisons:
        slug = c["repo"].split("/")[-1]
        print(
            f"  {slug} #{c['issue']}: overlap={c['file_overlap_subset']:.0%} "
            f"validate={c['validation_passed']} plan={c['plan_aligned']}"
        )
    print(f"Wrote {COMPARE}")


def cmd_report(_args):
    if not COMPARE.exists():
        print("Run compare first", file=sys.stderr)
        sys.exit(1)
    comparisons = json.loads(COMPARE.read_text())
    passed = sum(1 for c in comparisons if c.get("validation_passed"))
    total = len(comparisons)

    lines = [
        "# Multi-repo evaluation report",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Repos:** spf13/cobra, go-playground/validator, golangci/golangci-lint",
        f"**Validation pass rate:** {passed}/{total} ({100*passed//total if total else 0}%)",
        "",
        "## Summary",
        "",
        "| Repo | Issue | Size | Plan | Validate | File overlap | Reference PR |",
        "|------|-------|------|------|----------|--------------|--------------|",
    ]
    for c in comparisons:
        slug = c["repo"].split("/")[-1]
        pa = "✅" if c.get("plan_aligned") else ("❌" if c.get("plan_aligned") is False else "—")
        va = "✅" if c.get("validation_passed") else ("❌" if c.get("validation_passed") is False else "—")
        ov = f"{c.get('file_overlap_subset', 0):.0%}"
        lines.append(
            f"| {slug} | [#{c['issue']}]({c['issue_url']}) | {c.get('size','')} | {pa} | {va} | {ov} | "
            f"[#{c['reference_pr']}]({c.get('pr_url','')}) |"
        )

    lines += ["", "## Per-issue notes", ""]
    for c in comparisons:
        slug = c["repo"].split("/")[-1]
        lines += [
            f"### {slug} #{c['issue']} — {c.get('title','')}",
            "",
            f"- **Issue:** {c['issue_url']}",
            f"- **Reference PR:** {c.get('pr_url')} ({c.get('pr_state')})",
            f"- **Plan:** {c.get('plan_check_summary') or c.get('plan_aligned')}",
            f"- **Validation:** apply={c.get('apply_passed')} build={c.get('build_passed')} tests={c.get('tests_passed')}",
            f"- **Error:** {c.get('validation_error') or 'none'}",
            f"- **Files (agent):** `{', '.join(c.get('agent_files_on_subset') or c.get('agent_files') or [])}`",
            f"- **Files (PR):** `{', '.join(c.get('pr_files_on_subset') or c.get('pr_files') or [])}`",
            f"- **Artifacts:** `eval/multi_runs/{slug}-issue-{c['issue']}/`",
            "",
        ]

    lines += [
        "## Benchmark criteria",
        "",
        "All issues are **open** with **open (unmerged) reference PRs** — fixes not yet on main.",
        "",
        "See [`eval/multi_comparisons.json`](multi_comparisons.json) for raw data.",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {REPORT}")


def main():
    p = argparse.ArgumentParser(description="Multi-repo Go issue evaluation")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Run agent on all benchmark issues")
    sub.add_parser("compare", help="Compare to reference PRs")
    sub.add_parser("report", help="Write MULTI_REPO_EVALUATION.md")
    args = p.parse_args()
    {"run": cmd_run, "compare": cmd_compare, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
