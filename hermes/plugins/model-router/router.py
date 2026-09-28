"""Pure routing logic for the model-router plugin (no Hermes imports, so it is unit-testable).

decide(text, previous, judge) -> (label, how)
    label: "hard" | "easy"      how: "skip" | "rule" | "llm" | "fallback"
"""
from __future__ import annotations

import re

HARD_MODEL = "claude-opus-5"
HARD_EFFORT = "medium"
JUDGE_MODEL = "claude-haiku-4-5"
JUDGE_TIMEOUT_S = 4.0
FOLLOW_UP_CHARS = 60          # a short message right after a hard turn usually continues it

# Obvious small talk never needs a model call.
_TRIVIAL = re.compile(r"^\s*(hi|hey|hello|yo|ok(ay)?|k|thanks?( you)?|thx|ty|good (morning|night)|gm|gn|"
                      r"👍|🙏|nice|cool|great|noted|got it|sure|yes|no|yep|nope)[\s!.?]*$", re.I)
# Used only when the judge is unavailable (timeout / error).
_HARD_HINTS = re.compile(
    r"analy[sz]|valuation|valuate|fundamental|technical|chart|outlook|forecast|target price|fair value|"
    r"should i (buy|sell|add|hold|average|cut|trim)|buy or sell|worth (buying|holding)|risk|thesis|"
    r"compare|comparison|\bvs\b\.?|versus|portfolio (review|strategy|allocation)|rebalanc|dcf|p/?e\b|"
    r"dividend yield|earnings (review|quality)|why did .* (drop|fall|rise|jump)|deep dive|strategy|plan",
    re.I)

JUDGE_PROMPT = """Classify how hard this chat message is for an assistant that tracks a Bursa Malaysia stock
portfolio. Answer with exactly one word: hard or easy.

hard = needs careful reasoning: analysing a stock or company (valuation, fundamentals, technicals,
outlook, buy/sell/hold, risks), comparing holdings, portfolio strategy, interpreting earnings or news
impact in depth, multi-step planning, long writing, debugging.
easy = quick lookups and chat: latest news or price of a stock, what an alert said, status of a job,
definitions, short factual questions, greetings, confirmations, simple commands.
{previous}
Message: {text}"""


def _previous_hint(previous: str | None, text: str) -> str:
    if previous == "hard" and len(text) <= FOLLOW_UP_CHARS:
        return "The previous message was hard; a short follow-up that continues that analysis is hard too.\n"
    return ""


def parse_label(reply: str | None) -> str | None:
    word = (reply or "").strip().lower()
    if word.startswith("hard"):
        return "hard"
    if word.startswith("easy"):
        return "easy"
    return None


def fallback(text: str) -> str:
    return "hard" if _HARD_HINTS.search(text or "") else "easy"


def decide(text: str, previous: str | None, judge) -> tuple[str | None, str]:
    """judge(prompt) -> reply text (may raise). Returns (None, 'skip') for commands / empty messages."""
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return None, "skip"
    if _TRIVIAL.match(text):
        return "easy", "rule"
    try:
        label = parse_label(judge(JUDGE_PROMPT.format(previous=_previous_hint(previous, text), text=text[:1500])))
    except Exception:
        label = None
    if label is None:
        return fallback(text), "fallback"
    return label, "llm"
