"""
Serialize pipeline + output artifacts for the live HTML dashboard.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _read_text(path: Path, max_chars: int = 0) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if max_chars and len(text) > max_chars:
            return text[:max_chars] + f"\n… ({len(text) - max_chars} more chars)"
        return text
    except OSError:
        return ""


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def parse_unified_diff(patch: str) -> list[dict]:
    """Split patch into per-file hunks for the UI diff viewer."""
    if not patch.strip():
        return []
    files: list[dict] = []
    current: Optional[dict] = None
    hunk_lines: list[str] = []
    hunk_header = ""

    def flush_hunk() -> None:
        nonlocal hunk_lines, hunk_header
        if current is not None and (hunk_header or hunk_lines):
            current["hunks"].append({
                "header": hunk_header,
                "lines": hunk_lines[:],
            })
        hunk_lines = []
        hunk_header = ""

    for line in patch.splitlines():
        if line.startswith("diff --git"):
            flush_hunk()
            if current:
                files.append(current)
            parts = line.split()
            b_path = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else parts[-1]
            current = {"path": b_path, "hunks": []}
            continue
        if line.startswith("@@") and current is not None:
            flush_hunk()
            hunk_header = line
            continue
        if current is not None and (
            line.startswith("+")
            or line.startswith("-")
            or line.startswith(" ")
            or line == r"\ No newline at end of file"
        ):
            hunk_lines.append(line)
    flush_hunk()
    if current:
        files.append(current)
    return files


def patch_stats(patch: str) -> dict:
    lines = patch.splitlines()
    files = [ln for ln in lines if ln.startswith("diff --git")]
    adds = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return {
        "file_count": len(files),
        "additions": adds,
        "deletions": dels,
        "has_tests": any("_test.go" in ln for ln in files),
    }


def build_live_payload(
    logger_snapshot: dict,
    log_dir: Path,
    output_dir: Path,
    log_tail_lines: int = 250,
    current_log_path: Optional[Path] = None,
) -> dict:
    output_dir = Path(output_dir)
    log_dir = Path(log_dir)

    latest_log = Path(current_log_path) if current_log_path else log_dir / "latest.log"
    if not latest_log.is_file():
        agents = sorted(log_dir.glob("agent_*.log"), key=lambda p: p.stat().st_mtime)
        latest_log = agents[-1] if agents else latest_log

    log_tail = ""
    if latest_log.is_file():
        try:
            lines = latest_log.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-log_tail_lines:])
        except OSError:
            pass

    patch = _read_text(output_dir / "fix.patch")
    plan = _read_text(output_dir / "plan.md", max_chars=120_000)
    pr = _read_text(output_dir / "pr_summary.md", max_chars=80_000)

    validation = _read_json(output_dir / "validation_report.json")

    elapsed = logger_snapshot.get("elapsed_sec", 0)
    steps = logger_snapshot.get("steps", [])
    any_running = any(s.get("status") == "running" for s in steps)

    return {
        "updated_at": datetime.now().isoformat(),
        "elapsed_sec": elapsed,
        "any_running": any_running,
        "success": logger_snapshot.get("success"),
        "steps": logger_snapshot.get("steps", []),
        "events": logger_snapshot.get("events", []),
        "artifacts": logger_snapshot.get("artifacts", []),
        "log_path": str(latest_log) if latest_log.is_file() else "",
        "log_tail": log_tail,
        "output_dir": str(output_dir.resolve()),
        "log_dir": str(log_dir.resolve()),
        "files": {
            "run_summary": _read_json(output_dir / "run_summary.json"),
            "issue_raw": _read_json(output_dir / "issue_raw.json"),
            "issue_understanding": _read_json(output_dir / "issue_understanding.json"),
            "context": _read_json(output_dir / "context.json"),
            "validation": validation,
            "plan_md": plan,
            "pr_summary_md": pr,
            "patch_raw": patch,
            "patch_files": parse_unified_diff(patch),
            "patch_stats": patch_stats(patch),
        },
    }


def write_live_state(
    path: Path,
    logger_snapshot: dict,
    log_dir: Path,
    output_dir: Path,
    current_log_path: Optional[Path] = None,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_live_payload(
        logger_snapshot,
        log_dir,
        output_dir,
        current_log_path=current_log_path,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
