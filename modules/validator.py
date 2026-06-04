"""
Module 5 — Validator

1. git apply the patch
2. Verify the patch includes tests
3. go test on affected packages (and full suite if small)
4. Retry with code generator on apply or test failure
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from modules.agent_logger import get_logger
from modules.config import PROJECT_ROOT, get
from modules.patch_utils import patch_files_changed, patch_includes_tests, packages_to_test

MAX_RETRIES = 2


def find_go_binary() -> str | None:
    """Resolve go executable: PATH, GO_BIN, or common install locations."""
    explicit = get("GO_BIN")
    if explicit and Path(explicit).is_file():
        return explicit

    found = shutil.which("go")
    if found:
        return found

    for candidate in (
        "/opt/homebrew/bin/go",
        "/usr/local/go/bin/go",
        os.path.expanduser("~/go/bin/go"),
    ):
        if Path(candidate).is_file():
            return candidate
    return None


class Validator:
    def __init__(self, repo_path: Path):
        self.repo = repo_path
        self.go_bin = find_go_binary()

    def validate(
        self,
        patch: str,
        generator,
        issue: dict,
        context: dict,
        plan: str,
    ) -> dict:
        current_patch = patch
        last_error = ""
        test_output = ""
        report = {
            "go_installed": self.go_bin is not None,
            "go_binary": self.go_bin,
            "patch_has_tests": patch_includes_tests(patch),
            "files_changed": patch_files_changed(patch),
            "packages_tested": [],
            "apply_passed": False,
            "tests_passed": False,
        }

        log = get_logger()
        log.kv("Patch includes *_test.go changes", report["patch_has_tests"])

        if not report["patch_has_tests"]:
            log.warning(
                "Patch has no test file changes — regeneration should add tests"
            )

        for attempt in range(MAX_RETRIES + 1):
            log.info(f"Validation attempt {attempt + 1}/{MAX_RETRIES + 1}")
            report["patch_has_tests"] = patch_includes_tests(current_patch)
            report["files_changed"] = patch_files_changed(current_patch)

            apply_result = self._try_apply(current_patch)
            if not apply_result["applied"]:
                last_error = apply_result["error"]
                test_output = last_error
                report["apply_passed"] = False
                log.warning("git apply failed")
                log.block("Apply error", last_error)
                if attempt < MAX_RETRIES:
                    log.info("Regenerating patch (must include tests)...")
                    current_patch = generator.regenerate(
                        issue, context, plan, current_patch, last_error
                    )
                continue

            report["apply_passed"] = True
            log.info("Patch applied successfully")

            test_result = self._run_tests(current_patch)
            test_output = test_result["output"]
            report["packages_tested"] = test_result["packages"]
            report["tests_passed"] = test_result["passed"]
            log.block(f"Test output (attempt {attempt + 1})", test_output)

            if test_result["passed"]:
                log.info("Tests PASSED — fix verified")
                self._write_report(report, test_output, passed=True)
                return {
                    "passed": True,
                    "final_patch": current_patch,
                    "test_output": test_output,
                    "error": None,
                    "validation_report": report,
                }

            last_error = test_output
            log.warning("Tests FAILED — fix does not pass validation")
            self._reset_repo()

            if attempt < MAX_RETRIES:
                log.info("Regenerating patch using test failures...")
                err = (
                    f"{test_output}\n\n"
                    "The patch MUST include *_test.go changes that prove the fix. "
                    "Ensure tests fail before the fix and pass after."
                )
                current_patch = generator.regenerate(
                    issue, context, plan, current_patch, err
                )

        report["apply_passed"] = report.get("apply_passed", False)
        report["tests_passed"] = False
        self._write_report(report, test_output, passed=False)
        log.error("Validation exhausted all retries")

        return {
            "passed": False,
            "final_patch": current_patch,
            "test_output": test_output,
            "error": last_error,
            "validation_report": report,
        }

    def _run_tests(self, patch: str) -> dict:
        log = get_logger()

        if not self.go_bin:
            msg = (
                "Go is not installed or not on PATH. "
                "Install Go (https://go.dev/dl/) or set GO_BIN in .env to the go binary. "
                "Cannot verify the fix with tests."
            )
            log.error(msg)
            return {"passed": False, "output": msg, "packages": []}

        pkgs = packages_to_test(patch)
        log.info(f"Running tests for packages: {pkgs}")

        combined = []
        all_ok = True
        for pkg in pkgs:
            log.info(f"  go test {pkg}")
            r = subprocess.run(
                [self.go_bin, "test", pkg, "-count=1"],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=300,
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
            )
            block = f"=== go test {pkg} (exit {r.returncode}) ===\n{r.stdout}\n{r.stderr}"
            combined.append(block.strip())
            if r.returncode != 0:
                all_ok = False

        return {
            "passed": all_ok,
            "output": "\n\n".join(combined),
            "packages": pkgs,
        }

    def _write_report(self, report: dict, test_output: str, passed: bool):
        out = PROJECT_ROOT / get("OUTPUT_DIR", "./output")
        out.mkdir(parents=True, exist_ok=True)
        report["overall_passed"] = passed
        report["test_output_preview"] = test_output[-4000:]
        path = out / "validation_report.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        get_logger().info(f"Validation report → {path}")

    def _try_apply(self, patch: str) -> dict:
        get_logger().debug("Running git apply --check")
        self._reset_repo()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False
        ) as f:
            f.write(patch)
            patch_file = f.name

        try:
            check = subprocess.run(
                ["git", "apply", "--check", patch_file],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if check.returncode != 0:
                return {
                    "applied": False,
                    "error": f"git apply --check failed:\n{check.stderr}\n{check.stdout}",
                }

            apply = subprocess.run(
                ["git", "apply", patch_file],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if apply.returncode != 0:
                return {
                    "applied": False,
                    "error": f"git apply failed:\n{apply.stderr}\n{apply.stdout}",
                }

            return {"applied": True, "error": None}
        finally:
            Path(patch_file).unlink(missing_ok=True)

    def _reset_repo(self):
        if not (self.repo / ".git").exists():
            return
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=self.repo,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=self.repo,
            capture_output=True,
            timeout=30,
        )
