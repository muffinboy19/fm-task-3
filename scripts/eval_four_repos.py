#!/usr/bin/env python3
"""
Four-repo evaluation: open issues + open reference PRs (fix not on master).

  python scripts/eval_four_repos.py discover
  python scripts/eval_four_repos.py run
  python scripts/eval_four_repos.py compare
  python scripts/eval_four_repos.py report
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

from modules.config import project_python
from modules.patch_utils import patch_files_changed

REPOS = [
    "gin-gonic/gin",
    "spf13/cobra",
    "go-playground/validator",
    "golangci/golangci-lint",
]

EVAL_DIR = ROOT / "eval"
RUNS_DIR = EVAL_DIR / "four_repo_runs"
BENCHMARK_FILE = EVAL_DIR / "four_repo_benchmark.json"
MANIFEST = EVAL_DIR / "four_repo_manifest.json"
COMPARE = EVAL_DIR / "four_repo_comparisons.json"
REPORT = EVAL_DIR / "FOUR_REPO_EVALUATION.md"

SKIP_PR_TITLE = re.compile(
    r"^(chore|deps|dependabot|bump|ci:|docs:|release|merge|revert)",
    re.I,
)
SKIP_ISSUE_TITLE = re.compile(r"\badd linter\b", re.I)


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


def file_overlap(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _run_key(repo: str, issue: int) -> str:
    return f"{repo.split('/')[-1]}-issue-{issue}"


def _issue_url(repo: str, n: int) -> str:
    return f"https://github.com/{repo}/issues/{n}"


def discover_repo(repo: str, limit: int = 6) -> list[dict]:
    """Open issues with an open PR linked (fix not merged to default branch)."""
    prs = fetch_github_json(
        [
            "pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
            "--json", "number,title,state,url,closingIssuesReferences",
        ]
    )
    candidates: dict[int, dict] = {}
    for pr in prs:
        title = pr.get("title") or ""
        if SKIP_PR_TITLE.search(title.strip()):
            continue
        refs = pr.get("closingIssuesReferences") or []
        if not refs:
            continue
        for ref in refs:
            num = ref.get("number")
            if not num or num in candidates:
                continue
            try:
                issue = fetch_github_json(
                    ["issue", "view", str(num), "--repo", repo,
                     "--json", "number,title,state,labels,url"]
                )
            except RuntimeError:
                continue
            if issue.get("state") != "OPEN":
                continue
            labels = [lb.get("name", "") for lb in issue.get("labels") or []]
            title = issue.get("title") or ""
            if repo == "golangci/golangci-lint" and SKIP_ISSUE_TITLE.search(title):
                continue
            candidates[num] = {
                "issue": num,
                "issue_title": title,
                "issue_url": issue.get("url") or _issue_url(repo, num),
                "reference_pr": pr["number"],
                "pr_title": title,
                "pr_url": pr.get("url"),
                "labels": labels,
            }

    # Prefer bugs/fixes; deprioritize feature/enhancement
    def sort_key(item: dict) -> tuple:
        labels = {x.lower() for x in item.get("labels") or []}
        title = (item.get("issue_title") or "").lower()
        score = 0
        if "bug" in labels or "bug" in title:
            score += 3
        if "fix" in title or title.startswith("fix"):
            score += 2
        if "feature" in labels or "enhancement" in labels:
            score -= 2
        if "feature" in title.lower():
            score -= 1
        return (-score, item["issue"])

    picked = sorted(candidates.values(), key=sort_key)[:limit]
    return picked


def cmd_discover(_args):
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    benchmark: dict = {}
    for repo in REPOS:
        found = discover_repo(repo, limit=6)
        benchmark[repo] = {
            str(item["issue"]): {
                "reference_pr": item["reference_pr"],
                "title": item["issue_title"],
                "pr_title": item["pr_title"],
                "pr_url": item["pr_url"],
                "labels": item["labels"],
            }
            for item in found
        }
        print(f"{repo}: {len(found)} issue(s)")
        for item in found:
            print(f"  #{item['issue']} ← PR #{item['reference_pr']} — {item['issue_title'][:60]}")

    payload = {
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "criteria": "open issue + open unmerged PR linked via closingIssuesReferences",
        "repos": REPOS,
        "benchmark": benchmark,
    }
    BENCHMARK_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {BENCHMARK_FILE}")


def _load_benchmark() -> dict:
    if not BENCHMARK_FILE.exists():
        raise SystemExit(f"Run discover first: python scripts/eval_four_repos.py discover")
    return json.loads(BENCHMARK_FILE.read_text())


def iter_benchmark(benchmark: dict):
    for repo, issues in benchmark.get("benchmark", benchmark).items():
        if repo in REPOS or "/" in repo:
            for issue_str, meta in issues.items():
                yield repo, int(issue_str), meta


def cmd_run(_args):
    data = _load_benchmark()
    benchmark = data.get("benchmark", data)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    py = project_python()
    results = []
    for repo, issue, meta in iter_benchmark(benchmark):
        url = _issue_url(repo, issue)
        key = _run_key(repo, issue)
        out_dir = RUNS_DIR / key
        log_dir = out_dir / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n{'='*60}\n{repo} #{issue} — {meta.get('title','')[:50]}\n{'='*60}")
        pr_num = meta.get("reference_pr")
        if pr_num:
            try:
                pr_diff = fetch_github_text(["pr", "diff", str(pr_num), "--repo", repo])
                (out_dir / "reference_pr.diff").write_text(pr_diff, encoding="utf-8")
                ref_files = sorted(set(patch_files_changed(pr_diff)))
                (out_dir / "reference_pr_files.json").write_text(
                    json.dumps(ref_files, indent=2), encoding="utf-8"
                )
            except RuntimeError as e:
                print(f"  (reference PR fetch skipped: {e})")
        cmd = [
            py, str(ROOT / "main.py"),
            "--issue", url,
            "--output", str(out_dir),
            "--log-dir", str(log_dir),
            "--no-ui",
        ]
        r = subprocess.run(cmd, cwd=ROOT)
        summary = {}
        sp = out_dir / "run_summary.json"
        if sp.exists():
            summary = json.loads(sp.read_text())
        results.append({
            "repo": repo,
            "issue": issue,
            "reference_pr": meta.get("reference_pr"),
            "url": url,
            "run_key": key,
            "exit_code": r.returncode,
            "summary": summary,
        })
    MANIFEST.write_text(
        json.dumps({"ran_at": datetime.now(timezone.utc).isoformat(), "results": results}, indent=2),
        encoding="utf-8",
    )
    passed = sum(1 for r in results if r.get("summary", {}).get("validation_passed"))
    print(f"\nDone: {passed}/{len(results)} validation passed")
    print(f"Manifest → {MANIFEST}")


def cmd_compare(_args):
    data = _load_benchmark()
    benchmark = data.get("benchmark", data)
    comparisons = []
    for repo, issue, meta in iter_benchmark(benchmark):
        key = _run_key(repo, issue)
        run_dir = RUNS_DIR / key
        pr_num = meta.get("reference_pr")
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

        run_summary = json.loads((run_dir / "run_summary.json").read_text()) if (run_dir / "run_summary.json").exists() else {}
        plan_check = json.loads((run_dir / "plan_check.json").read_text()) if (run_dir / "plan_check.json").exists() else {}
        validation = json.loads((run_dir / "validation_report.json").read_text()) if (run_dir / "validation_report.json").exists() else {}
        ctx = json.loads((run_dir / "context.json").read_text()) if (run_dir / "context.json").exists() else {}

        entry = {
            "repo": repo,
            "issue": issue,
            "issue_url": _issue_url(repo, issue),
            "title": meta.get("title", ""),
            "reference_pr": pr_num,
            "pr_url": pr_info.get("url") or meta.get("pr_url"),
            "pr_title": pr_info.get("title") or meta.get("pr_title"),
            "pr_state": pr_info.get("state"),
            "agent_files": sorted(agent_files),
            "pr_files": sorted(pr_files),
            "file_overlap": file_overlap(agent_files, pr_files),
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
            "exit_code": run_summary.get("validation_passed"),
        }
        if pr_diff:
            (run_dir / "reference_pr.diff").write_text(pr_diff, encoding="utf-8")
        comparisons.append(entry)

    COMPARE.write_text(json.dumps(comparisons, indent=2), encoding="utf-8")
    for c in comparisons:
        slug = c["repo"].split("/")[-1]
        va = "✅" if c.get("validation_passed") else "❌"
        print(f"  {slug} #{c['issue']}: overlap={c['file_overlap']:.0%} validate={va}")
    print(f"Wrote {COMPARE}")


def cmd_report(_args):
    if not COMPARE.exists():
        raise SystemExit("Run compare first")
    data = _load_benchmark()
    comparisons = json.loads(COMPARE.read_text())
    passed = sum(1 for c in comparisons if c.get("validation_passed"))
    total = len(comparisons)

    by_repo: dict[str, list] = {}
    for c in comparisons:
        by_repo.setdefault(c["repo"], []).append(c)

    lines = [
        "# Four-repo evaluation report",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Criteria",
        "",
        "- Issue **open** on default branch (fix not merged)",
        "- **Open PR** linked to the issue (reference for comparison)",
        "- Up to **6 issues per repo**",
        "",
        "## Overall",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Issues evaluated | {total} |",
        f"| Validation passed | **{passed}/{total}** ({100*passed//total if total else 0}%) |",
        f"| Plan aligned | {sum(1 for c in comparisons if c.get('plan_aligned'))}/{total} |",
        "",
        "## By repository",
        "",
    ]

    for repo in REPOS:
        items = by_repo.get(repo, [])
        if not items:
            lines += [f"### {repo}", "", "_No runs._", ""]
            continue
        rp = sum(1 for c in items if c.get("validation_passed"))
        slug = repo.split("/")[-1]
        lines += [
            f"### {repo}",
            "",
            f"**Validation:** {rp}/{len(items)} passed",
            "",
            "| Issue | Plan | Validate | File overlap | Reference PR |",
            "|-------|------|----------|--------------|--------------|",
        ]
        for c in items:
            pa = "✅" if c.get("plan_aligned") else ("❌" if c.get("plan_aligned") is False else "—")
            va = "✅" if c.get("validation_passed") else ("❌" if c.get("validation_passed") is False else "—")
            ov = f"{c.get('file_overlap', 0):.0%}"
            lines.append(
                f"| [#{c['issue']}]({c['issue_url']}) | {pa} | {va} | {ov} | "
                f"[#{c['reference_pr']}]({c.get('pr_url','')}) |"
            )
        lines += ["", f"Artifacts: `eval/four_repo_runs/{slug}-issue-*/`", ""]

    lines += ["## Per-issue notes", ""]
    for c in comparisons:
        slug = c["repo"].split("/")[-1]
        lines += [
            f"### {slug} #{c['issue']} — {c.get('title','')[:70]}",
            "",
            f"- **Issue:** {c['issue_url']}",
            f"- **Reference PR:** {c.get('pr_url')} ({c.get('pr_state')})",
            f"- **Plan:** {c.get('plan_check_summary') or c.get('plan_aligned')}",
            f"- **Validation:** apply={c.get('apply_passed')} build={c.get('build_passed')} tests={c.get('tests_passed')}",
            f"- **Error:** {c.get('validation_error') or 'none'}",
            f"- **File overlap (agent vs PR):** {c.get('file_overlap', 0):.0%}",
            f"- **Agent files:** `{', '.join(c.get('agent_files') or [])}`",
            f"- **PR files:** `{', '.join(c.get('pr_files') or [])}`",
            "",
        ]

    lines += [
        "## Improvement backlog",
        "",
        "1. **File targeting** — overlap with maintainer PR file set",
        "2. **Open PR benchmark** — issues with unmerged fixes are the fairest eval set",
        "3. **Feature vs bug** — bugs in existing code score higher than new-feature issues",
        "4. **golangci-lint** — large monorepo; context builder may miss package paths",
        "",
        "Raw data: [`eval/four_repo_comparisons.json`](four_repo_comparisons.json)",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {REPORT}")


def main():
    p = argparse.ArgumentParser(description="Four-repo open-issue evaluation")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover", help="Find open issues with open PRs")
    sub.add_parser("run", help="Run agent on all benchmark issues")
    sub.add_parser("compare", help="Compare agent output to reference PRs")
    sub.add_parser("report", help="Write FOUR_REPO_EVALUATION.md")
    args = p.parse_args()
    {"discover": cmd_discover, "run": cmd_run, "compare": cmd_compare, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
