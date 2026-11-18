# Working in this repository

`AGENTS.md` and `MEMORY.md` are authoritative. Read `AGENTS.md` first, then
`MEMORY.md` before changing business logic.

## Production

**DigitalOcean only** — Droplet `bizstack-prod` (`159.203.175.39`), Docker
Compose with Caddy. Deploy is manual (`rsync` + `docker compose build` +
`docker compose up -d`); there is no auto-deploy. Do not suggest or add any
other hosting provider, and do not add deploy workflows.

## Two agents can share this checkout

More than one AI agent may be working in this repository at the same time.

- Work on a branch or in a separate `git worktree`; never rewrite history or
  force-push.
- **Stage only the paths you changed** (`git add path/one.py path/two.py`).
  Never `git add -A`, `git add .`, or `git commit -a` — that commits another
  agent's half-finished work.
- Before editing, run `git status` and `git diff <file>`. Changes you did not
  make belong to someone else: leave them, do not revert, stash or delete them.
- If a file is listed under *Active work* below, do not edit it this session.
- `git commit` occasionally fails with `fatal: /: '/' is outside repository`;
  retry once.

## Active work

Check `git log --oneline -5` and `git status` for the current state. The list
below is a courtesy, not a lock; it is updated by whoever is working.

- 2026-11-18 — `html_text.py`, `mail_inbox.py`, `scripts/clean_mail_bodies.py`,
  `test_html_text.py`: HTML mail flattening and the one-off stored-body repair
  (OpenCode agent). Read-only elsewhere.
