"""
Local HTTP server for the live HTML dashboard.

  python main.py          # UI — paste issue URL and run
  python -m modules.dashboard_server
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from modules.config import PROJECT_ROOT, get_env
from modules.live_state import build_live_payload, clear_dashboard_state
from modules.run_controller import RunController

UI_DIR = PROJECT_ROOT / "ui"
DEFAULT_PORT = 8765


class ReuseHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def bind_http_server(
    handler: type,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    max_tries: int = 10,
) -> tuple[ReuseHTTPServer, int]:
    last_err: OSError | None = None
    for offset in range(max_tries):
        try_port = port + offset
        try:
            server = ReuseHTTPServer((host, try_port), handler)
            return server, try_port
        except OSError as e:
            if e.errno != 48:
                raise
            last_err = e
    msg = (
        f"Ports {port}–{port + max_tries - 1} are in use. "
        f"Stop the old server (Ctrl+C in that terminal) or run:\n"
        f"  lsof -ti:{port} | xargs kill"
    )
    raise OSError(last_err.errno, msg) from last_err


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
    run_controller: RunController | None = None

    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

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

    def _build_state(self) -> dict:
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
        if self.run_controller:
            data["job"] = self.run_controller.status()
        return data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if route == "/api/state":
            self._send_json(self._build_state())
            return

        if route == "/api/job":
            job = self.run_controller.status() if self.run_controller else {}
            self._send_json(job)
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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        body = self._read_json_body()

        if not self.run_controller:
            self._send_json({"error": "Run controller not available"}, status=503)
            return

        if route == "/api/run":
            try:
                status = self.run_controller.start(body.get("issue_url", ""))
                self._send_json(status)
            except (ValueError, RuntimeError) as e:
                self._send_json({"error": str(e)}, status=400)
            return

        if route == "/api/retry":
            try:
                status = self.run_controller.retry()
                self._send_json(status)
            except (ValueError, RuntimeError) as e:
                self._send_json({"error": str(e)}, status=400)
            return

        if route == "/api/reset":
            try:
                self.run_controller.reset_display()
                self._send_json({"ok": True})
            except RuntimeError as e:
                self._send_json({"error": str(e)}, status=400)
            return

        self.send_error(404)


def make_handler(
    log_dir: Path, output_dir: Path, run_controller: RunController | None
) -> type[DashboardHandler]:
    class Handler(DashboardHandler):
        pass

    Handler.log_dir = log_dir
    Handler.output_dir = output_dir
    Handler.run_controller = run_controller
    return Handler


def start_dashboard_server(
    port: int = DEFAULT_PORT,
    log_dir: Path | None = None,
    output_dir: Path | None = None,
    daemon: bool = True,
    run_controller: RunController | None = None,
) -> ThreadingHTTPServer:
    log_d, out_d = log_dir, output_dir
    if log_d is None or out_d is None:
        ld, od = resolve_dashboard_paths()
        log_d = log_d or ld
        out_d = out_d or od

    if run_controller is None and not daemon:
        run_controller = RunController(log_d, out_d)

    handler = make_handler(log_d, out_d, run_controller)
    server, bound_port = bind_http_server(handler, port=port)
    if daemon:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    else:
        server.serve_forever()
    return server


def run_serve_mode(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    log_dir, output_dir = resolve_dashboard_paths()
    log_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    clear_dashboard_state(log_dir, output_dir)
    controller = RunController(log_dir, output_dir)
    handler = make_handler(log_dir, output_dir, controller)
    server, bound_port = bind_http_server(handler, port=port)
    url = f"http://127.0.0.1:{bound_port}/"
    print(f"Alaph UI:  {url}")
    if bound_port != port:
        print(f"Note:      port {port} was busy, using {bound_port}")
    print(f"Log dir:   {log_dir}")
    print(f"Output:    {output_dir}")
    print("Paste a GitHub issue URL in the browser, then click Run. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Open Source Issue Solver UI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="Do not open browser")
    args = parser.parse_args()
    run_serve_mode(port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
