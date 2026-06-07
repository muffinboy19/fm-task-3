#!/usr/bin/env python3
"""Run 5 benchmark bug issues and print a summary table."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.config import project_python
from modules.patch_utils import patch_files_changed
from scripts.eval_four_repos import fetch_github_text, _issue_url, _run_key

RUNS = [
    ("gin-gonic/gin", 4034, 4659, "redirect loop bug"),
    ("gin-gonic/gin", 2510, 4585, "form binding bug"),
    ("spf13/cobra", 2226, 2392, "ErrPrefix empty string"),
    ("go-playground/validator", 518, 1556, "datauri bug"),
    ("go-playground/validator", 1529, 1531, "cron validator bug"),
]

OUT_ROOT = ROOT / "eval" / "smoke5_runs"
MANIFEST = ROOT / "eval" / "smoke5_manifest.json"


def main():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    py = project_python()
    results = []

    for repo, issue, pr_num, note in RUNS:
        key = f"smoke5-{_run_key(repo, issue)}"
        out_dir = OUT_ROOT / key
        log_dir = out_dir / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        url = _issue_url(repo, issue)
        print(f"\n{'='*60}\n{repo} #{issue} — {note}\n{'='*60}", flush=True)

        try:
            pr_diff = fetch_github_text(["pr", "diff", str(pr_num), "--repo", repo])
            (out_dir / "reference_pr.diff").write_text(pr_diff, encoding="utf-8")
            ref_files = sorted(set(patch_files_changed(pr_diff)))
            (out_dir / "reference_pr_files.json").write_text(
                json.dumps(ref_files, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"  reference PR fetch skipped: {e}", flush=True)

        cmd = [
            py,
            str(ROOT / "main.py"),
            "--issue",
            url,
            "--output",
            str(out_dir),
            "--log-dir",
            str(log_dir),
            "--no-ui",
        ]
        r = subprocess.run(cmd, cwd=ROOT)
        summary = {}
        sp = out_dir / "run_summary.json"
        if sp.exists():
            summary = json.loads(sp.read_text(encoding="utf-8"))

        validation = {}
        vp = out_dir / "validation_report.json"
        if vp.exists():
            validation = json.loads(vp.read_text(encoding="utf-8"))

        plan_check = {}
        pc = out_dir / "plan_check.json"
        if pc.exists():
            plan_check = json.loads(pc.read_text(encoding="utf-8"))

        patch_lines = 0
        pp = out_dir / "fix.patch"
        if pp.exists():
            patch_lines = len(pp.read_text(encoding="utf-8").splitlines())

        entry = {
            "repo": repo,
            "issue": issue,
            "reference_pr": pr_num,
            "note": note,
            "url": url,
            "exit_code": r.returncode,
            "plan_aligned": summary.get("plan_aligned"),
            "validation_passed": summary.get("validation_passed"),
            "overall_passed": summary.get("overall_passed"),
            "patch_lines": patch_lines,
            "integrity_issues": validation.get("integrity_issues"),
            "apply_passed": validation.get("apply_passed"),
            "build_passed": validation.get("build_passed"),
            "tests_passed": validation.get("tests_passed"),
            "plan_check_passed": plan_check.get("passed"),
            "error": validation.get("error"),
        }
        results.append(entry)
        print(
            f"  → plan={entry['plan_aligned']} val={entry['validation_passed']} "
            f"patch={patch_lines}L exit={r.returncode}",
            flush=True,
        )

    passed = sum(
        1
        for r in results
        if r.get("validation_passed") and r.get("plan_aligned")
    )
    payload = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "total": len(results),
        "results": results,
    }
    MANIFEST.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n{'='*60}\nSUMMARY: {passed}/{len(results)} passed (validation + plan aligned)\n{'='*60}")
    for r in results:
        ok = "PASS" if r.get("validation_passed") and r.get("plan_aligned") else "FAIL"
        print(
            f"  [{ok}] {r['repo'].split('/')[-1]} #{r['issue']} — "
            f"plan={r.get('plan_aligned')} val={r.get('validation_passed')} "
            f"patch={r.get('patch_lines')}L "
            f"build={r.get('build_passed')} tests={r.get('tests_passed')}"
        )
        if r.get("integrity_issues"):
            print(f"        integrity: {r['integrity_issues'][:2]}")
        if r.get("error") and not r.get("validation_passed"):
            print(f"        error: {str(r['error'])[:120]}")
    print(f"\nManifest → {MANIFEST}")


if __name__ == "__main__":
    main()
