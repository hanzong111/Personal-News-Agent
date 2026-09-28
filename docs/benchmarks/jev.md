# Benchmark: TickerPigeon with and without Jev

TickerPigeon can optionally use [TypeSafe's Jev](https://docs.typesafe.ai/), a model that returns typed
judgments (yes/no, pick one, a score) instead of text. It replaces four jobs that the original workflow
does with word overlap, regexes and keyword lists:

1. **Repeat stories**: do two headlines report the same event?
2. **Relevance**: is the stock the headline's subject, or is it only quoted or listed?
3. **Story type**: earnings, contract, corporate action, analyst call, …
4. **Sentiment**: is the news good or bad for a shareholder?

This page compares the two on the same stored news.

## Setup

- **Data:** one TickerPigeon install's real news from 20–28 Sep 2026: 696 stock and sector headlines, and
  a 120-headline Malaysia news window. Everything ran in shadow mode, with nothing sent or saved.
- **Baseline:** what the original workflow did or stored. That means the word-overlap repeat rules, the
  headline clustering rule, the attribution regexes, the keyword type rules, and the labels written by
  Claude Haiku (headline classifier) and Claude Sonnet (alert briefer).
- **Jev:** `jev-1.13.0`, pinned, given the same category definitions the original prompts use.
- **Judging:** there's no pre-labelled answer set. Every disagreement was checked by hand. Agreements
  weren't re-checked.
- **Cost:** the whole benchmark was about 1,350 judgments for **US$0.035**.

## Results

| Capability | Original workflow | With Jev |
|---|---|---|
| **Duplicate alerts delivered** | 3 extra alerts in 8 days: one contract story alerted 3 times in one afternoon, one land deal twice | Replaying the triple, Jev keeps the first and drops the 2 repeats (0.6 s) |
| **Repeat detection, stock news** (600 candidate pairs) | Word rule merges 56 pairs | Finds 173 same-event pairs: **128 the rule missed**. It also separates 11 pairs the rule merged: 7 were really different stories (e.g. "…this Tuesday" / "…this Thursday"), 1 was the same story and Jev missed it, 3 are debatable |
| **Repeat detection, Malaysia headlines** (120 headlines) | 102 stories | **75 stories**, including the same story in English and Malay, which word overlap can't match |
| **Relevance: stock is the subject** (94 pairs) | 4 regexes | With a 0.7 confidence gate: **16 fixes, 0 regressions**. Broker calls ("RHB Turns Bearish On CPO…") and stock lists stop being treated as company news. The Sonnet briefer already dropped most of the lists later; Jev drops them before that paid step |
| **Story type** (256 items) | Keyword rules put 189 in "other" | Jev puts 61 in "other" and types the rest. In the 30 most confident disagreements with the Claude labels: Jev better in 20, Claude in 7 (5 were land leases, fixed since), 3 judgment calls |
| **Shareholder sentiment** (61 items) | Sonnet says "neutral" in all 16 disagreements | Picks a side; right in most, one clear miss (a "stocks with momentum" list called negative) |
| **Headline section** (86 stories) | Claude Haiku | 83% agreement; the disagreements are judgment calls, so **Jev isn't used for this** |

### Examples

Same event, missed by the word rule:

- "HE Group secures RM124mil subcontract for Johor data centre" / "HE Group Awarded RM124 Million
  Electrical Subcontract Job For Data Centre" (four wordings of one story)
- "MACC investigates UiTM Holdings losses, RM42 million solar project payment" / "MACC Swoops In On UITM
  Holdings For Alleged Corruption Including Solar Project Payment"
- "Siti Hasmah, wife of Malaysian ex-PM Mahathir, dies at 100" / "Isteri mantan PM Malaysia Dr Mahathir
  Mohamad, Siti Hasmah, meninggal dunia pada usia 100 tahun"

Stock quoted, not the subject (the regexes say subject; Jev says source only, confidence ≥ 0.97):

- "RHB Turns Bearish On CPO As Price Drops Below RM4,800"
- "India's Palm Oil Tax Cut Could Boost Earnings For Local Planters, CIMB"

Story type, keyword rules vs Jev: "Oil extends advance on renewed tensions between the US and Iran" (other →
commodity), "Cypark Fixes Third Tranche Private Placement Price" (other → corporate action), "SUNeVision Wins
Sustainable Organisation Bronze Award" (contract → other).

## Where Jev is wired in

| Step | With Jev | Without Jev, or when a call fails |
|---|---|---|
| Alert scan: repeats and updates | Two questions per pair: same story? something happened after the earlier report? Same + nothing new = repeat (dropped); same + new = update (alerts, marked 🔄). Compared with the past week's alerts and earlier items in the batch | Word overlap ≥ 0.5 |
| Alert scan: relevance | Stock only quoted or listed (confidence ≥ 0.7) → evening digest | Attribution regexes |
| Malaysia headlines: clustering | Jev judges candidate pairs (up to 400 a run) | Shared rare words or overlap ≥ 0.45 |
| Curator: story type fallback | Jev story type | Keyword rules |

The repeat numbers above were measured with an earlier single "same event?" score. That version
also treated a deal reaching a new stage (signed, completed) as a repeat. The live check now asks
the two questions separately. Re-scored on the same 173 repeat pairs: 149 are still dropped, 22 now
alert as updates (mostly real developments such as a trial moving from "test" to "completed"), and
2 are no longer judged the same story.

Each answer falls back to the original rule for just that input, so a partial outage degrades gracefully.
In daily use Jev costs about **US$1 a month**, most of it for the three headline editions.

## Limits

- One week of one user's news. Re-run the benchmark on your own news before you rely on it (below).
- Hand-judged disagreements, not a labelled test set, so treat the counts as indicative.
- Grouping is transitive. Coverage of a big event (a death, the tributes, the state funeral) becomes one
  story. That's what a headline index wants, but a follow-up can land inside the original story.
- Jev is strongest in English. Malay matching worked well here, but Chinese headlines were few.
- Jev still makes mistakes: an op-ed about AI "hyperscalers" typed as a corporate action, and a
  land-bank feature as earnings.

## Reproduce on your own news

```bash
./.venv/bin/python -m pip install -r requirements.txt          # includes typesafe-sdk
export TYPESAFE_API_KEY=…                                         # or put it in ~/.hermes/.env
./.venv/bin/python benchmarks/jev/same_story.py --corpus bursa    # repeats, stock news
./.venv/bin/python benchmarks/jev/same_story.py --corpus general  # repeats, Malaysia headlines
./.venv/bin/python benchmarks/jev/subject_check.py                # relevance (add --also CODE:SHORT:NAME:ALIASES for brokers)
./.venv/bin/python benchmarks/jev/categorize.py --corpus bursa    # story type + sentiment
./.venv/bin/python benchmarks/jev/categorize.py --corpus general  # section, sentiment, risk
```

Each script has `--dry-run` (no key needed: counts and a cost estimate). Results and cached answers go to
`benchmarks/jev/results/` and `benchmarks/jev/.cache/`, both gitignored because they contain your stocks
and headlines. The benchmark scripts keep their own copy of the question wording. The pipeline's live
wording is in `agents/jev.py`, which adds land leases to "corporate action" after this benchmark.
