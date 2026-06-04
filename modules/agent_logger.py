"""
Central logging — console + logs/run_report.md dashboard UI.
"""

import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_logger_instance: Optional["AgentLogger"] = None

STATUS_ICON = {
    "pending": "⏳",
    "running": "🔄",
    "ok": "✅",
    "fail": "❌",
    "skip": "⏭️",
    "warn": "⚠️",
}


class AgentLogger:
    STEPS = [
        ("1", "Issue understanding", "Fetch GitHub issue"),
        ("2", "Context builder", "Path anchors + curated grep + slice"),
        ("3", "Code reasoning", "LLM fix plan"),
        ("4", "Code generator", "LLM unified diff"),
        ("5", "Validator", "Patch matches plan"),
        ("6", "PR writer", "LLM PR summary"),
    ]

    def __init__(self, log_dir: Path, output_dir: Optional[Path] = None):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = Path(output_dir) if output_dir else self.log_dir.parent / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.live_state_path = self.log_dir / "live_state.json"
        self.html_dashboard_url: Optional[str] = None
        self._run_success: Optional[bool] = None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"agent_{ts}.log"
        self.report_path = self.log_dir / "run_report.md"
        self.dashboard_path = self.log_dir / "dashboard.md"

        self._step_state: dict[str, dict] = {
            sid: {"status": "pending", "detail": desc, "title": title}
            for sid, title, desc in self.STEPS
        }
        self._artifacts: list[tuple[str, str]] = []
        self._events: list[str] = []
        self._started = datetime.now()

        self._py_logger = logging.getLogger("open-source-issue-solver")
        self._py_logger.setLevel(logging.DEBUG)
        self._py_logger.handlers.clear()

        fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
        fh = logging.FileHandler(self.log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(logging.INFO)
        self._py_logger.addHandler(fh)
        self._py_logger.addHandler(ch)

        self._write_dashboard()
        self._print_dashboard_header()
        self._publish_live_state()

        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="dashboard-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    # ── step UI ──────────────────────────────────────────────────

    def step_start(self, step_id: str, extra: str = ""):
        if step_id in self._step_state:
            self._step_state[step_id]["status"] = "running"
            if extra:
                self._step_state[step_id]["detail"] = extra
        self._event(f"Step {step_id} started: {self._step_state.get(step_id, {}).get('title', '')}")
        self._refresh_ui()

    def step_ok(self, step_id: str, detail: str = ""):
        if step_id in self._step_state:
            self._step_state[step_id]["status"] = "ok"
            if detail:
                self._step_state[step_id]["detail"] = detail
        self._event(f"Step {step_id} OK — {detail}")
        self._refresh_ui()

    def step_fail(self, step_id: str, detail: str = ""):
        if step_id in self._step_state:
            self._step_state[step_id]["status"] = "fail"
            if detail:
                self._step_state[step_id]["detail"] = detail
        self._event(f"Step {step_id} FAILED — {detail}")
        self._refresh_ui()

    def step_skip(self, step_id: str, detail: str = ""):
        if step_id in self._step_state:
            self._step_state[step_id]["status"] = "skip"
            if detail:
                self._step_state[step_id]["detail"] = detail
        self._event(f"Step {step_id} skipped — {detail}")
        self._refresh_ui()

    def step_warn(self, step_id: str, detail: str = ""):
        if step_id in self._step_state:
            self._step_state[step_id]["status"] = "warn"
            if detail:
                self._step_state[step_id]["detail"] = detail
        self._event(f"Step {step_id} WARN — {detail}")
        self._refresh_ui()

    def artifact(self, label: str, path: str):
        self._artifacts.append((label, path))
        self._event(f"Artifact: {label} → {path}")
        self._write_dashboard()

    def diff_summary(self, patch: str, path: str):
        lines = patch.splitlines()
        files = []
        for ln in lines:
            if ln.startswith("diff --git"):
                parts = ln.split()
                if len(parts) >= 4:
                    files.append(parts[3][2:])  # b/path
        adds = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        dels = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
        has_tests = any(f.endswith("_test.go") for f in files)

        summary = (
            f"**{len(files)} file(s)** · **+{adds}** / **-{dels}** lines · "
            f"tests: **{'yes' if has_tests else 'NO'}**"
        )
        self._report_append(f"\n### Patch summary\n\n{summary}\n\n")
        self._report_append(f"Files: `{', '.join(files) or 'none'}`\n\n")
        self._report_append(f"Saved: `{path}`\n\n")
        self._event(f"Patch: {len(files)} files, +{adds}/-{dels}, tests={has_tests}")

        preview = patch if len(patch) <= 8000 else patch[:8000] + "\n... (truncated in dashboard)"
        self._report_append(f"```diff\n{preview}\n```\n")
        self._write_dashboard()

    # ── legacy API (maps to step UI) ─────────────────────────────

    def section(self, title: str, step: Optional[str] = None):
        sid = step.split("/")[0] if step and "/" in step else None
        if sid:
            self.step_start(sid, title)
        self._py_logger.info(f"[{step or '?'}] {title}")
        self._report_append(f"\n---\n\n## {title}\n\n")
        if step:
            self._report_append(f"*Pipeline step {step}*\n\n")

    def info(self, msg: str):
        self._py_logger.info(msg)
        self._report_append(f"- {msg}\n")

    def debug(self, msg: str):
        self._py_logger.debug(msg)

    def warning(self, msg: str):
        self._py_logger.warning(msg)
        self._report_append(f"- ⚠️ {msg}\n")
        self._event(f"Warning: {msg}")

    def error(self, msg: str):
        self._py_logger.error(msg)
        self._report_append(f"- ❌ {msg}\n")
        self._event(f"Error: {msg}")

    def block(self, title: str, content: str, max_report_chars: int = 12000):
        self._py_logger.debug("--- %s ---", title)
        preview = content if len(content) <= max_report_chars else (
            content[:max_report_chars]
            + f"\n... ({len(content) - max_report_chars} more in .log)"
        )
        self._report_append(f"\n#### {title}\n\n```\n{preview}\n```\n")

    def kv(self, key: str, value: Any):
        self.info(f"**{key}:** {value}")

    def llm_call(self, module: str, model: str, system_preview: str,
                  user_preview: str, response: str, duration_sec: Optional[float] = None):
        dur = f" ({duration_sec:.1f}s)" if duration_sec else ""
        self.info(f"LLM `{module}` · `{model}`{dur} · {len(response)} chars")
        self.block(f"{module} — response", response, max_report_chars=6000)

    def finalize(self, success: bool, summary: dict):
        self._heartbeat_stop.set()
        self._run_success = success
        for sid, _, _ in self.STEPS:
            if self._step_state[sid]["status"] == "running":
                self._step_state[sid]["status"] = "warn"
        self._report_append(f"\n---\n\n## Final summary\n\n")
        self._report_append(f"**Overall:** {'✅ SUCCESS' if success else '❌ ISSUES'}\n\n")
        self._report_append("| Key | Value |\n|-----|-------|\n")
        for k, v in summary.items():
            self._report_append(f"| {k} | {v} |\n")

        self._write_dashboard()
        self._flush_report()

        latest = self.log_dir / "latest.log"
        shutil.copy2(self.log_path, latest)

        self._print_dashboard_header()
        self._py_logger.info("Dashboard  : %s", self.dashboard_path)
        self._py_logger.info("Run report : %s", self.report_path)
        if self.html_dashboard_url:
            self._py_logger.info("Live UI    : %s", self.html_dashboard_url)

    # ── internals ────────────────────────────────────────────────

    def _report_append(self, text: str):
        if not hasattr(self, "_report_buf"):
            self._report_buf = [
                "# Open Source Issue Solver — Run report\n",
                f"**Started:** {self._started.isoformat()}\n\n",
                "> Live dashboard: [dashboard.md](./dashboard.md)\n\n",
            ]
        self._report_buf.append(text)

    def _flush_report(self):
        buf = getattr(self, "_report_buf", [])
        buf.append(f"\n**Finished:** {datetime.now().isoformat()}\n")
        self.report_path.write_text("".join(buf), encoding="utf-8")

    def _event(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._events.append(f"`{ts}` {msg}")
        if len(self._events) > 30:
            self._events = self._events[-30:]

    def _dashboard_table(self) -> str:
        lines = [
            "## Pipeline status\n",
            "| Step | Status | What | Detail |",
            "|------|--------|------|--------|",
        ]
        for sid, title, _ in self.STEPS:
            st = self._step_state[sid]
            icon = STATUS_ICON.get(st["status"], "?")
            lines.append(
                f"| **{sid}/6** {title} | {icon} {st['status'].upper()} "
                f"| {self.STEPS[int(sid)-1][2]} | {st.get('detail', '')} |"
            )
        return "\n".join(lines) + "\n"

    def snapshot_for_ui(self) -> dict:
        return {
            "elapsed_sec": (datetime.now() - self._started).seconds,
            "success": self._run_success,
            "steps": [
                {
                    "id": sid,
                    "title": title,
                    "description": desc,
                    "status": self._step_state[sid]["status"],
                    "detail": self._step_state[sid].get("detail", ""),
                }
                for sid, title, desc in self.STEPS
            ],
            "events": list(self._events),
            "artifacts": list(self._artifacts),
        }

    def _publish_live_state(self) -> None:
        try:
            from modules.live_state import write_live_state

            write_live_state(
                self.live_state_path,
                self.snapshot_for_ui(),
                self.log_dir,
                self.output_dir,
                current_log_path=self.log_path,
            )
        except Exception:
            pass

    def _heartbeat_loop(self) -> None:
        """Refresh live_state.json while the pipeline is active (log tail, elapsed)."""
        while not self._heartbeat_stop.wait(2.0):
            if self._run_success is not None:
                continue
            running = any(s["status"] == "running" for s in self._step_state.values())
            if running or any(s["status"] == "ok" for s in self._step_state.values()):
                self._publish_live_state()

    def _write_dashboard(self):
        body = [
            "# Live dashboard\n",
            f"**Updated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"**Elapsed:** {(datetime.now() - self._started).seconds}s\n",
        ]
        if self.html_dashboard_url:
            body.append(f"**Live UI:** {self.html_dashboard_url}\n")
        body.extend([
            "---\n",
            self._dashboard_table(),
            "\n## Artifacts\n",
        ])
        if self._artifacts:
            body.append("| Output | Path |\n|--------|------|\n")
            for label, path in self._artifacts:
                body.append(f"| {label} | `{path}` |\n")
        else:
            body.append("_No artifacts yet._\n")

        body.append("\n## Recent events\n")
        for ev in reversed(self._events[-15:]):
            body.append(f"- {ev}\n")

        body.append(f"\n---\n*Full log:* `{self.log_path.name}`\n")
        self.dashboard_path.write_text("".join(body), encoding="utf-8")
        self._publish_live_state()

    def _refresh_ui(self):
        self._write_dashboard()
        self._print_dashboard_header()

    def _print_dashboard_header(self):
        print("\n" + "─" * 56)
        print("  PIPELINE STATUS")
        print("─" * 56)
        for sid, title, _ in self.STEPS:
            st = self._step_state[sid]
            icon = STATUS_ICON.get(st["status"], "?")
            print(f"  {icon}  [{sid}/6] {title:<22} {st['status'].upper():<8}")
        print("─" * 56)
        if self._artifacts:
            print("  Outputs:", ", ".join(a[0] for a in self._artifacts[-4:]))
        print(f"  Dashboard → {self.dashboard_path}")
        if self.html_dashboard_url:
            print(f"  Live UI     → {self.html_dashboard_url}")
        print("─" * 56 + "\n")


def init_logger(log_dir: Path, output_dir: Optional[Path] = None) -> AgentLogger:
    global _logger_instance
    _logger_instance = AgentLogger(log_dir, output_dir=output_dir)
    return _logger_instance


def get_logger() -> AgentLogger:
    if _logger_instance is None:
        raise RuntimeError("Logger not initialized.")
    return _logger_instance
