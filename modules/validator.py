import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from modules.agent_logger import get_logger
from modules.config import get_env
from modules.patch_utils import (
    packages_to_test,
    patch_files_changed,
    patch_includes_tests,
    patch_integrity_issues,
    patch_touches_go,
)
from modules.repo_resolver import default_output_dir


def extract_test_names_from_plan(plan: str) -> list[str]:
    found: list[str] = []
    for pat in (
        r"`(Test[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)?)`",
        r"\b(Test[A-Z][A-Za-z0-9_]*(?:/[A-Za-z0-9_]+)?)\b",
    ):
        for m in re.findall(pat, plan):
            if m not in found and not m.startswith("Testify"):
                found.append(m)
    return found


def build_go_test_run_pattern(test_names: list[str]) -> str:
    parts = [re.escape(n) for n in test_names]
    return "|".join(parts)


class Validator:
    def __init__(
        self,
        repo_path: Path,
        go_bin: Optional[str] = None,
        full_suite: bool = False,
    ):
        self.repo_path = repo_path.resolve()
        self.go_bin = go_bin or get_env("GO_BIN") or shutil.which("go") or "go"
        self.full_suite = full_suite or get_env("VALIDATION_FULL", "").lower() in (
            "1",
            "true",
            "yes",
        )

    def validate(self, patch: str, plan: str = "", issue_type: str = "") -> dict:
        log = get_logger()
        files_changed = patch_files_changed(patch)
        pkgs = packages_to_test(patch)
        plan_tests = extract_test_names_from_plan(plan)
        has_go = patch_touches_go(patch)
        integrity = patch_integrity_issues(
            patch,
            issue_type=issue_type,
            repo_path=self.repo_path,
        )

        report: dict = {
            "check_type": "go_validation",
            "go_installed": bool(shutil.which(self.go_bin) or self._go_version_ok()),
            "go_binary": self.go_bin,
            "files_changed": files_changed,
            "packages": pkgs,
            "patch_has_tests": patch_includes_tests(patch),
            "plan_test_names": plan_tests,
            "integrity_issues": integrity,
            "tiers_run": [],
            "apply_passed": False,
            "build_passed": None,
            "tests_passed": None,
            "test_commands": [],
            "overall_passed": False,
            "test_output": "",
            "error": None,
        }

        if not patch.strip():
            report["error"] = "EMPTY_PATCH"
            self._write_report(report)
            return self._result(report, patch)

        if integrity:
            report["error"] = "; ".join(integrity[:4])
            report["tiers_run"].append("A_integrity")
            self._write_report(report)
            log.warning(f"Patch integrity failed: {integrity[:4]}")
            return self._result(report, patch)

        report["tiers_run"].append("A_apply_check")
        apply_check = self._git_apply_check(patch)
        if apply_check["exit"] != 0:
            report["error"] = apply_check["stderr"] or "git apply --check failed"
            report["apply_passed"] = False
            self._write_report(report)
            log.warning(f"git apply --check failed: {report['error'][:300]}")
            return self._result(report, patch)

        report["apply_passed"] = True
        log.info("git apply --check OK")

        if not has_go:
            report["build_passed"] = None
            report["tests_passed"] = None
            report["overall_passed"] = True
            report["test_output"] = "No .go files in patch — apply check only"
            self._write_report(report)
            log.info("Validation PASSED (non-Go patch, apply check only)")
            return self._result(report, patch)

        if not report["go_installed"]:
            report["error"] = f"Go not found (GO_BIN={self.go_bin})"
            self._write_report(report)
            log.warning(report["error"])
            return self._result(report, patch)

        applied = self._git_apply(patch)
        if not applied["ok"]:
            report["error"] = applied["stderr"] or "git apply failed"
            self._write_report(report)
            return self._result(report, patch)

        try:
            report["tiers_run"].append("B_build")
            build = self._go_build(pkgs)
            report["build_passed"] = build["passed"]
            report["test_output"] = build["output"]
            if not build["passed"]:
                report["error"] = "go build failed"
                self._write_report(report)
                log.warning("go build FAILED")
                return self._result(report, patch)
            log.info("go build OK")

            test_cmds = self._test_commands(pkgs, plan_tests)
            report["test_commands"] = test_cmds
            report["tiers_run"].append(
                "E_full_suite" if self.full_suite else (
                    "C_targeted_tests" if plan_tests else (
                        "D_package_tests" if patch_includes_tests(patch) else "D_package_tests"
                    )
                )
            )

            all_output: list[str] = []
            tests_ok = True
            for cmd in test_cmds:
                log.info(f"Running: {' '.join(cmd)}")
                r = self._run(cmd, timeout=600)
                block = f"=== {' '.join(cmd[1:])} (exit {r.returncode}) ===\n{r.stdout or ''}{r.stderr or ''}"
                all_output.append(block)
                if r.returncode != 0:
                    tests_ok = False
                    break

            report["test_output"] = "\n\n".join(all_output)
            report["tests_passed"] = tests_ok
            report["overall_passed"] = tests_ok
            if not tests_ok:
                report["error"] = "go test failed"
                log.warning("go test FAILED")
            else:
                log.info("go test PASSED")
                preview = report["test_output"][-4000:]
                log.block("Test output", preview)

        finally:
            self._revert_repo()

        self._write_report(report)
        return self._result(report, patch)

    def _test_commands(self, pkgs: list[str], plan_tests: list[str]) -> list[list[str]]:
        if self.full_suite:
            return [[self.go_bin, "test", "-count=1", "./..."]]

        if plan_tests:
            run_pat = build_go_test_run_pattern(plan_tests)
            return [[self.go_bin, "test", "-count=1", "-run", run_pat, pkg] for pkg in pkgs]

        return [[self.go_bin, "test", "-count=1", pkg] for pkg in pkgs]

    def _go_version_ok(self) -> bool:
        try:
            r = subprocess.run(
                [self.go_bin, "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _git_apply_check(self, patch: str) -> dict:
        patch_file = self._write_temp_patch(patch)
        try:
            r = subprocess.run(
                ["git", "apply", "--check", patch_file],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {"exit": r.returncode, "stderr": (r.stderr or r.stdout or "").strip()}
        finally:
            Path(patch_file).unlink(missing_ok=True)

    def _git_apply(self, patch: str) -> dict:
        patch_file = self._write_temp_patch(patch)
        try:
            r = subprocess.run(
                ["git", "apply", patch_file],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            ok = r.returncode == 0
            if ok:
                log = get_logger()
                log.info("Patch applied successfully")
            return {
                "ok": ok,
                "stderr": (r.stderr or r.stdout or "").strip(),
            }
        finally:
            Path(patch_file).unlink(missing_ok=True)

    def _revert_repo(self) -> None:
        subprocess.run(["git", "checkout", "--", "."], cwd=self.repo_path, check=False)
        subprocess.run(["git", "clean", "-fd"], cwd=self.repo_path, check=False)

    def _go_build(self, pkgs: list[str]) -> dict:
        if len(pkgs) == 1 and pkgs[0] == "./...":
            cmd = [self.go_bin, "build", "./..."]
        else:
            cmd = [self.go_bin, "build"] + pkgs
        r = self._run(cmd, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        return {"passed": r.returncode == 0, "output": out.strip()}

    def _run(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _write_temp_patch(self, patch: str) -> str:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
            f.write(patch if patch.endswith("\n") else patch + "\n")
            return f.name

    def _write_report(self, report: dict) -> None:
        out = default_output_dir()
        out.mkdir(parents=True, exist_ok=True)
        path = out / "validation_report.json"
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        get_logger().info(f"Validation report → {path}")

    def _result(self, report: dict, patch: str) -> dict:
        passed = bool(report.get("overall_passed"))
        return {
            "passed": passed,
            "validation_passed": passed,
            "plan_aligned": None,
            "final_patch": patch,
            "test_output": report.get("test_output") or report.get("error") or "",
            "error": report.get("error"),
            "validation_report": report,
        }
