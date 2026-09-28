# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through
GitHub: **Security → Report a vulnerability** on this repository. You'll get a reply within a
week. Once a fix is out you're credited, unless you'd rather not be.

## What's in scope

- `bootstrap.sh`, `hermes/install.sh` and `hermes/cron/create-jobs.sh`: anything that runs
  unexpected commands, writes outside the project or `~/.hermes`, or uses `sudo` without asking.
- The pipeline and agents: prompt injection from news content that makes a message link to
  something other than the stored item, or makes the chat agent run commands it shouldn't.
- The local dashboard (`pipeline/dashboard`): it binds to `127.0.0.1` and is read-only. Anything
  that lets it write, or exposes it beyond loopback by default, is a bug.

Vulnerabilities in Hermes Agent itself belong to
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).

## Keeping your install safe

- Your API keys and bot tokens live in `~/.hermes/.env`, never in this repo. Don't paste them
  into issues.
- `data/portfolio.yaml`, `data/preferences.yaml`, `data/state/` and `data/logs/` are gitignored
  because they describe what you own. Leave them out of forks and bug reports.
- If you put the dashboard on the internet (a tunnel or reverse proxy), put authentication in
  front of it.
- Read `bootstrap.sh` before piping it to `bash` if you'd rather not trust it blindly. The README
  shows the manual steps too.
