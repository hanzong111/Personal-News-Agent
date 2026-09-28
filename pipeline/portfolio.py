"""Portfolio file editing + Bursa stock lookup, used by `pipeline.setup` (terminal wizard and chat skill).

Lookup uses Yahoo Finance's public search (code, Bursa short name, company name, industry), falling
back to the chart endpoint for a bare code. From that we build a ready-to-use portfolio entry:
aliases for headline matching, a Google News query, and a suggested sector from sectors.yaml `match`.

data/portfolio.yaml keeps two lists with the same fields:
  holdings   stocks you own
  watchlist  stocks you don't own but want news on (same alerts, marked 👀)
"""
from __future__ import annotations
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
import yaml
from . import config
from .log import get as _get_log

log = _get_log("setup")

SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
CODE_RE = re.compile(r"^\d{4}(SS)?$")      # Bursa stock codes; SS = stapled securities (5235SS KLCC)
SECTIONS = ("holdings", "watchlist")
# Trailing words dropped to get the name a headline would use ("Top Glove Corporation Bhd" -> "Top Glove").
GENERIC = {"bhd", "berhad", "holdings", "holding", "group", "corporation", "corp", "company", "co", "(m)",
           "(malaysia)", "malaysia", "international", "industries", "technologies", "resources", "limited", "ltd"}

HEADER = """\
# Your stocks — the agent's source of truth. Every pipeline run reads this fresh.
# Written by `python -m pipeline.setup` (or the bursa-setup chat skill); editing by hand is fine too.
#
# holdings   stocks you own
# watchlist  stocks you don't own but want news on (same instant alerts, marked 👀)
#
# code      Bursa stock code (used for KLSE Screener + Bursa API lookups)
# short     Bursa short name
# name      full company name
# sector    key into sectors.yaml (which industry themes apply)
# aliases   strings that identify this company in a headline (case-insensitive;
#           a trailing space forces a whole-word match, for short names like "IJM ")
# queries   Google News search queries for stock-specific news
# qty / avg_cost   optional, for your own reference (not used for alerts)
"""


@dataclass
class Stock:
    code: str
    short: str
    name: str
    industry: str = ""
    sector: str = "other"
    aliases: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    def entry(self) -> dict:
        return {"code": self.code, "short": self.short, "name": self.name, "sector": self.sector,
                "aliases": self.aliases, "queries": self.queries}

    def line(self, cfg: config.Config | None = None) -> str:
        label = cfg.sectors[self.sector].label if cfg and self.sector in cfg.sectors else self.sector
        ind = f" (Yahoo: {self.industry})" if self.industry else ""
        return f"{self.code} {self.short} — {self.name} · sector: {label}{ind}"


# ---------------------------------------------------------------- lookup

def _get_json(url: str, params: dict) -> dict:
    from curl_cffi import requests
    r = requests.get(url, params=params, impersonate="chrome", timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def tidy_name(name: str) -> str:
    """'Gamuda Berhad' -> 'Gamuda Bhd'; 'Top Glove Corporation Bhd.' -> 'Top Glove Corporation Bhd'."""
    name = re.sub(r"\s+", " ", name or "").strip().rstrip(".")
    return re.sub(r"\bBerhad$", "Bhd", name)


def core_name(name: str) -> str:
    """The name a headline would use: generic trailing words dropped, REITs shortened."""
    name = tidy_name(name).replace("Real Estate Investment Trust", "REIT")
    words = name.split()
    while len(words) > 1 and words[-1].lower().rstrip(".,") in GENERIC:
        words.pop()
    return " ".join(words)


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def make_aliases(short: str, name: str) -> list[str]:
    """['Inari Amertron', 'INARI'], ['IJM '], ['Nestlé', 'Nestle', 'NESTLE']. Anything of 5 characters
    or fewer gets a trailing space (whole-word match) so 'IJM' does not fire on 'IJMLAND'."""
    core = core_name(name)
    out: list[str] = []
    for a in (core, _ascii(core), short):                    # "Nestlé" headlines are often "Nestle"
        a = a.strip()
        if a and a.lower() not in {x.strip().lower() for x in out}:
            out.append(a + " " if len(a) <= 5 else a)
    return out


def suggest_sector(industry: str, cfg: config.Config) -> str:
    ind = (industry or "").lower()
    if ind:
        for key, s in cfg.sectors.items():
            if any(m.lower() in ind for m in s.match):
                return key
    return "other" if "other" in cfg.sectors else next(iter(cfg.sectors))


def _stock(code: str, short: str, name: str, industry: str, cfg: config.Config) -> Stock:
    name = tidy_name(name or short)
    return Stock(code=code, short=short.upper(), name=name, industry=industry or "",
                 sector=suggest_sector(industry, cfg), aliases=make_aliases(short.upper(), name),
                 queries=[f'"{core_name(name)}"'])


def _search(q: str, cfg: config.Config, fetch=None) -> list[Stock]:
    try:
        quotes = (fetch or _get_json)(SEARCH, {"q": q, "quotesCount": 10, "newsCount": 0}).get("quotes") or []
    except Exception as e:  # noqa: BLE001 — network trouble must not kill the wizard
        log.warn("lookup failed", q=q, error=str(e)[:200])
        return []
    out, seen = [], set()
    for x in quotes:
        sym = str(x.get("symbol", ""))
        code = sym[:-3] if sym.endswith(".KL") else ""
        if not CODE_RE.match(code) or code in seen:
            continue
        seen.add(code)
        out.append(_stock(code, x.get("shortname") or code, x.get("longname") or x.get("shortname") or code,
                          x.get("industry") or "", cfg))
    return out


def _by_code(code: str, cfg: config.Config, fetch=None) -> list[Stock]:
    try:
        meta = (fetch or _get_json)(CHART.format(f"{code}.KL"), {"range": "1d", "interval": "1d"})["chart"]["result"][0]["meta"]
    except Exception as e:  # noqa: BLE001
        log.warn("code lookup failed", code=code, error=str(e)[:200])
        return []
    return [_stock(code, meta.get("shortName") or code, meta.get("longName") or meta.get("shortName") or code, "", cfg)]


def find(query: str, cfg: config.Config | None = None, fetch=None) -> list[Stock]:
    """Candidates for a name, short name or code. Tries shorter phrasings when nothing matches."""
    cfg = cfg or config.load()
    q = query.strip()
    code = q.upper().removesuffix(".KL")
    if CODE_RE.match(code):
        hits = [s for s in _search(code, cfg, fetch) if s.code == code]
        return hits or _by_code(code, cfg, fetch)
    words = q.split()
    while words:
        hits = _search(" ".join(words), cfg, fetch)
        if hits:
            return hits
        words = words[:-1]
    return []


# ---------------------------------------------------------------- portfolio file

def read(path: Path | None = None) -> dict:
    path = path or config.PORTFOLIO_FILE
    raw = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    return {k: list(raw.get(k) or []) for k in SECTIONS}


def _scalar(v) -> str:
    return yaml.safe_dump([v], default_flow_style=True, allow_unicode=True, width=10_000).strip()[1:-1]


def render(data: dict) -> str:
    out = [HEADER]
    for sec in SECTIONS:
        entries = data.get(sec) or []
        if not entries:
            out.append(f"{sec}: []\n")
            continue
        out.append(f"{sec}:")
        for e in entries:
            out.append(f'  - code: "{e["code"]}"')
            for k in ("short", "name", "sector", "aliases", "queries"):
                out.append(f"    {k}: {_scalar(e.get(k, []) if k in ('aliases', 'queries') else e.get(k, ''))}")
            for k, v in e.items():                                   # keep user extras (qty, avg_cost, …)
                if k not in ("code", "short", "name", "sector", "aliases", "queries"):
                    out.append(f"    {k}: {_scalar(v)}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def write(data: dict, path: Path | None = None) -> Path:
    path = path or config.PORTFOLIO_FILE
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(render(data))
    return path


def locate(data: dict, key: str) -> tuple[str, int] | None:
    """Find an entry by code or short name (case-insensitive) -> (section, index)."""
    k = key.strip().upper().removesuffix(".KL")
    for sec in SECTIONS:
        for n, e in enumerate(data[sec]):
            if str(e["code"]).upper() == k or str(e["short"]).upper() == k:
                return sec, n
    return None


def add(data: dict, stock: Stock, watch: bool = False) -> str:
    """Add (or move/update) a stock. Returns what happened, for display."""
    target = "watchlist" if watch else "holdings"
    entry = stock.entry()
    hit = locate(data, stock.code)
    if hit:
        sec, n = hit
        old = data[sec].pop(n)
        entry = {**old, "sector": stock.sector} if sec != target or old.get("sector") != stock.sector else old
        data[target].append(entry)
        if sec != target:
            return f"moved {stock.short} to {target}"
        return f"updated {stock.short}" if old.get("sector") != stock.sector else f"{stock.short} already in {target}"
    data[target].append(entry)
    return f"added {stock.short} to {target}"


def remove(data: dict, key: str) -> str | None:
    hit = locate(data, key)
    if not hit:
        return None
    sec, n = hit
    e = data[sec].pop(n)
    return f"removed {e['short']} from {sec}"
