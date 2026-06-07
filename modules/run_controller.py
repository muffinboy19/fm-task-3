"""
Start pipeline runs from the local dashboard (background subprocess).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from modules.config import PROJECT_ROOT, project_python, project_venv_env
from modules.live_state import clear_dashboard_state


class RunController:
    def __init__(self, log_dir: Path, output_dir: Path) -> None:
        self.log_dir = Path(log_dir)
        self.output_dir = Path(output_dir)
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._current_issue: str = ""
        self._last_issue: str = ""
        self._started_at: float = 0.0
        self._exit_code: Optional[int] = None
        self._watcher = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher.start()

    def _watch_loop(self) -> None:
        while True:
            with self._lock:
                proc = self._process
            if proc is not None and proc.poll() is not None:
                with self._lock:
                    if self._process is proc:
                        self._exit_code = proc.poll()
                        self._process = None
            time.sleep(0.5)

    def _launch_locked(self, issue_url: str) -> None:
        issue_url = issue_url.strip()
        if not issue_url:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        clear_dashboard_state(self.log_dir, self.output_dir)

        cmd = [
            project_python(),
            str(PROJECT_ROOT / "main.py"),
            "--issue",
            issue_url,
            "--no-ui",
            "--log-dir",
            str(self.log_dir),
            "--output",
            str(self.output_dir),
        ]
        env = project_venv_env()
        env["GITHUB_ISSUE_URL"] = issue_url
        env["LOG_DIR"] = str(self.log_dir)
        env["OUTPUT_DIR"] = str(self.output_dir)

        self._current_issue = issue_url
        self._last_issue = issue_url
        self._started_at = time.time()
        self._exit_code = None
        self._process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env)

    def start(self, issue_url: str) -> dict:
        url = (issue_url or "").strip()
        if not url:
            raise ValueError("Issue URL is required")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A run is already in progress")
            self._launch_locked(url)
        return self.status()

    def retry(self) -> dict:
        with self._lock:
            target = self._last_issue or self._current_issue
            if not target:
                raise ValueError("No previous issue to retry")
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A run is already in progress")
            self._launch_locked(target)
        return self.status()

    def reset_display(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("Cannot reset while a run is in progress")
        clear_dashboard_state(self.log_dir, self.output_dir)

    def status(self) -> dict:
        with self._lock:
            proc = self._process
            running = proc is not None and proc.poll() is None
            elapsed = int(time.time() - self._started_at) if running and self._started_at else 0
            return {
                "running": running,
                "current_issue": self._current_issue if running else "",
                "last_issue": self._last_issue,
                "pid": proc.pid if running and proc else None,
                "exit_code": self._exit_code,
                "elapsed_sec": elapsed,
            }
