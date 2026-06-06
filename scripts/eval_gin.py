#!/usr/bin/env python3
"""
Screen gin-gonic/gin issues and run batch evaluation.

Usage:
  python scripts/eval_gin.py screen
  python scripts/eval_gin.py run --issues 4688,4645,4535
  python scripts/eval_gin.py compare --issues 4688,4645,4535
  python scripts/eval_gin.py report
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.issue_guardrails import score_issue
from modules.patch_utils import patch_files_changed

EVAL_DIR = ROOT / "eval"
GIN_SCREEN = EVAL_DIR / "gin_issue_screen.json"
GIN_RUNS = EVAL_DIR / "gin_runs"
GIN_COMPARE = EVAL_DIR / "gin_comparisons.json"

# Curated gin issues with a reference PR for benchmarking (open or merged).
BENCHMARK_ISSUES = {
    # Open issues — fix not yet on master (eval run 2026-06-06)
    4572: {
        "reference_pr": 4584,
        "size": "small",
        "title": "X-Forwarded-For IPv6 brackets and port",
        "pr_files_subset": ["gin.go"],
    },
    3850: {
        "reference_pr": 4674,
        "size": "small",
        "title": "PathUnescape vs QueryUnescape for path params",
        "pr_files_subset": ["tree.go", "tree_test.go"],
    },
    4034: {
        "reference_pr": 4659,
        "size": "small",
        "title": "Infinite redirect with RedirectFixedPath",
        "pr_files_subset": ["gin.go", "routes_test.go"],
    },
    4622: {
        "reference_pr": 4650,
        "size": "small",
        "title": "SaveUploadedFile chmod on existing directories",
        "pr_files_subset": ["context.go", "context_test.go"],
    },
    4237: {
        "reference_pr": 4592,
        "size": "medium",
        "title": "Unwrap errors.Join in Context.Error()",
        "pr_files_subset": ["context.go", "errors_test.go"],
    },
    3791: {
        "reference_pr": 4337,
        "size": "medium",
        "title": "Infinite recursion in form binding",
        "pr_files_subset": ["binding/form_mapping.go", "binding/form_mapping_test.go"],
    },
}


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


def cmd_screen(_args):
    closed = fetch_github_json(
        [
            "issue", "list", "--repo", "gin-gonic/gin",
            "--state", "closed", "--label", "bug", "--limit", "60",
            "--json", "number,title,state,labels,body,closedAt,url",
        ]
    )
    open_ = fetch_github_json(
        [
            "issue", "list", "--repo", "gin-gonic/gin",
            "--state", "open", "--limit", "60",
            "--json", "number,title,state,labels,body,url",
        ]
    )
    seen, issues = set(), []
    for i in closed + open_:
        if i["number"] not in seen:
            seen.add(i["number"])
            issues.append(i)

    scored = []
    for i in issues:
        labels = [l["name"] for l in i.get("labels", [])]
        s = score_issue(i["title"], i.get("body") or "", labels)
        entry = {
            "number": i["number"],
            "title": i["title"],
            "state": i["state"],
            "url": i["url"],
            "labels": labels,
            "score": s["score"],
            "recommended": s["recommended"],
            "reasons": s["reasons"],
        }
        if i["number"] in BENCHMARK_ISSUES:
            entry["benchmark"] = BENCHMARK_ISSUES[i["number"]]
        scored.append(entry)

    scored.sort(key=lambda x: (-x["score"], x["number"]))
    out = {
        "repo": "gin-gonic/gin",
        "screened_at": datetime.now(timezone.utc).isoformat(),
        "total_screened": len(scored),
        "benchmark_issues": BENCHMARK_ISSUES,
        "recommended_for_agent": [x for x in scored if x["recommended"]][:30],
        "low_score": [x for x in scored if x["score"] < 0][:20],
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    GIN_SCREEN.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {GIN_SCREEN} ({len(scored)} issues, {len(out['recommended_for_agent'])} recommended)")


def cmd_run(args):
    numbers = [int(x.strip()) for x in args.issues.split(",") if x.strip()]
    GIN_RUNS.mkdir(parents=True, exist_ok=True)
    results = []
    for n in numbers:
        url = f"https://github.com/gin-gonic/gin/issues/{n}"
        out_dir = GIN_RUNS / f"issue-{n}"
        log_dir = out_dir / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n{'='*60}\nRunning agent on #{n}\n{'='*60}")
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
        results.append({"issue": n, "url": url, "exit_code": r.returncode, "summary": summary})
    manifest = {"ran_at": datetime.now(timezone.utc).isoformat(), "results": results}
    (EVAL_DIR / "gin_run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nManifest → {EVAL_DIR / 'gin_run_manifest.json'}")


def _files_from_diff(diff: str) -> set[str]:
    return set(patch_files_changed(diff))


def file_overlap_score(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cmd_compare(args):
    numbers = [int(x.strip()) for x in args.issues.split(",") if x.strip()]
    comparisons = []
    for n in numbers:
        meta = BENCHMARK_ISSUES.get(n, {})
        pr_num = meta.get("reference_pr") or meta.get("merged_pr")
        run_dir = GIN_RUNS / f"issue-{n}"
        agent_patch_path = run_dir / "fix.patch"
        agent_patch = agent_patch_path.read_text(encoding="utf-8") if agent_patch_path.exists() else ""
        agent_files = _files_from_diff(agent_patch)

        pr_diff = ""
        pr_files: set[str] = set()
        pr_info = {}
        if pr_num:
            try:
                pr_info = fetch_github_json(
                    ["pr", "view", str(pr_num), "--repo", "gin-gonic/gin",
                     "--json", "number,title,mergedAt,files,url"]
                )
                pr_diff = fetch_github_text(["pr", "diff", str(pr_num), "--repo", "gin-gonic/gin"])
                pr_files = _files_from_diff(pr_diff)
                if pr_info.get("files"):
                    pr_files |= {f["path"] for f in pr_info["files"]}
            except Exception as e:
                pr_info = {"error": str(e)}

        subset = set(meta.get("pr_files_subset") or [])
        agent_on_subset = agent_files & subset if subset else agent_files
        pr_on_subset = pr_files & subset if subset else pr_files

        run_summary = {}
        rs = run_dir / "run_summary.json"
        if rs.exists():
            run_summary = json.loads(rs.read_text())
        plan_check = {}
        pc = run_dir / "plan_check.json"
        if pc.exists():
            plan_check = json.loads(pc.read_text())
        validation = {}
        vr = run_dir / "validation_report.json"
        if vr.exists():
            validation = json.loads(vr.read_text())

        ctx = {}
        cp = run_dir / "context.json"
        if cp.exists():
            ctx = json.loads(cp.read_text())

        entry = {
            "issue": n,
            "issue_url": f"https://github.com/gin-gonic/gin/issues/{n}",
            "title": meta.get("title", ""),
            "size": meta.get("size", ""),
            "reference_pr": pr_num,
            "pr_url": pr_info.get("url"),
            "pr_title": pr_info.get("title"),
            "agent_files": sorted(agent_files),
            "pr_files": sorted(pr_files),
            "file_overlap_all": file_overlap_score(agent_files, pr_files),
            "file_overlap_subset": file_overlap_score(agent_on_subset, pr_on_subset),
            "agent_files_on_subset": sorted(agent_on_subset),
            "pr_files_on_subset": sorted(pr_on_subset),
            "context_candidate_files": ctx.get("candidate_files", []),
            "plan_aligned": run_summary.get("plan_aligned"),
            "validation_passed": run_summary.get("validation_passed"),
            "apply_passed": validation.get("apply_passed"),
            "tests_passed": validation.get("tests_passed"),
            "test_commands": validation.get("test_commands", []),
            "plan_check_summary": plan_check.get("summary"),
            "agent_patch_lines": len(agent_patch.splitlines()) if agent_patch else 0,
            "pr_diff_lines": len(pr_diff.splitlines()) if pr_diff else 0,
        }
        if pr_diff and agent_patch:
            (run_dir / "merged_pr.diff").write_text(pr_diff, encoding="utf-8")
        comparisons.append(entry)

    GIN_COMPARE.write_text(json.dumps(comparisons, indent=2), encoding="utf-8")
    print(f"Wrote {GIN_COMPARE}")
    for c in comparisons:
        print(
            f"  #{c['issue']}: files={c['file_overlap_subset']:.0%} subset overlap, "
            f"validate={c['validation_passed']}, plan={c['plan_aligned']}"
        )


def cmd_report(_args):
    if not GIN_COMPARE.exists():
        print("Run compare first", file=sys.stderr)
        sys.exit(1)
    comparisons = json.loads(GIN_COMPARE.read_text())
    screen = json.loads(GIN_SCREEN.read_text()) if GIN_SCREEN.exists() else {}

    lines = [
        "# Gin evaluation report",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Issue screening",
        "",
        f"Screened **{screen.get('total_screened', '?')}** gin issues; "
        f"**{len(screen.get('recommended_for_agent', []))}** scored as agent-suitable (guardrails score ≥ 3).",
        "",
        "See [`eval/gin_issue_screen.json`](gin_issue_screen.json) for the full list.",
        "",
        "### Benchmark set (issues with reference PRs)",
        "",
        "| Issue | Size | Reference PR | Topic |",
        "|-------|------|--------------|-------|",
    ]
    for n, m in BENCHMARK_ISSUES.items():
        pr = m.get("reference_pr") or m.get("merged_pr")
        lines.append(
            f"| [#{n}](https://github.com/gin-gonic/gin/issues/{n}) | {m['size']} "
            f"| [#{pr}](https://github.com/gin-gonic/gin/pull/{pr}) | {m['title']} |"
        )

    lines += ["", "## Agent run results", ""]
    if not comparisons:
        lines.append("_No runs yet._")
    else:
        lines += [
            "| Issue | Plan OK | Go validate | File overlap (subset) | Agent files | PR files |",
            "|-------|---------|-------------|----------------------|-------------|----------|",
        ]
        for c in comparisons:
            pa = "✅" if c.get("plan_aligned") else ("⚠️" if c.get("plan_aligned") is False else "—")
            va = "✅" if c.get("validation_passed") else ("❌" if c.get("validation_passed") is False else "—")
            ov = f"{c.get('file_overlap_subset', 0):.0%}"
            af = ", ".join(c.get("agent_files_on_subset") or c.get("agent_files") or [])[:80]
            pf = ", ".join(c.get("pr_files_on_subset") or c.get("pr_files") or [])[:80]
            lines.append(f"| #{c['issue']} | {pa} | {va} | {ov} | `{af}` | `{pf}` |")

        lines += ["", "## Per-issue notes", ""]
        for c in comparisons:
            lines += [
                f"### Issue #{c['issue']} — {c.get('title', '')}",
                "",
                f"- **Issue:** {c['issue_url']}",
                f"- **Reference PR:** {c.get('pr_url', 'n/a')}",
                f"- **Context files found:** {', '.join(c.get('context_candidate_files') or []) or 'n/a'}",
                f"- **Plan adherence:** {c.get('plan_check_summary') or c.get('plan_aligned')}",
                f"- **Go validation:** apply={c.get('apply_passed')}, tests={c.get('tests_passed')}",
                f"- **Test commands:** `{c.get('test_commands')}`",
                f"- **File overlap (issue-relevant subset):** {c.get('file_overlap_subset', 0):.0%}",
                "",
            ]
            n = c["issue"]
            run_dir = GIN_RUNS / f"issue-{n}"
            if (run_dir / "fix.patch").exists():
                lines.append(f"Artifacts: `eval/gin_runs/issue-{n}/`")
            lines.append("")

    lines += [
        "## What we learned (improvement backlog)",
        "",
        "_Fill after reviewing diffs — see comparison JSON for raw data._",
        "",
        "1. **File targeting** — Does context builder find the same files as the merged PR?",
        "2. **Patch quality** — Does the agent fix the root cause or a symptom?",
        "3. **Test coverage** — Does the agent add/update the same tests maintainers chose?",
        "4. **Validation gate** — Does scoped `go test` catch bad patches before PR summary?",
        "5. **Scope creep** — Does the agent touch unrelated files (compare file lists)?",
        "",
    ]
    out = EVAL_DIR / "GIN_EVALUATION.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")


def main():
    p = argparse.ArgumentParser(description="Gin repo evaluation harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("screen", help="Screen gin issues")
    r = sub.add_parser("run", help="Run agent on issues")
    r.add_argument("--issues", default="4572,3850,4034,4622,4237,3791", help="Comma-separated issue numbers")
    c = sub.add_parser("compare", help="Compare agent output to reference PRs")
    c.add_argument("--issues", default="4572,3850,4034,4622,4237,3791")
    sub.add_parser("report", help="Write GIN_EVALUATION.md")
    args = p.parse_args()
    {"screen": cmd_screen, "run": cmd_run, "compare": cmd_compare, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
