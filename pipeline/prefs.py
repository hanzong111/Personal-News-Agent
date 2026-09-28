"""News preferences: which messages the user gets, and when. Source of truth is data/preferences.yaml.

Four message types, each backed by one Hermes cron job:

  alerts     bursa-scan     instant holding/watchlist alerts, a scan every 30 min inside a window
  digest     bursa-digest   evening sector/macro digest
  weekly     bursa-weekly   weekly review
  headlines  malaysia-news  general Malaysian headline index, one or more editions a day

Plus `deliver`: the Hermes delivery target every job of this project sends to — the chat app the user
picked (`telegram`, `discord`, `slack`, `whatsapp`, `signal`, `feishu`, …, or `platform:chat_id`).

Delivering jobs fire LEAD minutes early and `hold.until_target(<message>)` holds stdout until the
chosen time, so the message lands on it. `pipeline.setup apply` pushes these schedules and on/off
states to the Hermes jobs; this module only computes them.
"""
from __future__ import annotations
import copy
import re
from datetime import date
from pathlib import Path
import yaml
from . import config

MESSAGES = ("alerts", "digest", "weekly", "headlines")
JOB = {"alerts": "bursa-scan", "digest": "bursa-digest", "weekly": "bursa-weekly", "headlines": "malaysia-news"}
TITLE = {"alerts": "Instant alerts", "digest": "Evening digest", "weekly": "Weekly review", "headlines": "Malaysia headlines"}
BLURB = {
    "alerts": "news naming a stock you hold or watch, checked every 30 min",
    "digest": "sector and market news touching your stocks, once a day",
    "weekly": "your stocks' week vs the KLCI, what moved and why, what to watch",
    "headlines": "general Malaysian news index (politics, economy, policy …)",
}
# Minutes each job fires before its delivery time (runtime buffer; see docs in hold.py).
LEAD = {"digest": 5, "weekly": 5, "headlines": 10}
DAYS = {"weekdays": "1-5", "daily": "*"}
# Chat apps Hermes can deliver to, with display names. Any `platform:chat_id` target is accepted too.
PLATFORMS = {"telegram": "Telegram", "discord": "Discord", "slack": "Slack", "whatsapp": "WhatsApp",
             "signal": "Signal", "feishu": "Feishu / Lark", "weixin": "WeChat", "matrix": "Matrix",
             "mattermost": "Mattermost", "email": "Email", "sms": "SMS"}
DELIVER_RE = re.compile(r"^[a-z][a-z0-9_-]*(:.+)?$")
WEEKDAY = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
WEEKDAY_NAME = {v: k.capitalize() for k, v in WEEKDAY.items()}

DEFAULTS = {
    "alerts": {"enabled": True, "days": "weekdays", "hours": [8, 18]},
    "digest": {"enabled": True, "days": "weekdays", "time": "18:30"},
    "weekly": {"enabled": True, "day": "fri", "time": "20:00"},
    "headlines": {"enabled": True, "times": ["09:00", "14:00", "21:00"]},
}

HEADER = """\
# News preferences: which messages you get and when (local time, Malaysia).
# Written by `python -m pipeline.setup` (or the bursa-setup chat skill). Edit by hand if you like,
# then run `python -m pipeline.setup apply` so the Hermes cron jobs follow.
#
# alerts     scan every 30 min between hours[0]:00 and hours[1]:30; days: weekdays | daily
# digest     time HH:MM; days: weekdays | daily
# weekly     day mon..sun; time HH:MM
# headlines  times: list of HH:MM, all on the same minute (e.g. all :00)
# deliver    chat app for every message: telegram | discord | slack | whatsapp | signal | feishu | …
# jev        true = use TypeSafe's Jev to catch repeat stories, off-topic mentions and story types
#            (needs TYPESAFE_API_KEY; see docs/benchmarks/jev.md). false = the original rules only.
#            (or platform:chat_id for a specific chat). Must be connected in Hermes (`hermes setup gateway`).
"""


# ---------------------------------------------------------------- load / save

def load(path: Path | None = None) -> dict:
    """Preferences merged over the defaults. `prefs["_exists"]` says whether the file was there."""
    path = path or config.PREFS_FILE
    raw = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    prefs = copy.deepcopy(DEFAULTS)
    for m in MESSAGES:
        prefs[m].update((raw.get("messages") or {}).get(m) or {})
    prefs["deliver"] = raw.get("deliver") or None          # None = not chosen yet
    prefs["jev"] = parse_bool(raw.get("jev", False))
    prefs["_exists"] = path.exists()
    prefs["_onboarded"] = raw.get("onboarded")
    validate(prefs)
    return prefs


def save(prefs: dict, path: Path | None = None, onboarded: str | None = None) -> Path:
    path = path or config.PREFS_FILE
    validate(prefs)
    flow = lambda v: yaml.safe_dump(v, default_flow_style=True, width=10_000).strip().removesuffix("\n...")
    lines = [HEADER, "messages:"]
    for m in MESSAGES:
        lines.append(f"  {m}:")
        lines += [f"    {k}: {flow(v)}" for k, v in prefs[m].items()]
    if prefs.get("deliver"):
        lines.append(f"deliver: {flow(prefs['deliver'])}")
    lines.append(f"jev: {'true' if prefs.get('jev') else 'false'}")
    lines.append(f"onboarded: {flow(str(onboarded or prefs.get('_onboarded') or date.today().isoformat()))}")
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- parsing + validation

def parse_time(v) -> str:
    """'19:00', '7pm', '7:30 pm', '1930', 1900 -> 'HH:MM'."""
    s = str(v).strip().lower().replace(".", ":").replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm)?", s)
    if not m:
        raise ValueError(f"not a time: {v!r} (use HH:MM, e.g. 18:30 or 6:30pm)")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            raise ValueError(f"not a time: {v!r}")
        h = h % 12 + (12 if ap == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"not a time: {v!r}")
    return f"{h:02d}:{mi:02d}"


def parse_bool(v) -> bool:
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "enable", "enabled"):
        return True
    if s in ("0", "false", "no", "n", "off", "disable", "disabled"):
        return False
    raise ValueError(f"expected on/off, got {v!r}")


def parse_hours(v) -> list[int]:
    """'8-18', '08:00-18:30', [8, 18] -> [8, 18]: scans run at :00 and :30 of every hour in the range."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        a, b = int(v[0]), int(v[1])
    else:
        parts = re.split(r"\s*(?:-|–|to)\s*", str(v).strip())
        if len(parts) != 2:
            raise ValueError(f"alert hours must look like 8-18, got {v!r}")
        a, b = (int(parse_time(p)[:2]) for p in parts)
    if not (0 <= a <= b <= 23):
        raise ValueError(f"alert hours must be within 0-23 with start <= end, got {v!r}")
    return [a, b]


def validate(prefs: dict) -> None:
    """Normalise in place; raise ValueError with a user-facing message on bad values."""
    for m in MESSAGES:
        p = prefs[m]
        p["enabled"] = parse_bool(p["enabled"])
        if "days" in p and p["days"] not in DAYS:
            raise ValueError(f"{m}.days must be one of {', '.join(DAYS)}, got {p['days']!r}")
    prefs["alerts"]["hours"] = parse_hours(prefs["alerts"]["hours"])
    prefs["digest"]["time"] = parse_time(prefs["digest"]["time"])
    prefs["weekly"]["time"] = parse_time(prefs["weekly"]["time"])
    day = str(prefs["weekly"]["day"]).strip().lower()[:3]
    if day not in WEEKDAY:
        raise ValueError(f"weekly.day must be mon..sun, got {prefs['weekly']['day']!r}")
    prefs["weekly"]["day"] = day
    times = prefs["headlines"]["times"]
    times = [t for t in (times.split(",") if isinstance(times, str) else times) if str(t).strip()]
    if not times:
        raise ValueError("headlines.times needs at least one time (or turn headlines off)")
    times = sorted({parse_time(t) for t in times})
    if len({t[3:] for t in times}) > 1:
        raise ValueError(f"headline times must all be on the same minute (e.g. 09:00, 14:00, 21:00), got {', '.join(times)}")
    prefs["headlines"]["times"] = times
    if prefs.get("deliver") is not None:
        prefs["deliver"] = parse_deliver(prefs["deliver"])


def parse_deliver(v) -> str:
    """'Telegram' -> 'telegram'; 'discord:#alerts' kept; 'Feishu / Lark' -> 'feishu'."""
    s = str(v).strip()
    if ":" not in s:
        s = s.lower()
        s = next((k for k, name in PLATFORMS.items() if s == name.lower()), s).replace(" ", "")
    else:
        head, rest = s.split(":", 1)
        s = f"{head.strip().lower()}:{rest.strip()}"
    if not DELIVER_RE.match(s):
        raise ValueError(f"not a chat app / delivery target: {v!r} (e.g. telegram, discord, telegram:-100123)")
    return s


def platform_name(target: str | None) -> str:
    if not target:
        return "not chosen"
    head, _, rest = target.partition(":")
    name = PLATFORMS.get(head, head.capitalize())
    return f"{name} ({rest})" if rest else name


SETTABLE = {
    "alerts.enabled", "alerts.days", "alerts.hours",
    "digest.enabled", "digest.days", "digest.time",
    "weekly.enabled", "weekly.day", "weekly.time",
    "headlines.enabled", "headlines.times", "deliver", "jev",
}


def set_value(prefs: dict, key: str, value) -> None:
    """set_value(p, 'digest.time', '7pm'); a bare message name sets .enabled ('weekly', 'off')."""
    key = key.strip().lower()
    if key in MESSAGES:
        key += ".enabled"
    if key == "jev":
        prefs["jev"] = parse_bool(value)
        return
    if key in ("deliver", "app", "chat", "platform"):
        prefs["deliver"] = parse_deliver(value)
        return
    if key not in SETTABLE:
        raise ValueError(f"unknown setting {key!r}; settable: {', '.join(sorted(SETTABLE))}")
    m, field_ = key.split(".")
    before = copy.deepcopy(prefs[m])
    prefs[m][field_] = value
    try:
        validate(prefs)
    except ValueError:
        prefs[m] = before
        raise


# ---------------------------------------------------------------- schedules

def _hm(t: str) -> tuple[int, int]:
    return int(t[:2]), int(t[3:])


def _shift_dow(dow: str, days: int) -> str:
    if dow == "*" or days == 0:
        return dow
    if "-" in dow:
        a, b = (int(x) for x in dow.split("-"))
        a, b = (a + days) % 7, (b + days) % 7
        return f"{a}-{b}" if a <= b else ",".join(str(d % 7) for d in range(a, b + 8))
    return str((int(dow) + days) % 7)


def _fire(t: str, lead: int) -> tuple[int, int, int]:
    """Fire time for delivery `t` with `lead` minutes early -> (hour, minute, day shift)."""
    h, mi = _hm(t)
    mins = h * 60 + mi - lead
    shift = -1 if mins < 0 else 0
    mins %= 1440
    return mins // 60, mins % 60, shift


def schedule(prefs: dict, message: str) -> str:
    """Cron expression for the Hermes job behind `message`."""
    p = prefs[message]
    if message == "alerts":
        a, b = p["hours"]
        return f"*/30 {a}-{b} * * {DAYS[p['days']]}"
    if message == "digest":
        h, mi, shift = _fire(p["time"], LEAD["digest"])
        return f"{mi} {h} * * {_shift_dow(DAYS[p['days']], shift)}"
    if message == "weekly":
        h, mi, shift = _fire(p["time"], LEAD["weekly"])
        return f"{mi} {h} * * {_shift_dow(str(WEEKDAY[p['day']]), shift)}"
    fires = [_fire(t, LEAD["headlines"]) for t in p["times"]]
    hours = ",".join(str(h) for h in sorted({h for h, _, _ in fires}))
    return f"{fires[0][1]} {hours} * * *"


def deliver_at(prefs: dict, message: str) -> str | None:
    """Hold target(s) for hold.until_target: 'HH:MM' or 'HH:MM,HH:MM'. Alerts are never held."""
    p = prefs[message]
    if message == "digest" or message == "weekly":
        return p["time"]
    if message == "headlines":
        return ",".join(p["times"])
    return None


def describe(prefs: dict) -> list[str]:
    """One line per message type, for the wizard, `setup status` and chat."""
    out = [f"📨 Sent to — {platform_name(prefs.get('deliver'))}",
           f"🧠 Jev smart filtering — {'on' if prefs.get('jev') else 'off'}"]
    for m in MESSAGES:
        p = prefs[m]
        days = "Mon–Fri" if p.get("days") == "weekdays" else "daily"
        if m == "alerts":
            when = f"every 30 min, {p['hours'][0]:02d}:00–{p['hours'][1]:02d}:30, {days}"
        elif m == "digest":
            when = f"{p['time']}, {days}"
        elif m == "weekly":
            when = f"{p['day'].capitalize()} {p['time']}"
        else:
            when = " · ".join(p["times"]) + ", daily"
        out.append(f"{'✅' if p['enabled'] else '⛔'} {TITLE[m]} — {when if p['enabled'] else 'off'}")
    return out
