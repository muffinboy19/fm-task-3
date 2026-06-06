"""
Local HTTP server for the live HTML dashboard.

  python -m modules.dashboard_server
  python -m modules.dashboard_server --port 8765
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from modules.config import PROJECT_ROOT, get_env
from modules.live_state import build_live_payload

UI_DIR = PROJECT_ROOT / "ui"
DEFAULT_PORT = 8765


def resolve_dashboard_paths() -> tuple[Path, Path]:
    log_dir = Path(get_env("LOG_DIR", "./logs")).resolve()
    if not log_dir.is_absolute():
        log_dir = (PROJECT_ROOT / log_dir).resolve()
    output_dir = Path(get_env("OUTPUT_DIR", "./output")).resolve()
    if not output_dir.is_absolute():
        output_dir = (PROJECT_ROOT / output_dir).resolve()
    return log_dir, output_dir


class DashboardHandler(BaseHTTPRequestHandler):
    log_dir: Path = Path("logs")
    output_dir: Path = Path("output")

    def log_message(self, format: str, *args) -> None:
        pass  # quiet server

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        content = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if route == "/api/state":
            live_path = self.log_dir / "live_state.json"
            snapshot: dict = {}
            current_log: Path | None = None
            if live_path.is_file():
                try:
                    cached = json.loads(live_path.read_text(encoding="utf-8"))
                    snapshot = {
                        "elapsed_sec": cached.get("elapsed_sec", 0),
                        "success": cached.get("success"),
                        "steps": cached.get("steps", []),
                        "events": cached.get("events", []),
                        "artifacts": cached.get("artifacts", []),
                    }
                    lp = cached.get("log_path")
                    if lp:
                        current_log = Path(lp)
                except json.JSONDecodeError:
                    pass
            data = build_live_payload(
                snapshot,
                self.log_dir,
                self.output_dir,
                current_log_path=current_log,
            )
            self._send_json(data)
            return

        if route in ("/", "/index.html"):
            self._send_file(UI_DIR / "index.html")
            return

        if route.startswith("/static/"):
            rel = route[len("/static/") :]
            target = (UI_DIR / "static" / rel).resolve()
            if not str(target).startswith(str((UI_DIR / "static").resolve())):
                self.send_error(403)
                return
            self._send_file(target)
            return

        self.send_error(404)


def make_handler(log_dir: Path, output_dir: Path) -> type[DashboardHandler]:
    class Handler(DashboardHandler):
        pass

    Handler.log_dir = log_dir
    Handler.output_dir = output_dir
    return Handler


def start_dashboard_server(
    port: int = DEFAULT_PORT,
    log_dir: Path | None = None,
    output_dir: Path | None = None,
    daemon: bool = True,
) -> ThreadingHTTPServer:
    log_d, out_d = log_dir, output_dir
    if log_d is None or out_d is None:
        ld, od = resolve_dashboard_paths()
        log_d = log_d or ld
        out_d = out_d or od

    handler = make_handler(log_d, out_d)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    if daemon:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    else:
        server.serve_forever()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open Source Issue Solver live dashboard"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    log_dir, output_dir = resolve_dashboard_paths()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Dashboard: {url}")
    print(f"Log dir:   {log_dir}")
    print(f"Output:    {output_dir}")
    print("Press Ctrl+C to stop.")
    start_dashboard_server(port=args.port, log_dir=log_dir, output_dir=output_dir, daemon=False)


if __name__ == "__main__":
    main()
