"""CLASSIFIER — Haiku. One story in, one label set out. No editorial judgement.

  input : [{id, title, src, reports}]
  output: {id: {malaysian, section, sentiment, risk, title_en}}
"""
from __future__ import annotations
from . import llm
from pipeline.log import get as _get_log

log = _get_log("agent.classifier")

SECTIONS = ["POLITICS", "ECONOMY", "POLICY", "HAZE_WEATHER", "INCIDENTS", "SPORT", "OTHER"]
SENTIMENTS = ["pos", "neg", "neu", "routine"]

PROMPT = """You label Malaysian news headlines. For EVERY line below return one JSON object. Output a JSON array only — no prose, no markdown fences.

Fields:
  "id":        copy exactly
  "malaysian": true if the story is about Malaysia or directly affects it; false for foreign sport, Singapore-only local news, gadget launches, wire stories about other countries
  "section":   one of POLITICS | ECONOMY | POLICY | HAZE_WEATHER | INCIDENTS | SPORT | OTHER
               (POLITICS = parties, MPs, coalition, pardons, elections; ECONOMY = markets, companies, ringgit, inflation, trade;
                POLICY = government programmes, ministries, budget measures, diplomacy; HAZE_WEATHER = haze/API readings, floods, rain, cloud seeding;
                INCIDENTS = crime, accidents, fires, scams, rescues; SPORT; OTHER = culture, lifestyle, tech, tourism, human interest)
  "sentiment": pos | neg | neu | routine   (routine = scheduled/expected/no change, e.g. "ringgit to stay range-bound", obituaries)
  "risk":      true only if the story signals a material risk to people, the economy or the government (deaths, unhealthy air, fiscal stress, coalition instability)
  "title_en":  the headline in clear English, max 12 words, names and numbers exact; translate Malay/Chinese

Lines (id | reports | source | title):
{lines}"""


CHUNK = 60   # stories per call: keeps each reply short and lets a bad chunk fail alone


def run(stories: list[dict]) -> dict[str, dict]:
    labels: dict[str, dict] = {}
    n_chunks = (len(stories) + CHUNK - 1) // CHUNK
    log.info("start", stories=len(stories), chunks=n_chunks)
    for i in range(0, len(stories), CHUNK):
        got = _run_chunk(stories[i:i + CHUNK])
        log.info("chunk done", chunk=f"{i // CHUNK + 1}/{n_chunks}", stories=min(CHUNK, len(stories) - i), labelled=len(got))
        labels.update(got)
    missing = sum(1 for s in stories if s["id"] not in labels)
    if missing:
        log.warn("stories left unlabelled (defaulted to OTHER)", count=missing)
    for s in stories:   # anything the model skipped keeps its original title and lands in OTHER
        labels.setdefault(s["id"], {"malaysian": True, "section": "OTHER", "sentiment": "neu",
                                    "risk": False, "title_en": s["title"][:120]})
    return labels


def _run_chunk(stories: list[dict]) -> dict[str, dict]:
    if not stories:
        return {}
    lines = "\n".join(f"{s['id']} | x{s.get('reports', 1)} | {s.get('src', '')[:20]} | {s['title'][:140]}" for s in stories)
    try:
        out = llm.call_json(PROMPT.format(lines=lines), model=llm.HAIKU, effort="low", role="classifier")
    except Exception as e:
        log.error("chunk failed", err=f"{type(e).__name__}: {e}")
        return {}
    labels: dict[str, dict] = {}
    for row in out if isinstance(out, list) else []:
        sid = str(row.get("id", ""))
        if sid not in {s["id"] for s in stories}:
            continue
        sec = str(row.get("section", "OTHER")).upper().replace(" & ", "_").replace(" ", "_")
        labels[sid] = {
            "malaysian": bool(row.get("malaysian", True)),
            "section": sec if sec in SECTIONS else "OTHER",
            "sentiment": row.get("sentiment") if row.get("sentiment") in SENTIMENTS else "neu",
            "risk": bool(row.get("risk", False)),
            "title_en": (row.get("title_en") or "").strip()[:120],
        }
    return labels
