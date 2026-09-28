# Contributing

Thanks for helping. This is a small project with one maintainer, so short, focused pull requests
get reviewed fastest.

## Ways to help

- **Report a bug**: open an issue with the output of `python -m pipeline.setup status` and
  `python -m pipeline.log tail --level WARN -n 30`. Remove anything personal first.
- **Fix a noisy or missed alert**: most of these are data, not code. Aliases live in your
  `data/portfolio.yaml`; sector keywords and announcement filters live in `data/sectors.yaml`.
  A PR that improves `sectors.yaml` for everyone is very welcome.
- **Add a sector**: add a block to `data/sectors.yaml` with a `label`, 1–3 Google News `queries`,
  `keywords`, and `match` (the Yahoo Finance industry names that should map to it).
- **Add a news source**: add an adapter to `pipeline/fetchers.py` that returns the normalised item
  dict described at the top of that file. It needs a test and a note on the source's terms.

## Development setup

```bash
git clone https://github.com/hanzong111/Personal-News-Agent.git
cd Personal-News-Agent
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/python -m pytest -q
```

You don't need Hermes Agent, an API key or network access to run the tests. The suite uses fakes
and a fixture portfolio (`tests/fixtures/portfolio.yaml`), so it never touches your own data. CI
runs the same tests on Python 3.10–3.12, plus `shellcheck` on the shell scripts.

To try the whole pipeline without sending anything:

```bash
./.venv/bin/python -m pipeline.scan --dry-run      # what would alert right now
./.venv/bin/python -m pipeline.digest --dry-run    # tonight's digest
```

## Ground rules for code

These rules keep the messages trustworthy and the costs low. Please keep them:

1. **No model ever writes a URL, time or price into a message.** Agents return JSON keyed by item
   id; `agents/renderer.py` fills in facts from stored data.
2. **Relevance is code first.** Alias and keyword matching decide tier 1/2. A model only sees
   items the rules couldn't place.
3. **Pending work is never dropped.** An item advances in `news.db` only after its message has
   been written to stdout.
4. **Be polite to sources.** Keep `BURSA_KLSE_CRAWL_DELAY`, never add article-body scraping to
   the alert path, and prefer RSS or official APIs.
5. **Tests don't touch the network or real files.** Mock `fetchers.*` and `agents.llm.call_json`.

Match the style of the surrounding code: short docstrings that say *why*, plain names, no new
dependencies without a good reason.

## Pull requests

- One topic per PR, with tests for behaviour changes.
- `python -m pytest -q` and `shellcheck -S warning bootstrap.sh hermes/**/*.sh` pass.
- Update the README when you change a command, a setting or a default.
- Add a line to `CHANGELOG.md` under **Unreleased**.

By contributing you agree that your work is released under the [MIT License](LICENSE).
