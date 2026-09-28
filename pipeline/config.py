"""Paths and config loading. Everything reads data/*.yaml fresh on each run."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "state"
STATE.mkdir(parents=True, exist_ok=True)

PORTFOLIO_FILE = Path(os.environ.get("BURSA_PORTFOLIO") or DATA / "portfolio.yaml")
SECTORS_FILE = DATA / "sectors.yaml"
PREFS_FILE = Path(os.environ.get("BURSA_PREFS") or DATA / "preferences.yaml")
NEWS_DB = Path(os.environ.get("BURSA_NEWS_DB", STATE / "news.db"))

LOOKBACK_HOURS = int(os.environ.get("BURSA_LOOKBACK_HOURS", "72"))
HTTP_TIMEOUT = 30
# KLSE Screener's robots.txt asks for `Crawl-delay: 20`. Seconds between consecutive hits on
# that host; 0 disables. Only KLSE publishes such a rule, so it is not applied to other sources.
KLSE_CRAWL_DELAY = float(os.environ.get("BURSA_KLSE_CRAWL_DELAY", "20"))
RAW_RETENTION_DAYS = int(os.environ.get("BURSA_RAW_RETENTION_DAYS", "7"))
URL_RETENTION_DAYS = int(os.environ.get("BURSA_URL_RETENTION_DAYS", "14"))
DELIVERED_RETENTION_DAYS = int(os.environ.get("BURSA_DELIVERED_RETENTION_DAYS", "180"))
NOTE_RETENTION_DAYS = int(os.environ.get("BURSA_NOTE_RETENTION_DAYS", "60"))


@dataclass
class Holding:
    code: str
    short: str
    name: str
    sector: str
    aliases: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    watch: bool = False                 # on the watchlist (not held): same alerts, marked 👀

    @property
    def label(self) -> str:
        """Short name as shown in messages; watchlist stocks carry the 👀 marker."""
        return f"👀 {self.short}" if self.watch else self.short


@dataclass
class Sector:
    key: str
    label: str
    queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    match: list[str] = field(default_factory=list)   # Yahoo industry substrings that suggest this sector (setup)


@dataclass
class Config:
    holdings: list[Holding]
    sectors: dict[str, Sector]
    macro_queries: list[str]
    macro_keywords: list[str]
    announcement_ignore: list[str]
    announcement_high: list[str]

    @property
    def owned(self) -> list[Holding]:
        return [h for h in self.holdings if not h.watch]

    @property
    def watched(self) -> list[Holding]:
        return [h for h in self.holdings if h.watch]

    def holdings_in_sector(self, key: str) -> list[Holding]:
        return [h for h in self.holdings if h.sector == key]

    def touches(self, sector: str) -> str:
        """'you hold: IJM, SUNWAY · watching 👀: INARI' — for sector headings."""
        own = [h.short for h in self.holdings_in_sector(sector) if not h.watch]
        watch = [h.short for h in self.holdings_in_sector(sector) if h.watch]
        parts = ([f"you hold: {', '.join(own)}"] if own else []) + ([f"watching 👀: {', '.join(watch)}"] if watch else [])
        return " · ".join(parts)

    def by_code(self, code: str) -> Holding | None:
        return next((h for h in self.holdings if h.code == code), None)


def _yaml(path: Path) -> dict:
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def load() -> Config:
    """`cfg.holdings` = holdings + watchlist (watch=True). Everything that screens news iterates it, so
    watchlist stocks get the same alerts; use `cfg.owned` where only real positions make sense."""
    p = _yaml(PORTFOLIO_FILE)
    s = _yaml(SECTORS_FILE)
    holdings = [Holding(code=str(h["code"]), short=h["short"], name=h["name"], sector=h["sector"],
                        aliases=list(h.get("aliases", [])), queries=list(h.get("queries", [])), watch=watch)
                for key, watch in (("holdings", False), ("watchlist", True))
                for h in (p.get(key) or [])]
    sectors = {k: Sector(key=k, label=v.get("label", k), queries=list(v.get("queries", [])),
                         keywords=list(v.get("keywords", [])), match=list(v.get("match", [])))
               for k, v in (s.get("sectors") or {}).items()}
    macro = s.get("macro") or {}
    return Config(holdings=holdings, sectors=sectors,
                  macro_queries=list(macro.get("queries", [])),
                  macro_keywords=list(macro.get("keywords", [])),
                  announcement_ignore=list(s.get("announcement_ignore", [])),
                  announcement_high=list(s.get("announcement_high", [])))
