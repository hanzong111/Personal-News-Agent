"""Unified, staged news memory for the Bursa briefing pipeline.

The database separates full article rows from short-lived dedup keys. Pending work is read without
being removed and advances only after its consumer succeeds, giving scan/digest at-least-once
processing. The CLI provides migration, inspection and deterministic pruning::

    python -m pipeline.memory migrate
    python -m pipeline.memory stats
    python -m pipeline.memory query gamuda --days 30
    python -m pipeline.memory prune --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from . import config

UTC = timezone.utc
DELIVERED_STAGES = ("alerted", "digested")
PENDING_STAGES = ("alert", "digest", "judge")
TERMINAL_STAGES = ("alerted", "digested", "dropped", "seen")
ALL_STAGES = PENDING_STAGES + TERMINAL_STAGES


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value) -> str:
    return json.dumps(value or [], ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None):
    try:
        return json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []


def _title_key(title: str) -> str:
    return "t:" + re.sub(r"[^a-z0-9一-鿿]+", "", (title or "").lower())[:80]


def _terms(title: str) -> list[str]:
    return sorted({w for w in re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).split() if len(w) > 3})


def _chunks(values: list[str], size: int = 800):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def prune_rules(now: datetime | None = None) -> list[tuple[str, str, tuple]]:
    """The deterministic retention rules, as (name, DELETE sql, args). Only `Memory.prune` runs the
    DELETEs; the console turns them into COUNTs (`prune_count_sql`) to show rows overdue for pruning."""
    now = now or datetime.now(UTC)
    raw = (now - timedelta(days=config.RAW_RETENTION_DAYS)).isoformat()
    urls = (now - timedelta(days=config.URL_RETENTION_DAYS)).isoformat()
    delivered = (now - timedelta(days=config.DELIVERED_RETENTION_DAYS)).isoformat()
    return [
        ("keys", "DELETE FROM item_keys WHERE first_seen<?", (raw,)),
        ("raw", "DELETE FROM items WHERE stage IN ('seen','dropped') AND fetched_at<?", (raw,)),
        ("siblings", "DELETE FROM items WHERE stage IN ('alerted','digested') AND fetched_at<? "
         "AND story_id IS NOT NULL AND id NOT IN (SELECT root_id FROM stories)", (raw,)),
        ("delivered", "DELETE FROM items WHERE stage IN ('alerted','digested') AND fetched_at<?", (delivered,)),
        ("urls", "DELETE FROM urls WHERE used_at<?", (urls,)),
    ]


def prune_count_sql(delete_sql: str) -> str:
    return "SELECT COUNT(*) " + delete_sql[delete_sql.index("FROM"):]


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    tkey TEXT,
    source TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    published TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 0,
    codes TEXT NOT NULL DEFAULT '[]',
    sectors TEXT NOT NULL DEFAULT '[]',
    mention INTEGER NOT NULL DEFAULT 0,
    macro INTEGER NOT NULL DEFAULT 0,
    impact TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    stage_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    run TEXT NOT NULL DEFAULT '',
    story_id TEXT,
    type TEXT NOT NULL DEFAULT '',
    sentiment TEXT NOT NULL DEFAULT '',
    risk INTEGER NOT NULL DEFAULT 0,
    brief_headline TEXT NOT NULL DEFAULT '',
    brief_summary TEXT NOT NULL DEFAULT '',
    brief_why TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS item_keys (
    key TEXT PRIMARY KEY,
    item_id TEXT,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stories (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    codes TEXT NOT NULL DEFAULT '[]',
    sectors TEXT NOT NULL DEFAULT '[]',
    title TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    n_items INTEGER NOT NULL DEFAULT 1,
    type TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notes (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    updated TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS urls (
    aid TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_tkey ON items(tkey);
CREATE INDEX IF NOT EXISTS idx_items_stage ON items(stage);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published);
CREATE INDEX IF NOT EXISTS idx_items_story ON items(story_id);
CREATE INDEX IF NOT EXISTS idx_item_keys_item ON item_keys(item_id);
CREATE INDEX IF NOT EXISTS idx_stories_last_seen ON stories(last_seen);
"""


class Memory:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or config.NEWS_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','1')")
        self.conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.conn.close()

    def commit(self):
        self.conn.commit()

    def is_empty(self) -> bool:
        return self.conn.execute("SELECT COUNT(*) FROM item_keys").fetchone()[0] == 0

    def known(self, item_id: str, tkey: str | None = None) -> bool:
        keys = list(dict.fromkeys(k for k in (item_id, tkey) if k))
        if not keys:
            return False
        qs = ",".join("?" for _ in keys)
        return self.conn.execute(f"SELECT 1 FROM item_keys WHERE key IN ({qs}) LIMIT 1", keys).fetchone() is not None

    def _add(self, item: dict, stage: str, run: str = "", fetched_at: str | None = None) -> bool:
        if stage not in ALL_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        now = fetched_at or _now()
        item_id = str(item["id"])
        tkey = str(item.get("tkey") or _title_key(item.get("title", "")))
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO items(id,tkey,source,kind,title,summary,url,published,fetched_at,"
            "tier,codes,sectors,mention,macro,impact,stage,stage_at,run) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, tkey, str(item.get("source") or ""), str(item.get("kind") or ""),
             str(item.get("title") or ""), str(item.get("summary") or ""), str(item.get("url") or ""),
             str(item.get("published") or ""), now, int(item.get("tier") or 0), _json(item.get("codes")),
             _json(item.get("sectors")), int(bool(item.get("mention"))), int(bool(item.get("macro"))),
             str(item.get("impact") or ""), stage, now, run))
        for key in dict.fromkeys((item_id, tkey)):
            if key:
                self.conn.execute(
                    "INSERT INTO item_keys(key,item_id,first_seen) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET item_id=COALESCE(item_keys.item_id,excluded.item_id)",
                    (key, item_id, now))
        return cur.rowcount > 0

    def add(self, item: dict, stage: str, run: str = "", fetched_at: str | None = None) -> bool:
        added = self._add(item, stage, run, fetched_at)
        self.conn.commit()
        return added

    def add_many(self, rows: Iterable[tuple[dict, str]], run: str = "") -> int:
        n = 0
        with self.conn:
            for item, stage in rows:
                n += int(self._add(item, stage, run))
        return n

    @staticmethod
    def _item(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["codes"] = _loads(item.get("codes"))
        item["sectors"] = _loads(item.get("sectors"))
        item["mention"] = bool(item.get("mention"))
        item["macro"] = bool(item.get("macro"))
        item["risk"] = bool(item.get("risk"))
        if item.get("stage") == "digest" and item.get("reason"):
            item["judge_why"] = item["reason"]
        if item.get("brief_headline") or item.get("type"):
            item["_verdict"] = {
                "keep": item.get("stage") != "dropped",
                "type": item.get("type") or "other",
                "sentiment": item.get("sentiment") or "neu",
                "risk": item["risk"],
                "headline": item.get("brief_headline") or item.get("title", "")[:90],
                "summary": item.get("brief_summary") or item.get("summary", "")[:400],
                "why": item.get("brief_why") or "",
                "skip_reason": item.get("reason") or "",
            }
        return item

    def pending(self, *stages: str) -> list[dict]:
        stages = stages or PENDING_STAGES
        if any(s not in PENDING_STAGES for s in stages):
            raise ValueError(f"invalid pending stage(s): {stages}")
        qs = ",".join("?" for _ in stages)
        rows = self.conn.execute(
            f"SELECT * FROM items WHERE stage IN ({qs}) ORDER BY published DESC, fetched_at DESC", stages).fetchall()
        return [self._item(r) for r in rows]

    def advance(self, ids: Iterable[str], stage: str, reason: str = "", run: str = "") -> int:
        ids = list(dict.fromkeys(map(str, ids)))
        if not ids:
            return 0
        if stage not in ALL_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        n = 0
        now = _now()
        with self.conn:
            for chunk in _chunks(ids):
                qs = ",".join("?" for _ in chunk)
                cur = self.conn.execute(
                    f"UPDATE items SET stage=?,stage_at=?,reason=?,run=CASE WHEN ?='' THEN run ELSE ? END "
                    f"WHERE id IN ({qs})", (stage, now, reason, run, run, *chunk))
                n += cur.rowcount
        return n

    def save_verdicts(self, verdicts: dict[str, dict]) -> None:
        with self.conn:
            for item_id, v in verdicts.items():
                self.conn.execute(
                    "UPDATE items SET type=?,sentiment=?,risk=?,brief_headline=?,brief_summary=?,brief_why=?,reason=? WHERE id=?",
                    (str(v.get("type") or "other"), str(v.get("sentiment") or "neu"), int(bool(v.get("risk"))),
                     str(v.get("headline") or "")[:90], str(v.get("summary") or "")[:400],
                     str(v.get("why") or "")[:200], str(v.get("skip_reason") or "")[:120], item_id))

    def judge_result(self, item_id: str, verdict: dict | None, run: str = "") -> None:
        now = _now()
        with self.conn:
            if verdict:
                codes, sectors = verdict.get("codes") or [], verdict.get("sectors") or []
                self.conn.execute(
                    "UPDATE items SET codes=?,sectors=?,tier=2,mention=?,stage='digest',stage_at=?,reason=?,run=? WHERE id=?",
                    (_json(codes), _json(sectors), int(bool(codes)), now, str(verdict.get("why") or ""), run, item_id))
            else:
                self.conn.execute(
                    "UPDATE items SET stage='dropped',stage_at=?,reason='judge_irrelevant',run=? WHERE id=?",
                    (now, run, item_id))

    def recent_alerts(self, hours: int = 48) -> list[dict]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT title,codes,COALESCE(NULLIF(published,''),stage_at) AS when_at FROM items "
            "WHERE stage='alerted' AND stage_at>=? ORDER BY stage_at DESC", (cutoff,)).fetchall()
        out = []
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["when_at"]).timestamp()
            except (TypeError, ValueError):
                ts = datetime.now(UTC).timestamp()
            out.append({"ts": ts, "codes": _loads(r["codes"]), "title": r["title"], "terms": _terms(r["title"])})
        return out

    def delivered(self, days: int = 7) -> list[dict]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM items WHERE stage IN ('alerted','digested') "
            "AND COALESCE(NULLIF(published,''),stage_at)>=? ORDER BY published DESC,stage_at DESC", (cutoff,)).fetchall()
        return [self._item(r) for r in rows]

    def unclustered_delivered(self, days: int = 7) -> list[dict]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM items WHERE stage IN ('alerted','digested') AND story_id IS NULL "
            "AND COALESCE(NULLIF(published,''),stage_at)>=? ORDER BY published", (cutoff,)).fetchall()
        return [self._item(r) for r in rows]

    def open_stories(self, days: int = 7) -> list[dict]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.conn.execute("SELECT * FROM stories WHERE last_seen>=? ORDER BY last_seen DESC", (cutoff,)).fetchall()
        out = []
        for row in rows:
            d = dict(row); d["codes"] = _loads(d["codes"]); d["sectors"] = _loads(d["sectors"]); out.append(d)
        return out

    def create_story(self, item: dict, story_type: str = "other") -> str:
        story_id = "story-" + hashlib.sha1(str(item["id"]).encode()).hexdigest()[:16]
        when = item.get("published") or item.get("stage_at") or _now()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO stories(id,root_id,codes,sectors,title,first_seen,last_seen,n_items,type) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (story_id, item["id"], _json(item.get("codes")), _json(item.get("sectors")), item.get("title", ""),
                 when, when, 1, story_type))
            self.conn.execute("UPDATE items SET story_id=?,type=CASE WHEN type='' THEN ? ELSE type END WHERE id=?",
                              (story_id, story_type, item["id"]))
        return story_id

    def attach_story(self, item: dict, story_id: str, story_type: str = "") -> bool:
        story = self.conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
        if not story:
            return False
        codes = sorted(set(_loads(story["codes"])) | set(item.get("codes") or []))
        sectors = sorted(set(_loads(story["sectors"])) | set(item.get("sectors") or []))
        when = item.get("published") or item.get("stage_at") or _now()
        with self.conn:
            current = self.conn.execute("SELECT story_id FROM items WHERE id=?", (item["id"],)).fetchone()
            increment = bool(current and current[0] != story_id)
            self.conn.execute(
                "UPDATE stories SET codes=?,sectors=?,last_seen=CASE WHEN last_seen>? THEN last_seen ELSE ? END,"
                "n_items=n_items+?,type=CASE WHEN type='' AND ?!='' THEN ? ELSE type END WHERE id=?",
                (_json(codes), _json(sectors), when, when, int(increment), story_type, story_type, story_id))
            self.conn.execute("UPDATE items SET story_id=?,type=CASE WHEN type='' AND ?!='' THEN ? ELSE type END WHERE id=?",
                              (story_id, story_type, story_type, item["id"]))
        return True

    def set_note(self, key: str, kind: str, text: str) -> None:
        self.conn.execute(
            "INSERT INTO notes(key,kind,updated,text) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET kind=excluded.kind,updated=excluded.updated,text=excluded.text",
            (key, kind, _now(), text.strip()))
        self.conn.commit()

    def notes_for(self, codes: Iterable[str] = (), sectors: Iterable[str] = ()) -> dict[str, str]:
        keys = list(dict.fromkeys([*map(str, codes), *map(str, sectors)]))
        if not keys:
            return {}
        qs = ",".join("?" for _ in keys)
        return dict(self.conn.execute(f"SELECT key,text FROM notes WHERE key IN ({qs})", keys).fetchall())

    def all_notes(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM notes ORDER BY kind,key")]

    def urls_get(self, aids: list[str], touch: bool = True) -> dict[str, str]:
        if not aids:
            return {}
        out: dict[str, str] = {}
        for chunk in _chunks(list(dict.fromkeys(aids))):
            qs = ",".join("?" for _ in chunk)
            out.update(self.conn.execute(f"SELECT aid,url FROM urls WHERE aid IN ({qs})", chunk).fetchall())
        if touch and out:
            now = _now()
            with self.conn:
                for chunk in _chunks(list(out)):
                    qs = ",".join("?" for _ in chunk)
                    self.conn.execute(f"UPDATE urls SET used_at=? WHERE aid IN ({qs})", (now, *chunk))
        return out

    def urls_put(self, pairs: dict[str, str]) -> None:
        if not pairs:
            return
        now = _now()
        with self.conn:
            self.conn.executemany(
                "INSERT INTO urls(aid,url,used_at) VALUES(?,?,?) "
                "ON CONFLICT(aid) DO UPDATE SET url=excluded.url,used_at=excluded.used_at",
                [(aid, url, now) for aid, url in pairs.items()])

    def query(self, term: str, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        like = f"%{term.lower()}%"
        rows = self.conn.execute(
            "SELECT * FROM items WHERE COALESCE(NULLIF(published,''),fetched_at)>=? AND "
            "(lower(title) LIKE ? OR lower(summary) LIKE ? OR lower(source) LIKE ? OR lower(codes) LIKE ? OR lower(sectors) LIKE ?) "
            "ORDER BY published DESC,fetched_at DESC", (cutoff, like, like, like, like, like)).fetchall()
        return [self._item(r) for r in rows]

    def stats(self) -> dict:
        stages = dict(self.conn.execute("SELECT stage,COUNT(*) FROM items GROUP BY stage"))
        oldest = self.conn.execute("SELECT MIN(fetched_at) FROM items").fetchone()[0]
        newest = self.conn.execute("SELECT MAX(fetched_at) FROM items").fetchone()[0]
        result = {
            "path": str(self.path), "bytes": self.path.stat().st_size if self.path.exists() else 0,
            "items": sum(stages.values()), "by_stage": stages,
            "keys": self.conn.execute("SELECT COUNT(*) FROM item_keys").fetchone()[0],
            "stories": self.conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
            "notes": self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
            "urls": self.conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0],
            "oldest": oldest, "newest": newest,
        }
        row = self.conn.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
        result["last_prune"] = _loads(row[0]) if row else None
        return result

    def prune(self, dry_run: bool = False) -> dict:
        rules = prune_rules()
        counts: dict[str, int | bool | str] = {}
        total_before = sum(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                           for t in ("items", "item_keys", "stories", "urls"))
        for name, delete_sql, args in rules:
            counts[name] = self.conn.execute(prune_count_sql(delete_sql), args).fetchone()[0]
        if dry_run:
            counts["stories"] = self.conn.execute(
                "SELECT COUNT(*) FROM stories WHERE NOT EXISTS (SELECT 1 FROM items WHERE items.story_id=stories.id)").fetchone()[0]
            counts.update(dry_run=True, vacuum=False)
            return counts
        with self.conn:
            for _, sql, args in rules:
                self.conn.execute(sql, args)
            counts["stories"] = self.conn.execute(
                "DELETE FROM stories WHERE NOT EXISTS (SELECT 1 FROM items WHERE items.story_id=stories.id)").rowcount
        freed = sum(int(v) for k, v in counts.items() if k not in ("stories",) and isinstance(v, int)) + int(counts["stories"])
        vacuum = total_before > 0 and freed / total_before > 0.20
        report = {**counts, "dry_run": False, "vacuum": vacuum, "at": _now()}
        self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_prune',?)", (_json(report),))
        self.conn.commit()
        if vacuum:
            self.conn.execute("VACUUM")
        return report

    def migrate(self, legacy_state: str | Path | None = None, rename: bool = True) -> dict:
        state = Path(legacy_state or config.STATE)
        now = _now()
        paths = {
            "seen": state / "seen.db", "digest": state / "digest_queue.jsonl",
            "judge": state / "judge_queue.jsonl", "alerted": state / "alerted.jsonl",
            "urls": state / "gnews_urls.db",
        }
        report = {"seen": 0, "digest": 0, "judge": 0, "alerted": 0, "urls": 0, "renamed": []}
        seen_keys: list[str] = []
        if paths["seen"].exists():
            con = sqlite3.connect(paths["seen"])
            rows = con.execute("SELECT id,first_seen FROM seen").fetchall(); con.close()
            seen_keys = [r[0] for r in rows]
            with self.conn:
                self.conn.executemany("INSERT OR IGNORE INTO item_keys(key,item_id,first_seen) VALUES(?,NULL,?)",
                                      [(key, first or now) for key, first in rows])
            report["seen"] = len(rows)

        def jsonl(path: Path) -> list[dict]:
            if not path.exists():
                return []
            out = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
            return out

        pending_ids: list[str] = []
        for name, stage in (("digest", "digest"), ("judge", "judge")):
            rows = jsonl(paths[name])
            with self.conn:
                for item in rows:
                    item.setdefault("tkey", _title_key(item.get("title", "")))
                    self._add(item, stage, fetched_at=now)
                    pending_ids.append(str(item["id"]))
            report[name] = len(rows)

        alerts = jsonl(paths["alerted"])
        with self.conn:
            for row in alerts:
                item_id = "legacy-alert-" + hashlib.sha1(
                    f"{row.get('ts')}|{row.get('title')}|{row.get('codes')}".encode()).hexdigest()[:18]
                when = datetime.fromtimestamp(float(row.get("ts") or 0), UTC).isoformat()
                self.conn.execute(
                    "INSERT OR IGNORE INTO items(id,tkey,title,published,fetched_at,tier,codes,stage,stage_at) "
                    "VALUES(?,?,?,?,?,1,?,'alerted',?)",
                    (item_id, _title_key(row.get("title", "")), row.get("title", ""), when, when,
                     _json(row.get("codes")), when))
        report["alerted"] = len(alerts)

        url_aids: list[str] = []
        if paths["urls"].exists():
            con = sqlite3.connect(paths["urls"])
            rows = con.execute("SELECT aid,url FROM urls").fetchall(); con.close()
            url_aids = [r[0] for r in rows]
            with self.conn:
                self.conn.executemany("INSERT OR IGNORE INTO urls(aid,url,used_at) VALUES(?,?,?)",
                                      [(aid, url, now) for aid, url in rows])
            report["urls"] = len(rows)

        def present(table: str, column: str, values: list[str]) -> int:
            n = 0
            for chunk in _chunks(list(dict.fromkeys(values))):
                qs = ",".join("?" for _ in chunk)
                n += self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({qs})", chunk).fetchone()[0]
            return n

        checks = {
            "seen": (present("item_keys", "key", seen_keys), len(set(seen_keys))),
            "pending": (present("items", "id", pending_ids), len(set(pending_ids))),
            "urls": (present("urls", "aid", url_aids), len(set(url_aids))),
        }
        bad = {k: v for k, v in checks.items() if v[0] != v[1]}
        if bad:
            raise RuntimeError(f"migration reconciliation failed: {bad}")
        report["checks"] = checks
        self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('migration',?)", (_json(report),))
        self.conn.commit()
        if rename:
            for path in paths.values():
                if not path.exists():
                    continue
                target = path.with_name(path.name + ".migrated")
                if target.exists():
                    raise FileExistsError(f"refusing to overwrite migration backup: {target}")
                os.replace(path, target)
                report["renamed"].append(str(target))
        return report


def _print_query(rows: list[dict]) -> None:
    for item in rows:
        codes = ",".join(item.get("codes") or []) or "-"
        print(f"{item.get('published') or item.get('fetched_at')}  {item['stage']:9s}  {codes:12s}  {item['title']}")
        if item.get("url"):
            print(f"  {item['url']}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipeline.memory")
    ap.add_argument("--db", type=Path, default=config.NEWS_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    q = sub.add_parser("query"); q.add_argument("term"); q.add_argument("--days", type=int, default=30)
    m = sub.add_parser("migrate"); m.add_argument("--legacy-state", type=Path, default=config.STATE); m.add_argument("--no-rename", action="store_true")
    p = sub.add_parser("prune"); p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    with Memory(a.db) as memory:
        if a.cmd == "stats":
            print(json.dumps(memory.stats(), indent=2, ensure_ascii=False))
        elif a.cmd == "query":
            _print_query(memory.query(a.term, a.days))
        elif a.cmd == "migrate":
            print(json.dumps(memory.migrate(a.legacy_state, rename=not a.no_rename), indent=2, ensure_ascii=False))
        elif a.cmd == "prune":
            print(json.dumps(memory.prune(a.dry_run), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
