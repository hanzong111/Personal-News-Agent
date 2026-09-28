"""Pipeline console HTTP server — stdlib only, read-only, loopback by default.

  python -m pipeline.dashboard [--port 9120] [--host 127.0.0.1]

Routes: see docs/dev/plan.md. /api/stream is Server-Sent Events: every new events.jsonl line is
pushed as `event: event`, and `event: tick` carries the overview every TICK_SECONDS.
"""
from __future__ import annotations
import argparse
import json
import mimetypes
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from . import data

STATIC = Path(__file__).resolve().parent / "static"
TICK_SECONDS = 5.0
POLL_SECONDS = 0.5


class Handler(BaseHTTPRequestHandler):
    server_version = "PipelineConsole/0.1"

    def log_message(self, fmt, *args):      # quiet unless it's an error
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ helpers
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel: str):
        p = (STATIC / rel).resolve()
        if not str(p).startswith(str(STATIC)) or not p.is_file():
            return self._json({"error": "not found"}, 404)
        body = p.read_bytes()
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ routes
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/overview":
                return self._json(data.overview())
            if path == "/api/runs":
                return self._json(data.runs(int(q.get("n", ["30"])[0])))
            if path.startswith("/api/runs/"):
                rid = urllib.parse.unquote(path[len("/api/runs/"):])
                evs = data.run_events(rid)
                if not evs:
                    return self._json({"error": "no such run"}, 404)
                summary = data.summarise_run(rid, evs)
                data._join_hermes([summary])
                return self._json({"run": summary, "events": evs})
            if path == "/api/health":
                return self._json(data.health(float(q.get("hours", ["24"])[0])))
            if path == "/api/spend":
                return self._json(data.spend(int(q.get("days", ["7"])[0])))
            if path == "/api/costs":
                return self._json(data.costs())
            if path == "/api/memory":
                return self._json(data.memory(int(q.get("days", ["7"])[0])))
            if path == "/api/events":
                n = int(q.get("n", ["200"])[0])
                return self._json(data.read_events(n)[-n:])
            if path == "/api/stream":
                return self._stream()
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:      # never take the page down over one bad read
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    # ------------------------------------------------------------ SSE
    def _sse(self, event: str, payload) -> None:
        msg = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        self.wfile.write(msg.encode("utf-8"))
        self.wfile.flush()

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        tail = data.Tail()
        self._sse("tick", data.overview())
        last_tick = time.time()
        while True:
            for line in tail.poll():
                try:
                    self._sse("event", json.loads(line))
                except json.JSONDecodeError:
                    continue
            if time.time() - last_tick >= TICK_SECONDS:
                self._sse("tick", data.overview())
                last_tick = time.time()
            time.sleep(POLL_SECONDS)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipeline.dashboard")
    ap.add_argument("--port", type=int, default=9120)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"pipeline console on http://{a.host}:{a.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
