"""
Plan adherence checker (pipeline step 5).

LLM compares fix.patch to plan.md — files, approach, and planned tests.
Writes output/plan_check.json only (not validation_report.json).
"""

import json
from typing import Optional

from modules.agent_logger import get_logger
from modules.config import PROJECT_ROOT
from modules.plan_checker import PlanChecker
from modules.repo_resolver import default_output_dir


class PlanAdherenceChecker:
    def __init__(self, api_key: Optional[str] = None):
        self.checker = PlanChecker(api_key=api_key)

    def check(self, plan: str, patch: str) -> dict:
        log = get_logger()
        check = self.checker.check(plan=plan, patch=patch)
        aligned = bool(check.get("aligned"))
        summary = check.get("summary") or ""
        deviations = check.get("deviations") or []

        report = {
            **check,
            "check_type": "plan_adherence",
            "overall_passed": aligned,
        }
        self._write_report(report)

        if aligned:
            log.info("Plan adherence PASSED — patch matches plan")
        else:
            log.warning(f"Plan adherence FAILED: {summary}")
            if deviations:
                log.block("Plan deviations", "\n".join(f"- {d}" for d in deviations))

        return {
            "passed": aligned,
            "plan_aligned": aligned,
            "summary": summary,
            "deviations": deviations,
            "plan_check": check,
            "error": None if aligned else (summary or "patch does not match plan"),
        }

    def _write_report(self, report: dict) -> None:
        out = default_output_dir()
        out.mkdir(parents=True, exist_ok=True)
        path = out / "plan_check.json"
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        get_logger().info(f"Plan check → {path}")
