"""Setup: pick your stocks, a watchlist, and which messages you get when.

Terminal wizard (run once after install):

    python -m pipeline.setup

Plain commands (what the bursa-setup chat skill runs; each prints a short result):

    python -m pipeline.setup status                   portfolio + preferences + whether Hermes jobs match
    python -m pipeline.setup find <name|code>         look a stock up (code, short name, sector guess)
    python -m pipeline.setup add <name|code> [--watch] [--sector KEY] [--pick N]
    python -m pipeline.setup remove <code|short>
    python -m pipeline.setup sectors                  sector keys you can assign
    python -m pipeline.setup set digest.time=19:00 weekly=off headlines.times=08:00,20:00 ...
    python -m pipeline.setup set deliver=discord         chat app every message goes to
    python -m pipeline.setup apps                     chat apps connected in Hermes
    python -m pipeline.setup test                     send a test message to the chosen chat app
    python -m pipeline.setup apply [--dry-run]        push schedules, on/off and chat app to the Hermes jobs
    python -m pipeline.setup finish                   save preferences, apply, print the summary

Files: data/portfolio.yaml (holdings + watchlist), data/preferences.yaml (messages + times).
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from . import __version__, config, log as pipeline_log, portfolio, prefs
from .log import get as _get_log, new_run

log = _get_log("setup")
HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
# Every Hermes job this project creates (hermes/cron/create-jobs.sh). They all follow `deliver`.
PROJECT_JOBS = ("bursa-scan", "bursa-digest", "bursa-weekly", "malaysia-news", "bursa-curate", "bursa-ops",
                "lss6-watch", "cost-ledger", "chat-sweep")


def connected_apps() -> list[str]:
    """Chat platforms the Hermes gateway has connected, in Hermes' order (from channel_directory.json)."""
    f = HERMES_HOME / "channel_directory.json"
    try:
        return [str(x) for x in json.loads(f.read_text()).get("platforms") or []]
    except (OSError, ValueError):
        return []


def target(p: dict) -> str:
    """Where messages go: the user's choice, else the first connected chat app, else ''."""
    return p.get("deliver") or next(iter(connected_apps()), "")


def send_test(dest: str, run=subprocess.run) -> str:
    msg = ("✅ TickerPigeon is connected.\n"
           "Alerts, digests and reviews for your Bursa stocks will arrive here.")
    try:
        r = run(["hermes", "send", "-q", "-t", dest, msg], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return "⚠️ `hermes` is not on PATH — can't send a test message."
    if r.returncode != 0:
        return f"⚠️ Test message failed: {(r.stderr or r.stdout).strip()[-300:]}"
    return f"✓ Test message sent to {prefs.platform_name(dest)} — check your chat."


# ---------------------------------------------------------------- display

def portfolio_lines(cfg: config.Config) -> list[str]:
    out = []
    for title, items in (("Holdings", cfg.owned), ("Watchlist 👀", cfg.watched)):
        out.append(f"{title} ({len(items)}):")
        for h in items:
            label = cfg.sectors[h.sector].label if h.sector in cfg.sectors else f"{h.sector} (unknown sector!)"
            out.append(f"  {h.code} {h.short} — {h.name} · {label}")
        if not items:
            out.append("  (none)")
    return out


def onboarded(cfg: config.Config) -> bool:
    """Set up = has stocks. Message preferences are optional; without the file the defaults apply."""
    return bool(cfg.holdings)


# ---------------------------------------------------------------- Hermes cron sync

def _jobs(jobs_file: Path) -> dict[str, dict]:
    if not jobs_file.exists():
        return {}
    d = json.loads(jobs_file.read_text())
    return {j["name"]: j for j in (d["jobs"] if isinstance(d, dict) else d)}


def _paused(job: dict) -> bool:
    return bool(job.get("paused_at")) or job.get("state") == "paused" or job.get("enabled") is False


def plan_apply(p: dict, jobs: dict[str, dict]) -> tuple[list[list[str]], list[str]]:
    """-> (hermes commands to run, notes). Pure: compares preferences with the current job table."""
    cmds, notes = [], []
    if p.get("deliver"):
        for name in PROJECT_JOBS:
            job = jobs.get(name)
            if job and job.get("deliver") != p["deliver"]:
                cmds.append(["hermes", "cron", "edit", job["id"], "--deliver", p["deliver"]])
                notes.append(f"{name}: deliver → {p['deliver']}")
    for m in prefs.MESSAGES:
        name, want_on = prefs.JOB[m], p[m]["enabled"]
        job = jobs.get(name)
        if not job:
            if want_on:
                notes.append(f"⚠️ Hermes job {name} not found — create it with hermes/cron/create-jobs.sh")
            continue
        want = prefs.schedule(p, m)
        if (job.get("schedule") or {}).get("expr") != want:
            cmds.append(["hermes", "cron", "edit", job["id"], "--schedule", want])
            notes.append(f"{name}: schedule → {want}")
        if want_on and _paused(job):
            cmds.append(["hermes", "cron", "resume", job["id"]])
            notes.append(f"{name}: resume")
        elif not want_on and not _paused(job):
            cmds.append(["hermes", "cron", "pause", job["id"]])
            notes.append(f"{name}: pause")
    return cmds, notes


def apply(p: dict, dry_run: bool = False, jobs_file: Path | None = None, run=subprocess.run) -> list[str]:
    jobs_file = jobs_file or HERMES_HOME / "cron" / "jobs.json"
    cmds, notes = plan_apply(p, _jobs(jobs_file))
    if not cmds and notes and all("not found" in n for n in notes):
        return ["Saved. The Hermes jobs aren't created yet — hermes/cron/create-jobs.sh sets them to these times."]
    if not cmds:
        return notes or ["Hermes jobs already match your preferences."]
    if dry_run:
        return ["(dry run) would change:"] + notes
    for c in cmds:
        try:
            r = run(c, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            return ["⚠️ `hermes` is not on PATH — preferences saved, but the cron jobs were not updated."]
        if r.returncode != 0:
            log.warn("hermes cron failed", cmd=" ".join(c), err=(r.stderr or r.stdout)[-300:])
            notes.append(f"⚠️ failed: {' '.join(c)} — {(r.stderr or r.stdout).strip()[-200:]}")
    log.info("applied", changes=len(cmds))
    return ["Updated Hermes jobs:"] + notes


# ---------------------------------------------------------------- commands

def cmd_status(_a) -> int:
    cfg, p = config.load(), prefs.load()
    print(f"TickerPigeon {__version__}")
    print(f"Set up: {'yes' if onboarded(cfg) else 'no — run the setup flow'}")
    print("\n".join(portfolio_lines(cfg)))
    print("Messages:" + ("" if p["_exists"] else " (defaults — not chosen yet)"))
    print("\n".join("  " + line for line in prefs.describe(p)))
    apps = connected_apps()
    print("Chat apps connected in Hermes: " + (", ".join(apps) or "none — run `hermes setup gateway`"))
    if p.get("deliver") and apps and p["deliver"].split(":")[0] not in apps:
        print(f"  ⚠️ {prefs.platform_name(p['deliver'])} is not connected in Hermes — messages won't arrive")
    _, notes = plan_apply(p, _jobs(HERMES_HOME / "cron" / "jobs.json"))
    print("Hermes jobs: " + ("in sync" if not notes else "out of sync — run `setup apply`:\n  " + "\n  ".join(notes)))
    return 0


def cmd_find(a) -> int:
    cfg = config.load()
    hits = portfolio.find(a.query, cfg)
    if not hits:
        print(f"No Bursa stock found for {a.query!r}. Try the 4-digit code (see klsescreener.com).")
        return 1
    for n, s in enumerate(hits, 1):
        print(f"{n}. {s.line(cfg)}")
    return 0


def cmd_add(a) -> int:
    cfg = config.load()
    hits = portfolio.find(a.query, cfg)
    if not hits:
        print(f"No Bursa stock found for {a.query!r}. Try the 4-digit code.")
        return 1
    if a.pick:
        if not 1 <= a.pick <= len(hits):
            print(f"--pick must be 1..{len(hits)}")
            return 2
        hits = [hits[a.pick - 1]]
    exact = [s for s in hits if a.query.strip().upper() in (s.code, s.short)]
    if len(hits) > 1 and len(exact) != 1:
        print(f"Several matches for {a.query!r} — rerun with --pick N or the code:")
        for n, s in enumerate(hits, 1):
            print(f"{n}. {s.line(cfg)}")
        return 2
    stock = exact[0] if len(hits) > 1 else hits[0]
    if a.sector:
        if a.sector not in cfg.sectors:
            print(f"Unknown sector {a.sector!r}. Options: {', '.join(cfg.sectors)}")
            return 2
        stock.sector = a.sector
    data = portfolio.read()
    msg = portfolio.add(data, stock, watch=a.watch)
    portfolio.write(data)
    log.info("portfolio edit", action=msg, code=stock.code)
    print(f"✓ {msg}: {stock.line(cfg)}")
    if stock.sector == "other":
        print("  (no sector matched — news will cover the stock itself only; pick one with --sector)")
    return 0


def cmd_remove(a) -> int:
    data = portfolio.read()
    msg = portfolio.remove(data, a.key)
    if not msg:
        print(f"{a.key!r} is not in your holdings or watchlist.")
        return 1
    portfolio.write(data)
    log.info("portfolio edit", action=msg)
    print(f"✓ {msg}")
    return 0


def cmd_sectors(_a) -> int:
    cfg = config.load()
    held = {h.sector for h in cfg.holdings}
    for k, s in cfg.sectors.items():
        print(f"{k:13} {s.label}{'  ← in use' if k in held else ''}")
    return 0


def cmd_set(a) -> int:
    p = prefs.load()
    for kv in a.pairs:
        if "=" not in kv:
            print(f"expected key=value, got {kv!r}")
            return 2
        k, v = kv.split("=", 1)
        try:
            prefs.set_value(p, k, v)
        except ValueError as e:
            print(f"✗ {e}")
            return 2
    prefs.save(p)
    print("\n".join(prefs.describe(p)))
    if not a.no_apply:
        print("\n".join(apply(p)))
    return 0


def cmd_apps(_a) -> int:
    apps = connected_apps()
    for n, app in enumerate(apps, 1):
        print(f"{n}. {app} — {prefs.PLATFORMS.get(app, app)}")
    if not apps:
        print("No chat app connected in Hermes yet. Connect one: hermes setup gateway")
    return 0 if apps else 1


def cmd_test(_a) -> int:
    dest = target(prefs.load())
    if not dest:
        print("No chat app chosen or connected. Connect one with `hermes setup gateway`, then `setup set deliver=<app>`.")
        return 1
    out = send_test(dest)
    print(out)
    return 0 if out.startswith("✓") else 1


def cmd_target(_a) -> int:
    """Print the delivery target for scripts (create-jobs.sh); empty + exit 1 when there is none."""
    dest = target(prefs.load())
    print(dest)
    return 0 if dest else 1


def cmd_apply(a) -> int:
    print("\n".join(apply(prefs.load(), dry_run=a.dry_run)))
    return 0


def summary(cfg: config.Config, p: dict) -> list[str]:
    return ["Your newsletter is set:", *portfolio_lines(cfg), "Messages:", *("  " + x for x in prefs.describe(p))]


def cmd_finish(_a) -> int:
    cfg, p = config.load(), prefs.load()
    if not cfg.holdings:
        print("Add at least one stock first (setup add <name|code>).")
        return 1
    prefs.save(p)
    print("\n".join(summary(cfg, p)))
    print("\n".join(apply(p)))
    return 0


# ---------------------------------------------------------------- terminal wizard

class Wizard:
    def __init__(self, ask=input, say=print, apply_fn=apply, send_fn=send_test):
        self.ask, self.say, self.apply_fn, self.send_fn = ask, say, apply_fn, send_fn

    def _q(self, prompt: str, default: str = "") -> str:
        v = self.ask(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        return v or default

    def _yes(self, prompt: str, default: bool = True) -> bool:
        v = self.ask(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not v else v.startswith("y")

    def _pick_sector(self, cfg: config.Config, stock: portfolio.Stock) -> None:
        keys = list(cfg.sectors)
        while True:
            v = self._q(f"    Sector — Enter to keep '{cfg.sectors[stock.sector].label}', or type a key "
                        f"('?' lists them)").strip().lower()
            if v in ("", stock.sector):
                return
            if v == "?":
                self.say("    " + ", ".join(keys))
            elif v in cfg.sectors:
                stock.sector = v
                return
            else:
                self.say(f"    Unknown sector {v!r}.")

    def _collect(self, cfg: config.Config, data: dict, watch: bool) -> None:
        while True:
            q = self._q("  Stock name or code (Enter when done)")
            if not q:
                return
            hits = portfolio.find(q, cfg)
            if not hits:
                self.say("    Not found. Try the 4-digit Bursa code (klsescreener.com shows it).")
                continue
            stock = hits[0]
            if len(hits) > 1:
                for n, s in enumerate(hits[:6], 1):
                    self.say(f"    {n}. {s.line(cfg)}")
                v = self._q("    Which one? (number, Enter to skip)")
                if not v.isdigit() or not 1 <= int(v) <= min(len(hits), 6):
                    continue
                stock = hits[int(v) - 1]
            else:
                self.say(f"    ✓ {stock.line(cfg)}")
            self._pick_sector(cfg, stock)
            self.say(f"    {portfolio.add(data, stock, watch=watch)}")

    def _chat_app(self, p: dict) -> None:
        apps = connected_apps()
        current = p.get("deliver") or next(iter(apps), "telegram")
        if apps:
            self.say("  Connected in Hermes: " + "  ".join(f"{n}. {prefs.platform_name(a)}" for n, a in enumerate(apps, 1)))
        else:
            self.say("  No chat app is connected in Hermes yet (connect one later with `hermes setup gateway`).")
        self.say("  Type a number, an app name (telegram, discord, slack, whatsapp, signal, feishu …),"
                 " or app:chat_id for a specific chat.")
        while True:
            v = self._q("  Send my messages to", current)
            if v.isdigit() and 1 <= int(v) <= len(apps):
                v = apps[int(v) - 1]
            try:
                prefs.set_value(p, "deliver", v)
            except ValueError as e:
                self.say(f"    ✗ {e}")
                continue
            if apps and p["deliver"].split(":")[0] not in apps:
                self.say(f"    ⚠️ {prefs.platform_name(p['deliver'])} isn't connected in Hermes yet — "
                         "run `hermes setup gateway` so messages can arrive.")
            return

    def _messages(self, p: dict) -> None:
        for m in prefs.MESSAGES:
            self.say(f"\n  {prefs.TITLE[m]} — {prefs.BLURB[m]}")
            p[m]["enabled"] = self._yes("    Receive these?", p[m]["enabled"])
            if not p[m]["enabled"]:
                continue
            while True:
                try:
                    if m == "alerts":
                        a, b = p[m]["hours"]
                        prefs.set_value(p, "alerts.hours", self._q("    Alert hours, first-last (scans at :00 and :30)", f"{a}-{b}"))
                        prefs.set_value(p, "alerts.days", self._q("    Days (weekdays/daily)", p[m]["days"]))
                    elif m == "digest":
                        prefs.set_value(p, "digest.time", self._q("    Time", p[m]["time"]))
                        prefs.set_value(p, "digest.days", self._q("    Days (weekdays/daily)", p[m]["days"]))
                    elif m == "weekly":
                        prefs.set_value(p, "weekly.day", self._q("    Day (mon..sun)", p[m]["day"]))
                        prefs.set_value(p, "weekly.time", self._q("    Time", p[m]["time"]))
                    else:
                        prefs.set_value(p, "headlines.times", self._q("    Times, comma-separated", ",".join(p[m]["times"])))
                    break
                except ValueError as e:
                    self.say(f"    ✗ {e}")

    def run(self) -> int:
        cfg, p, data = config.load(), prefs.load(), portfolio.read()
        self.say("TickerPigeon — setup\n"
                 "Four steps: the stocks you hold, a watchlist, your chat app, and which messages you want when.\n")
        if data["holdings"] or data["watchlist"]:
            self.say("\n".join(portfolio_lines(cfg)))
            if not self._yes("Keep these and add to them?", True):
                data = {"holdings": [], "watchlist": []}
        self.say("\nStep 1/4 · Stocks you hold")
        self._collect(cfg, data, watch=False)
        self.say("\nStep 2/4 · Watchlist — stocks you don't hold but want news on (alerts marked 👀)")
        self._collect(cfg, data, watch=True)
        if not data["holdings"] and not data["watchlist"]:
            self.say("\nNo stocks chosen — nothing saved.")
            return 1
        self.say("\nStep 3/4 · Chat app — where your messages arrive")
        self._chat_app(p)
        self.say("\nStep 4/4 · Which messages, and when (Malaysia time)")
        self._messages(p)

        portfolio.write(data)
        prefs.save(p)
        cfg = config.load()
        self.say("\n" + "\n".join(summary(cfg, p)))
        if self._yes("\nApply these times and chat app to the Hermes cron jobs now?", True):
            self.say("\n".join(self.apply_fn(p)))
        else:
            self.say("Later: python -m pipeline.setup apply")
        if self._yes(f"Send a test message to {prefs.platform_name(p['deliver'])}?", True):
            self.say(self.send_fn(p["deliver"]))
        self.say("\nTip: `python -m pipeline.scan --dry-run` shows what would alert right now.")
        return 0


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.setup", description="Portfolio + news preference setup.")
    ap.add_argument("--version", action="version", version=f"TickerPigeon {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    f = sub.add_parser("find"); f.add_argument("query")
    ad = sub.add_parser("add"); ad.add_argument("query"); ad.add_argument("--watch", action="store_true")
    ad.add_argument("--sector"); ad.add_argument("--pick", type=int)
    rm = sub.add_parser("remove"); rm.add_argument("key")
    sub.add_parser("sectors")
    st = sub.add_parser("set"); st.add_argument("pairs", nargs="+"); st.add_argument("--no-apply", action="store_true")
    apl = sub.add_parser("apply"); apl.add_argument("--dry-run", action="store_true")
    sub.add_parser("finish")
    sub.add_parser("apps")
    sub.add_parser("test")
    sub.add_parser("target")
    sub.add_parser("wizard")
    a = ap.parse_args(argv)
    # Output is read by a person or by the chat agent: keep INFO lines in the log files, off stderr.
    pipeline_log.STDERR_LEVEL = max(pipeline_log.STDERR_LEVEL, pipeline_log.LEVELS["WARN"])
    new_run("setup")
    if a.cmd in (None, "wizard"):
        if not sys.stdin.isatty() and a.cmd is None:
            ap.print_help()
            return 2
        try:
            return Wizard().run()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled — nothing saved.")
            return 130
    return {"status": cmd_status, "find": cmd_find, "add": cmd_add, "remove": cmd_remove, "sectors": cmd_sectors,
            "set": cmd_set, "apply": cmd_apply, "finish": cmd_finish, "apps": cmd_apps, "test": cmd_test,
            "target": cmd_target}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
