---
name: bursa-setup
description: "First-time setup and later changes for the Bursa news agent: which stocks the user holds, a watchlist, which chat app messages go to, and which messages (alerts, digest, weekly, headlines) they get at what times."
version: 1.0.0
author: hanzong111
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Bursa, KLSE, onboarding, setup, preferences, watchlist, newsletter]
---

# Bursa setup — onboarding and preferences

Use this skill when:
- the user says `/setup`, "set me up", "get started", "start over", or is clearly new;
- `setup status` prints `Set up: no` (check this at the start of any portfolio conversation);
- the user wants to change **what** they get, **when**, or **where**: "move the digest to 7pm", "stop the
  weekly review", "headlines only in the morning", "alerts only 9 to 5", "watch INARI", "I sold IJM",
  "send everything to Discord instead".

All changes go through one command — never edit the YAML files by hand from chat:

```bash
cd "__PROJECT_DIR__"
S="./.venv/bin/python -m pipeline.setup"
```

## Onboarding conversation (new user)

Keep each message short. One question at a time. Confirm what you did after each step.

**0. Where things stand.** Run `$S status`. If they already have stocks, show them and ask whether to
keep and add, or start over (start over = `$S remove <code>` for each).

**1. Holdings.** Ask: *"Which Bursa stocks do you hold? Names or codes are fine, e.g. Gamuda, 5347, Top Glove."*
For each one run:
```bash
$S add "<what they typed>"
```
- `✓ added …` → tell them the short name and the sector it was filed under.
- `Several matches …` (exit code 2) → show the numbered list, ask which, then `$S add "<same text>" --pick N`.
- `No Bursa stock found` → ask for the 4-digit code (klsescreener.com shows it) and retry with the code.
- Sector shows `Other`, or looks wrong (Yahoo files some solar firms under construction, glove makers
  under medical) → offer the right one from `$S sectors` and redo with `--sector <key>`.

**2. Watchlist.** Ask: *"Any stocks you don't hold but want news on? They get the same alerts, marked 👀."*
Same as step 1 with `--watch`. "None" is a fine answer.

**3. Chat app.** Default: the app this conversation is on. Run `$S apps` for the apps connected in
Hermes and ask: *"Should your alerts come here on <this app>, or to another app?"* Then:
```bash
$S set deliver=telegram --no-apply          # or discord, slack, whatsapp, signal, feishu, … ; app:chat_id for a group
```
Only connected apps can receive. If they want one that isn't listed, tell them it needs connecting on the
laptop first (`hermes setup gateway`) and use this app for now.

**4. Messages and times.** Explain the four messages in one list, with the defaults, then ask which
they want and whether any time should change:
```
⚡ Instant alerts — news naming your stocks, checked every 30 min, 08:00–18:30 Mon–Fri
🌆 Evening digest — sector + market news for your stocks, 18:30 Mon–Fri
📅 Weekly review — your week vs the KLCI, Fri 20:00
🗞️ Malaysia headlines — general news index, 09:00 · 14:00 · 21:00 daily
```
Turn their answer into ONE `set` call (it saves and updates the schedules):
```bash
$S set weekly=off digest.time=19:00 headlines.times=08:00,20:00 alerts.hours=9-17 --no-apply
```
Settable keys: `deliver alerts.enabled alerts.days alerts.hours digest.enabled digest.days digest.time
weekly.enabled weekly.day weekly.time headlines.enabled headlines.times` (`<message>=on|off` is
shorthand for `.enabled`). Days: `weekdays` | `daily`. Times: `19:00`, `7pm`, `6:30pm`. Alert hours:
`9-17` = scans at :00 and :30 from 09:00 to 17:30. Headline times must share the same minute.
A `✗ …` line means a bad value — tell them why, in plain words, and ask again.

**5. Finish.** Run `$S finish`, then `$S test`. `finish` saves, updates the Hermes jobs and prints a
summary — paste it back as-is. `test` sends a test message to the chosen app; if they picked another app,
ask them to confirm it arrived there. End with one line: *"You'll hear from me when there's news. Say
/setup any time to change this."*

## Later changes (already set up)

One command, then confirm in one line:

| User says | Run |
|---|---|
| "I bought KPJ" / "add Inari" | `$S add "KPJ"` |
| "watch Top Glove" | `$S add "Top Glove" --watch` |
| "I bought my watchlist stock X" | `$S add "X"` (moves it to holdings) |
| "I sold IJM" / "stop watching X" | `$S remove IJM` |
| "digest at 7pm" | `$S set digest.time=19:00` |
| "no more weekly review" | `$S set weekly=off` |
| "alerts on weekends too" | `$S set alerts.days=daily` |
| "send my alerts to Discord" | `$S set deliver=discord` then `$S test` |
| "what am I getting?" | `$S status` |

`set` without `--no-apply` also updates the Hermes cron jobs. If its output contains `⚠️ … not found`,
the scheduled jobs haven't been created on this machine — tell the user to run
`hermes/cron/create-jobs.sh` (a one-time step on the laptop), don't try to create jobs from chat.

## Good to know

- A stock added after setup gets a one-off **catch-up alert** on the next scan: news from the last
  3 days that names it. Mention this when adding a stock mid-week so it isn't a surprise.
- The very first scan after onboarding is silent: it records what's already published so the user
  isn't flooded with old news. Alerts start with the next new story.

## Rules

- ≤ 3 tool calls per reply. Don't read source files or YAML to double-check — `status` is the truth.
- Never invent stock codes or sectors; everything comes from `add` / `find` / `sectors` output.
- Quantities and prices the user mentions are not needed for alerts — don't ask for them.
