"""
Module 5 — Validator

Verifies the generated patch matches the fix plan (no git apply / go test).
"""

import json
from typing import Optional

from modules.agent_logger import get_logger
from modules.config import PROJECT_ROOT, get
from modules.plan_checker import PlanChecker


class Validator:
    def __init__(self, api_key: Optional[str] = None):
        self.checker = PlanChecker(api_key=api_key)

    def validate(self, plan: str, patch: str) -> dict:
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
        self._write_reports(report)

        if aligned:
            log.info("Validation PASSED — patch matches plan")
        else:
            log.warning(f"Validation FAILED — patch does not match plan: {summary}")
            if deviations:
                log.block("Plan deviations", "\n".join(f"- {d}" for d in deviations))

        return {
            "passed": aligned,
            "plan_aligned": aligned,
            "final_patch": patch,
            "test_output": summary,
            "error": None if aligned else (summary or "patch does not match plan"),
            "validation_report": report,
            "plan_check": check,
        }

    def _write_reports(self, report: dict) -> None:
        out = PROJECT_ROOT / get("OUTPUT_DIR", "./output")
        out.mkdir(parents=True, exist_ok=True)
        validation_path = out / "validation_report.json"
        plan_check_path = out / "plan_check.json"
        validation_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        plan_check_path.write_text(
            json.dumps(
                {k: v for k, v in report.items() if k != "check_type"},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        get_logger().info(f"Validation report → {validation_path}")
