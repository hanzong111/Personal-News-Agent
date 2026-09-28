"""ROS2-style logging for every node in the pipeline.

    from pipeline.log import get
    log = get("agent.classifier")
    log.info("chunk 1/3", stories=60)            -> [INFO]  [2026-09-21 00:15:32.418] [agent.classifier] chunk 1/3 stories=60
    with log.span("fetch", src="klse"): ...      -> start/end lines with duration

Sinks:  stderr (never stdout — stdout is the message Hermes delivers)
        data/logs/pipeline.log   human-readable, same lines
        data/logs/events.jsonl   one JSON per line with run id + fields, for visualisation
Run id: PIPELINE_RUN env var (set by entry points via new_run("scan")), so one cron invocation = one run.

Viewer:  python -m pipeline.log tail [-n 50] [--node X] [--run X] [--level WARN]
         python -m pipeline.log runs [-n 20]
         python -m pipeline.log show <run-id>
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(os.environ.get("PIPELINE_LOG_DIR") or Path(__file__).resolve().parent.parent / "data" / "logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
TEXT = LOG_DIR / "pipeline.log"
EVENTS = LOG_DIR / "events.jsonl"
MAX_BYTES = 5_000_000
LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
STDERR_LEVEL = LEVELS.get(os.environ.get("PIPELINE_LOG_LEVEL", "INFO").upper(), 20)


def new_run(kind: str) -> str:
    """Start a run id for this process (entry points call this once)."""
    rid = os.environ.get("PIPELINE_RUN") or f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    os.environ["PIPELINE_RUN"] = rid
    return rid


def _rotate(path: Path):
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def _fmt_fields(fields: dict) -> str:
    out = []
    for k, v in fields.items():
        if isinstance(v, float):
            v = f"{v:.2f}"
        elif isinstance(v, str) and (" " in v or not v):
            v = json.dumps(v, ensure_ascii=False)
        out.append(f"{k}={v}")
    return " ".join(out)


class Logger:
    def __init__(self, node: str):
        self.node = node

    def _emit(self, level: str, msg: str, fields: dict):
        now = datetime.now()
        run = os.environ.get("PIPELINE_RUN", "-")
        line = f"[{level:<5}] [{now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] [{self.node}] {msg}"
        if fields:
            line += " " + _fmt_fields(fields)
        if LEVELS[level] >= STDERR_LEVEL:
            print(line, file=sys.stderr, flush=True)
        try:
            _rotate(TEXT); _rotate(EVENTS)
            with open(TEXT, "a", encoding="utf-8") as f:
                f.write(f"{line}  run={run}\n")
            with open(EVENTS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now.timestamp(), "level": level, "node": self.node, "run": run,
                                    "msg": msg, **fields}, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def debug(self, msg, **f): self._emit("DEBUG", msg, f)
    def info(self, msg, **f): self._emit("INFO", msg, f)
    def warn(self, msg, **f): self._emit("WARN", msg, f)
    def error(self, msg, **f): self._emit("ERROR", msg, f)

    @contextmanager
    def span(self, msg: str, **f):
        """Log 'msg …' then 'msg done' with duration; exceptions are logged and re-raised."""
        t0 = time.time()
        self.debug(f"{msg} …", **f)
        try:
            yield f
        except Exception as e:
            self.error(f"{msg} failed", err=f"{type(e).__name__}: {e}", dur=time.time() - t0, **f)
            raise
        else:
            self.info(f"{msg} done", dur=time.time() - t0, **f)


_cache: dict[str, Logger] = {}


def get(node: str) -> Logger:
    return _cache.setdefault(node, Logger(node))


# ---------------------------------------------------------------- viewer
def _events(limit_bytes: int = 20_000_000) -> list[dict]:
    if not EVENTS.exists():
        return []
    rows = []
    for l in EVENTS.read_text(encoding="utf-8").splitlines()[-200000:]:
        try:
            rows.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return rows


def _fmt_ev(e: dict, rel: float | None = None) -> str:
    t = datetime.fromtimestamp(e["ts"])
    when = f"+{e['ts'] - rel:7.2f}s" if rel is not None else t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    extra = {k: v for k, v in e.items() if k not in ("ts", "level", "node", "run", "msg")}
    return f"[{e['level']:<5}] [{when}] [{e['node']}] {e['msg']} {_fmt_fields(extra)}".rstrip()


def cmd_tail(a):
    rows = _events()
    if a.node:
        rows = [r for r in rows if r["node"].startswith(a.node)]
    if a.run:
        rows = [r for r in rows if r["run"] == a.run]
    lvl = LEVELS.get(a.level.upper(), 10)
    rows = [r for r in rows if LEVELS.get(r["level"], 20) >= lvl]
    for r in rows[-a.n:]:
        print(_fmt_ev(r))


def cmd_runs(a):
    runs: dict[str, list] = {}
    for r in _events():
        runs.setdefault(r["run"], []).append(r)
    print(f"{'run':28s} {'start':19s} {'dur':>7s} {'events':>6s} {'llm':>4s} {'tok_in':>7s} {'tok_out':>7s}  nodes / result")
    for rid, evs in list(runs.items())[-a.n:]:
        if rid == "-":
            continue
        t0, t1 = evs[0]["ts"], evs[-1]["ts"]
        llm = [e for e in evs if e["node"] == "llm" and e["msg"] == "call done"]
        tin = sum(int(e.get("tok_in") or 0) for e in llm); tout = sum(int(e.get("tok_out") or 0) for e in llm)
        nodes = sorted({e["node"].split(".")[0] for e in evs})
        errs = sum(1 for e in evs if e["level"] == "ERROR")
        res = next((e for e in reversed(evs) if e["msg"] in ("message", "silent")), None)
        tail = (res["msg"] + (f" lines={res.get('lines')}" if res and res.get("lines") else "")) if res else ""
        print(f"{rid:28s} {datetime.fromtimestamp(t0).strftime('%Y-%m-%d %H:%M:%S'):19s} {t1 - t0:6.1f}s {len(evs):6d} {len(llm):4d} {tin:7d} {tout:7d}  "
              f"{','.join(nodes)} {'ERR=' + str(errs) if errs else ''} {tail}")


def cmd_show(a):
    evs = [r for r in _events() if r["run"] == a.run or r["run"].startswith(a.run)]
    if not evs:
        print("no such run"); return
    t0 = evs[0]["ts"]
    print(f"run {evs[0]['run']}  started {datetime.fromtimestamp(t0).strftime('%Y-%m-%d %H:%M:%S')}  {evs[-1]['ts'] - t0:.1f}s\n")
    for e in evs:
        bar = ""
        if e.get("dur") is not None:
            try:
                bar = " " + "▇" * max(1, min(40, int(float(e["dur"]) * 2)))
            except (TypeError, ValueError):
                pass
        print(_fmt_ev(e, rel=t0) + bar)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipeline.log")
    sub = ap.add_subparsers(dest="cmd")
    t = sub.add_parser("tail"); t.add_argument("-n", type=int, default=50); t.add_argument("--node"); t.add_argument("--run"); t.add_argument("--level", default="DEBUG")
    r = sub.add_parser("runs"); r.add_argument("-n", type=int, default=20)
    s = sub.add_parser("show"); s.add_argument("run")
    a = ap.parse_args(argv)
    {"tail": cmd_tail, "runs": cmd_runs, "show": cmd_show}.get(a.cmd, lambda _: ap.print_help())(a)


if __name__ == "__main__":
    main()
