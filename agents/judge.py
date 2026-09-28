"""RELEVANCE JUDGE — Haiku. Rescues holdings/sector news the keyword rules missed.

  input : candidate items [{id, title, summary}] that matched no alias/keyword, plus the portfolio
  output: {id: {"codes": [...], "sectors": [...]}} for the relevant ones only
"""
from __future__ import annotations
from . import llm
from pipeline.log import get as _get_log

log = _get_log("agent.judge")

PROMPT = """You screen news for a Bursa Malaysia investor. Below are the holdings and the sectors they belong to, then candidate headlines that matched no keyword. Decide which candidates a shareholder would want to know about, and why.

Return a JSON array only — no prose, no fences — with one object per RELEVANT candidate:
  {{"id": "...", "codes": [stock codes it directly concerns, may be empty], "sectors": [sector keys it materially affects, may be empty], "confidence": "high|medium", "why": "<= 12 words naming the transmission channel"}}
Omit everything else. The bar is: would a careful analyst covering THIS holding put it in a client note today?

RELEVANT (high): the holding is named or is the obvious counterparty (a contract it won, a project it builds, a hospital it runs); a Malaysian regulation, tariff, tax, subsidy, price cap or policy that changes the economics of one of the SECTORS (e.g. anti-dumping duty on Malaysian solar modules, Bank Negara capital or OPR change, new private-hospital fee rules, a change to grid access charges); a large Malaysian project award in a sector (e.g. an LSS round, a rail package).
RELEVANT (medium): a Malaysian macro item with a clear, named channel to a sector (e.g. electricity tariff change -> solar demand; construction material price surge -> construction margins).
NOT RELEVANT: foreign demographics or health trends, ASEAN/global statements without a Malaysian rule change, generic market wrap-ups and index moves, other companies' results, analyst picks of other stocks, lifestyle/tech launches, anything where the link to a holding is a general theme rather than a specific mechanism.

HOLDINGS:
{holdings}

SECTORS:
{sectors}

CANDIDATES (id | title | summary):
{lines}"""


def run(cfg, candidates: list[dict]) -> dict[str, dict]:
    if not candidates:
        log.info("no candidates")
        return {}
    log.info("start", candidates=len(candidates))
    holdings = "\n".join(f"- {h.code} {h.short}: {h.name} (sector {h.sector}; aliases {', '.join(h.aliases)})" + (" [WATCHLIST — not held]" if h.watch else "") for h in cfg.holdings)
    sectors = "\n".join(f"- {k}: {s.label} — themes: {', '.join(s.keywords[:8])}" for k, s in cfg.sectors.items() if cfg.holdings_in_sector(k))
    lines = "\n".join(f"{c['id']} | {c['title'][:120]} | {(c.get('summary') or '')[:160]}" for c in candidates)
    out = llm.call_json(PROMPT.format(holdings=holdings, sectors=sectors, lines=lines), model=llm.HAIKU, effort="low", role="judge")
    valid_codes = {h.code for h in cfg.holdings}
    valid_secs = {k for k in cfg.sectors if cfg.holdings_in_sector(k)}
    by_id = {c["id"]: c for c in candidates}
    ids = set(by_id)
    res = {}
    for row in out if isinstance(out, list) else []:
        i = str(row.get("id", ""))
        if i not in ids:
            continue
        text = f"{by_id[i].get('title', '')} {by_id[i].get('summary', '')}".lower()
        codes, secs = [], [s for s in map(str, row.get("sectors", [])) if s in valid_secs]
        for c in map(str, row.get("codes", [])):
            h = cfg.by_code(c)
            if h and any(a.strip().lower() in text for a in h.aliases):
                codes.append(c)                     # company must actually be named
            elif h and h.sector not in secs:
                secs.append(h.sector)               # otherwise it's sector news at best
        conf = str(row.get("confidence", "medium")).lower()
        if (codes or secs) and conf == "high" or (secs and conf == "medium" and not codes):
            res[i] = {"codes": codes, "sectors": secs, "why": str(row.get("why", ""))[:80]}
            log.info("rescued", id=i, codes=",".join(codes) or "-", sectors=",".join(secs) or "-", conf=conf,
                     title=by_id[i].get("title", "")[:70], why=res[i]["why"])
    log.info("done", candidates=len(candidates), flagged=len(out) if isinstance(out, list) else 0, rescued=len(res))
    return res
