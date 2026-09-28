---
name: malaysia-news
description: "TickerPigeon Malaysia headlines follow-up: 'more <section>' lists, and article detail for any headline."
version: 2.0.0
author: hanzong111
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Malaysia, news, headlines, briefing, follow-up]
---

# Malaysia News — follow-up

The `malaysia-news` cron job (12:00 and 20:00 MYT) sends a **tiered index** built by a chain of
single-purpose agents (fetch → Classifier → Editor → Renderer). Everything it counted is saved in
`data/state/news/` of the Bursa project. Two follow-ups land here:

```bash
cd "__PROJECT_DIR__"
PY=./.venv/bin/python
```

## 1. "more <section>"  ("more politics", "what else in economy", "more sport")

No model needed — the index already classified everything:

```bash
$PY -m pipeline.news_index --more politics      # politics | economy | policy | haze | incidents | sport | other
```

Paste the output as-is (it is already formatted: emoji + English headline + report count).

## 2. "tell me about <headline>"

```bash
$PY -m pipeline.news_fetch --pool haze          # find it: ID | time | source | title | URL (keyword, case-insensitive)
$PY -m pipeline.news_fetch --article 8a9474     # article text by pool ID (or a URL)
```

Then answer in exactly this shape (translate Malay/Chinese to English):

```
<sentiment emoji> **<headline, 4–8 words>**
🕒 <date and time from the pool, MYT>
<2–4 sentences with the concrete facts: names, numbers, what happens next>
💡 <why it matters — one line>
🔗 [<Source name>](<url>)
```

Notes
- Several pool lines for one story = same story from different outlets; fetch the freshest.
- Empty article text (paywall / JS page): say so and give the link. Don't guess.
- Not in the pool = it wasn't in the index window. Offer a wider look:
  `$PY -m pipeline.news_fetch --raw --hours 24 --all` (read-only, marks nothing).
- Never run `pipeline.news_index --cron` by hand — that is the scheduled job and it advances the window.
