# AGENTS.md

BuildStack Construction — a small construction-business back office and public site.
FastAPI + Postgres + Jinja, deployed to Railway. ~50 flat Python modules at the repo root.

**Read `MEMORY.md` before touching business logic.** It documents decisions that are not
recoverable from the code: phone numbers, the business-contacts-only boundary, Stripe
account history, why Skip Trace is a deliberate exception, why Accurate Append is blocked.
Treat it as authoritative and keep it current.

---

## This is ONE of TWO separate companies — do not merge them

BuildStack Construction and BuildStack Hosts (`../bizstack-hosts`) are **separate businesses
with separate domains, separate Railway projects and separate databases.** They were
un-separated on 2026-10-11. They are not a monorepo, not a shared package, and not two
services over one database.

| | Construction (this repo) | Hosts |
|---|---|---|
| Railway project | **BuildStack Construction** `281375d7-2f13-43bb-813d-c6f048ead3f3` | **BuildStack Hosts** `d5ed0396-4729-442c-b38e-cdeedb2fe815` |
| App service | `Construction` `f0831991-898a-46a2-8b96-35bf89af912f` | `Hosts` `9ff95d24-ad4c-41c1-a217-a5d1d663aae3` |
| Postgres | `86bda89f-4620-44ef-b450-612ca2de6f11`, db `construction` | `5027eac7-620c-4cc0-a331-e99263bef206`, db `hosts` |
| Domain | `construction.bizstackperks.com` | `bizstackperks.com` |
| Repo | `get-them666/bizstack-construction` | `get-them666/bizstack-hosts` |

**Rules that follow from that:**

- **Never add a dependency on another repository.** `requirements.txt` must not name a git
  URL. The lead pipeline was `bizstack-leads @ git+...` until `3b4fd69`, which vendored it to
  `bizstack_leads/` in this tree. That package is **dead** — nothing consumes it. Do not
  reintroduce it, and do not import from `../bizstack-hosts`.
- **Never point this app at the hosts database, or vice versa.** Each has one `DATABASE_URL`
  and they differ.
- **Never assume a file exists in both repos because it used to.** 63 paths were duplicated
  across the two; 31 had already drifted. They now evolve independently and will keep
  diverging. That is correct, not a bug to fix by syncing.
- **`railway up` from this directory deploys to BuildStack Construction.** The CLI links are
  per-directory and were repointed on 2026-10-11; confirm with `railway status` before deploying.

**Still not fully separated — know this before you assume otherwise.** `construction_main.py`
was forked from the hosts monolith and still contains the entire Broom/hosts application:
292 references to `crew`/`timesheets`/`payroll_runs`/`clock_events`, 30 `broom` mentions, the
crew admin and training-quiz routes, and `construction_main.py:2194` `# Broom - check crew
table` in the shared Google OAuth callback (`:2222` then routes a non-construction sign-in
into it). Both apps therefore still create all 52 tables. The *data* is fully split; the *code*
is not. Extracting it is a real refactor of a live 8,176-line app — see `MEMORY.md`.

---

## Commands

**Only `venv/bin/python` works.** Python 3.10.11, has `fastapi`/`psycopg`/`jinja2`/`openai`
*and* pytest. `.venv/` and system `python3` are 3.14 with none of those — imports fail.
`pytest` is not in `requirements.txt` (runtime deps only); it exists solely in `venv/`.

```bash
venv/bin/python -m uvicorn construction_main:app --reload   # dev server
npm run css                                                  # rebuild static/css/tailwind.css after editing templates
venv/bin/python -m pytest -v                                 # 342 tests — see the trap below
venv/bin/python test_sql_safety.py                           # script-style: one file at a time
venv/bin/python -m pytest test_lead_pipeline_tools.py -v     # single file
venv/bin/python -m pytest test_model_unavailable.py::test_copilot_raises_on_empty_credits -v
```

**The trap: `pytest` alone runs less than half the suite.** 39 `test_*.py` files, and there is
no `conftest.py`, `pytest.ini`, or `pyproject.toml` to unify them.

- 22 files are pure pytest/unittest — collected normally. **395 tests total.**
- The other 17 files carry an `if __name__ == "__main__":` block with the tests *inside* it,
  so **pytest collects 0 from them and still reports success.** Some carry module-level tests
  too and are collected normally, so the guard alone is not the signal — test what pytest
  actually collects.

Zero-collected — run these by hand, or they are simply never run: `test_bid_inquiry`
`test_contact_policy` `test_email_send_accuracy` `test_federal_block` `test_flag_semantics`
`test_greeting` `test_lob_send` `test_mail_letters` `test_manual_touch` `test_permit_sources`
`test_schema_safety` `test_scrap_io` `test_send_guard` `test_site_smoke` `test_sql_safety`
`test_template_js` `test_vision_quote`.

Two passes, and treat a green `pytest` as necessary but not sufficient:

```bash
venv/bin/python -m pytest -q
venv/bin/python -m pytest --collect-only -q | grep '::' | cut -d: -f1 | sort -u > /tmp/collected
for f in test_*.py; do
  grep -qx "$f" /tmp/collected || venv/bin/python "$f" --quiet || echo "FAILED: $f"
done
```

No test CI exists — nothing runs these for you. The only workflow in `.github/` is a
manual-only Cloud Run deploy that must **never** get a `push:` trigger again (see Deploy below).

### Known failures at HEAD `3b4fd69` (clean tree)

Do not assume you broke these — all five were failing before the separation:

- `test_flag_semantics.py` → `AttributeError: module 'construction_main' has no attribute '_disabled'`
  The test expects a `_disabled()` helper; the source still uses bare
  `os.getenv("DISABLE_*")` truthiness. Test is stale.
- `test_sql_safety.py` → 2 failures. Its AST scan flags `%s` inside quoted SQL literals and a
  pattern still present in `copilot_tasks`.
- `test_lob_send.py` → its real-batch assertions need `/tmp/letters_final/manifest.csv`,
  an artifact of the already-mailed 177-letter run. Fails anywhere but the original machine.
- `test_site_smoke.py` → 2 failures on permit-card assertions; see the warning below.
- `test_open_geo.py::test_geocode_survives_a_broken_upstream` fails **only when this machine
  has working outbound network.** It stubs `_nominatim` to raise, but `open_geo.geocode()`
  tries the Census/`zip_lookup` path *first* (`open_geo.py:104-120`) and that path is not
  stubbed — so it makes a live call, gets a hit, and returns `ok=True`. The test's premise
  only holds offline. Incomplete mocking, not a regression.

`test_schema_safety`, `test_send_guard`, `test_template_js` pass. `test_template_js` needs
`node` on PATH (exit 2 if absent).

### `test_site_smoke.py` is the only real-DB test — don't run it casually

It **writes rows** and imports the app, which fires the `lifecycle` startup and all background
threads. It `setdefault`s `DISABLE_PERMIT_IMPORT=1`, `DISABLE_LEAD_SCAN=1`,
`DISABLE_LOAN_OUTREACH=1` for that reason, and it `setdefault`s
`DATABASE_URL=postgresql://localhost/bz_scratch` — so with `DATABASE_URL` unset in your
shell it lands on a disposable scratch DB, **not** production. Prior runs seeded 24 junk
leads, 6 junk permits and 1,598 scraped `job_leads` into a live database. Always be explicit:

```bash
createdb bz_test
DATABASE_URL=postgresql://$(whoami)@localhost:5432/bz_test venv/bin/python test_site_smoke.py
```

Set `DATABASE_URL` **before** the import, not after. `test_permit_sources.py` hits live
permit feeds only when `PERMIT_LIVE=1`.

### Other environment gotchas

- No test calls `load_dotenv()`, so `.env` is not read by the suite. Tests use
  `monkeypatch.setenv`. `PDL_API_KEY` in `.env` is real — its presence changes module-level
  behaviour in `pdl_contact_service`.
- The suite defends against live network calls by monkeypatching the network entry point to a
  raiser (e.g. `test_property_service.py` patches `property_service._api_call`). RentCast's free
  tier is 50 calls/**month**, so never let a test reach the real API.
- Outbound TLS from this machine needs `SSL_CERT_FILE=/etc/ssl/cert.pem`.
- `railway run` executes **locally**, so `*.railway.internal` never resolves from your machine.
  Use a tcp-proxy to reach the live DB, then close and delete the URL.
- `docker build` cannot run here — the Docker daemon isn't running, so Dockerfile edits are
  unverified locally.

---

## Architecture

`construction_main.py` is 8,176 lines with **169 inline route handlers** and no `APIRouter`
— one module scope, divided by `# --- Section ---` banner comments. Notable blocks:
`lifecycle()` at `:312` (startup DDL + every background thread), `app` at `:733`,
AI tool handlers `:1017-1830`, lead pipeline board `:2267+`, live voice/SWML agent `:4275+`
(its own OpenAI client at `:4872`).

Everything else is a flat sibling module. Read `MEMORY.md` plus the relevant module rather
than expecting a clean layered architecture — there isn't one.

### Database

`psycopg` v3, raw SQL, no ORM, **no connection pool**. `get_db()` (`construction_main.py:765`)
opens a connection per request; ~45 modules open their own ad hoc.

`DATABASE_URL` (27 call sites, fallback at `:83`). `CON_DATABASE_URL` overrides it in
`google_oauth.py` only. This is **construction's own database** (`construction` in Railway
project `281375d7-…`); it is not shared with hosts. `max_connections` was raised to 500 —
the old shared server ran at 500, and the new one defaulted to 100, which is too tight for an
app that opens a connection per request.

**Schema is created at startup**, not migrated: the lifespan runs a large idempotent
`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block
(`:318-667`), plus `_ensure_schema` in `inbound_email`, `copilot_memory`, `copilot_tasks`.
~12 other modules lazily self-init their own tables on first use.

That whole DDL block is **one transaction**. An `ALTER` placed before its `CREATE` aborts the
entire block and rolls back all of it — there's an explicit comment about this past bug at
`:393-409`. `test_schema_safety.py` pins it: DDL must route through `_safe_ddl`, retry a lock
deadlock once, and tolerate a missing `leads` table.

### AI agents — handlers and schemas are reconciled by hand

Schemas live in `con_ai_agent.py` (`_safe_tools:258` guest subset, `_full_tools:549` copilot,
`_operator_tools:382` copilot-only). Handlers live in `construction_main.py`
(`build_tool_handlers:1017`, `build_copilot_handlers:1790`) and `copilot_ops.py`.

**Nothing enforces that the two agree.** A handler with no schema is dead code that looks
wired; a schema with no handler is a button whose only outcome is `No handler for tool`. The
model *reports a missing capability* rather than erroring, so the only symptom is it quietly
saying it cannot do the thing.

When you add a Copilot tool, update **both** and check both directions:

```python
advertised = {t["function"]["name"] for t in BusinessAIAgent(subset="copilot")._tools()}
handlers   = set(build_copilot_handlers(db, owner_email))
dead       = advertised - handlers   # can only fail
unreachable= handlers - advertised   # does nothing, silently
```

`test_lead_pipeline_tools.py` pins this for the permit pipeline. When *removing* a tool, remove
its schema too — `send_email_message`'s handler was popped from the Copilot on 2026-10-03
because it looped and sent three identical emails, but its schema was left behind and offered
the model a send button that could only error.

**There is a third place: the knowledge files are the system prompt.**
`con_ai_agent.py:110-111` concatenates `bot_knowledge.md` + `construction_knowledge.md` into
the prompt, and `construction_knowledge.md` §15 ("Available owner tools") hand-lists the toolkit
in prose. Nothing checks it, and **it is currently drifted** as of `3b4fd69`: it advertises
`send_email`, which no longer exists under any name, and omits the whole permit pipeline
(`skip_trace_owner`, `lookup_zip`, `contact_lookup`, `save_contact`, `draft_lead_email`) that
`6f5b336` made reachable. So a tool is live only when all three agree: **schema
(`con_ai_agent.py`) + handler (`construction_main.py`/`copilot_ops.py`) + prompt
(`construction_knowledge.md` §15).**

```bash
# what the model is actually offered, vs what the prompt claims it can use
venv/bin/python -c "from con_ai_agent import BusinessAIAgent; \
print(sorted(t['function']['name'] for t in BusinessAIAgent(subset='copilot')._tools()))"
```

Env: `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-4o-mini`), `OPENAI_VOICE_MODEL`,
`OPENAI_SEARCH_MODEL`, `SEARCH_BLOCKED_DOMAINS`, `COPILOT_TOOL_ATTEMPTS` (default 2),
`COPILOT_TOOL_DEADLINE_SECONDS` (default 90). Search uses the **Responses API** —
`gpt-4o-mini-search-preview` and the `web_search_preview` tool type were **shut down
2026-07-23**; do not reintroduce them.

### Retargeting the model at OpenCode Zen

`COPILOT_BASE_URL=https://opencode.ai/zen/v1` repoints the Copilot at Zen. Deliberately
**not** `OPENAI_BASE_URL`: that would drag the voice path and `web_search` along too.
Voice builds its own client (`construction_main.py:4827`) and search uses the Responses
API — neither is Zen-compatible, and both stay on OpenAI.

**Zen's free models need no key.** They authenticate with an *empty* bearer: verified
live on 2026-10-10, `POST /zen/v1/chat/completions` returns 200 with no `Authorization`
header and 401 the instant a non-empty one is sent. Two consequences:

- `OPENAI_API_KEY` is optional on Zen. The old `if not os.getenv("OPENAI_API_KEY"):
  return fallback` gate meant a keyless run never called a model at all and returned the
  polite fallback — an assistant that looks alive and does nothing.
- The placeholder in `~/.local/share/opencode/auth.json` (`opencode` → the literal
  string `"opencode auth login"`) was **causing** the 401, not missing a credential to
  fill in. Do not "fix" it by pasting a real key; the free tier needs none.
- The SDK refuses to construct with an empty `api_key`, hence a dummy plus
  `default_headers={"Authorization": ""}` in the `client` property. Off Zen a missing
  key still raises.

Tool calling works on the free tier — confirmed end-to-end against a live 41-tool
Copilot prompt, which ran `list_leads` → `calculate` and answered correctly. These are
still general chat models though, and they **loop**: two separate live runs re-called a
tool it had already been given, tripping `ToolBudgetExhausted` → HTTP 502. That guard is
the reason this is visible rather than an infinite grind, but budget failures on Zen are
usually model confusion, not a broken provider.

On a rate limit, 5xx or timeout, `BusinessAIAgent._complete` falls through
`COPILOT_FREE_FALLBACK_MODELS` (default `space-bunny-free`, `big-pickle`,
`ling-3.1-flash-free`, `nemotron-3.5-lightning-free`). Two deliberate limits:

- The free tier engages **only** on Zen. `opencode.ai` with an OpenAI key is a
  guaranteed 401, so off-Zen there is nothing to fall back to.
- An **auth** failure is never retried — a wrong key fails on every model, so
  retrying only multiplies one clear error.

The default list will rot: Zen documents every free model as "available for a limited
time". Re-check `https://opencode.ai/zen/v1/models` and update it.

`ToolBudgetExhausted` → HTTP 502, `ModelUnavailable` → HTTP 503. These are surfaced to the
owner on purpose, not swallowed. Everything else returns a soft fallback.

### Background work

No APScheduler, no cron, no `BackgroundTasks` — hand-rolled `threading.Thread(daemon=True)`
loops started from `lifecycle()`: permit importer (`PERMIT_SCAN_HOURS`, default 24), lead-source
scheduler (6h), Gmail inbound poll (900s), acquisition radar, bid-inquiry firing, and
`loan_outreach`'s hourly outreach/reply loops. Each start is individually try/except'd so one
failure doesn't kill startup.

Kill switches: `DISABLE_PERMIT_IMPORT`, `DISABLE_LEAD_SCAN`, `DISABLE_LOAN_OUTREACH`.
Use `_disabled()`-style explicit parsing — Railway stores unset flags as the **string
`"false"`**, which is truthy, so bare `os.getenv(...)` truthiness silently disables nothing
or everything. This is the exact bug `test_flag_semantics.py` exists for.

### Outbound messaging

`auto_reply.py` is the **guardrail layer** — contact-once-then-5-days (`contact_policy_allows`),
per-recipient cooldown, daily caps, federal-domain block, review mode. It fails closed.
Read it before adding any send path. Actual delivery is `documents_service.send_email`
(`EMAIL_TRANSPORT`: `gmail` default / `resend` / `smtp`).

`send_sms_message` is still fully wired to the Copilot with no rate limit, dedupe, allowlist,
or confirmation — the same hazard profile as the withheld email tool, and the open question.

---

## Real customer value — do not lose or commit this

Much of this repo is real money and real people's data. It is **not** reproducible.

**Already tracked in git (irreversible — treat as public):**
`pnc_statement.pdf` **and both `pnc_statement copy*.pdf` duplicates** (real bank statements),
`send_today/LOAN_PACKAGE.pdf` (the active loan package — note it is **not** at the repo root),
`stale/LOAN_PACKAGE.SUPERSEDED-2026-10-06.pdf`, `docs/SBA_7a_LOAN_PITCH.pdf`, and
`docs/INVOICE_*` / `RECEIPT_*` / `OWNER-EQUITY-STATEMENT` / `NO-EMPLOYEE-OWNER-LETTER`
HTML, which name real customers (Barbara Mastic, Carl Simmons) and carry the EIN.

**`static/listing/projects/` — 63 real before/after customer job photos — are also tracked
and pushed.** They were added in `afa662c` (not by the separation work) and `test_site_smoke.py`
will fail if the templates stop resolving them. Treat the repo as public and do not add
further customer media; if photo privacy matters, that needs a hosting decision, not a commit.

**All loan paperwork belongs to this company and must stay here.** The hosts repo has no PDFs
at all. Loan data in the database (`outreach_touches`, `outreach_replies`, `ledger_entries`)
lives in the `construction` database only.

**Untracked but NOT gitignored — `git add -A` would commit these:**

| Path | What |
|---|---|
| `Hampton_Roads_Regional_Parcels.csv` | 179 MB of parcel/owner data |

That is the *only* remaining untracked file. Everything else previously listed here
(`static/listing/projects/`, `past jobs.jpeg`, `my_agent/`, `Dockerfile.enrich`,
`enrich_api.py`, `compressed-files.pdf`) has since been committed and is already public.

**Correctly gitignored — never commit, never delete:**
`outbox/` (278 generated letters with real customer addresses and names — regenerate with
`mail_letters.py`, but the batch is unrepeatable), `docs/stripe/` (real customer names in charge
metadata), `.env`, `SESSION_STATE.md`, `SESSION_NOTES.md`, `.agents/`, `.claude/`,
`opencode.json`, `eva_intel.json` (regenerate with `python eva_intel.py`).

**Never delete anything in this repo without asking.** The photos are the customer's record of
the work, the letters have already been mailed, and the PDFs are loan evidence. If something
looks like clutter, it is probably a source artifact for `docs/CHECKLIST.md`.

---

## Conventions

- Two phone numbers, and widening either without asking is a real error: `757-908-7121` is the
  business line on every homeowner- and customer-facing surface; `252-665-5891` is Shaun's
  personal mobile and belongs **only** in lending material. See `MEMORY.md`.
- Lead sourcing is **business contacts only**, enforced in
  `copilot_ops._BLOCKED_CONTACT_DOMAINS` (rides in `web_search`'s `blocked_domains`, not in a
  prompt — a prompt instruction is a preference the model can be talked out of).
  `Skip Trace` is the one deliberate, owner-approved exception, and every lookup is written to
  `skiptrace_audit`.
- New provider layers in the Skip Trace waterfall must return `NOT_FOUND` for both 400 and 404.
  RentCast uses those for "can't parse" and "not in my database", which for a tier-1 provider is
  most of Virginia. Raising on non-200 aborts the waterfall and breaks exactly the addresses
  that need the fallback.
- `construction_main.py` is generated-adjacent and huge; prefer adding a focused module with its
  own `ensure_schema()` over growing it.
- **CSS is a static Tailwind v3 build, not the Play CDN.** `base.html` and `app.html` link
  `/static/css/tailwind.css` (built from `tailwind.config.js`, scanning `templates/**`, `*.py`
  and `static/**/*.js`). The Docker image has no node, so the built file is **committed** —
  after adding a class a template has never used, run `npm run css` and commit the result,
  or that class renders unstyled in production. `?v=` on the link is the file's mtime at boot.
- Python 3.10 target in `venv/`, 3.12 in the Dockerfile (`Dockerfile`, `python:3.12-slim`). Keep
  syntax compatible with 3.10.
- Deploy is Railway, **Dockerfile** builder (`Dockerfile`, `python:3.12-slim`,
  `uvicorn construction_main:app`), not RAILPACK. `railway.json`'s
  `build.watchPatterns` decides which commits trigger a rebuild, and Railway
  **skips** any commit that touches none of them (`SKIPPED — "No changes to
  watched files"`). That list was a hand-maintained 19 files covering a small
  slice of the ~60 flat modules, so a commit touching only an unwatched module
  (e.g. `lead_enrich_loop.py` on 2026-10-21) silently never shipped. It is now
  broad globs (`**/*.py`, `**/*.md`, `templates/**`, `static/**`, plus
  `requirements.txt`/`Dockerfile`/`Procfile`/`railway.json`). **Keep it broad** —
  this is one service; there is nothing to gate. After a push, confirm with
  `railway deployment list --json` that the newest deployment is `SUCCESS`, not
  `SKIPPED`.

  **The second way a push gets `SKIPPED`: a failing GitHub check.** The service has
  "Wait for CI" on (`source.checkSuites: true`), so any red check suite on the commit
  makes Railway skip it with `meta.skippedReason: "CI check suite failed"`. From
  2026-10-08 to 2026-10-29 a `Deploy to Cloud Run` workflow with a `push:` trigger and
  no GCP variables failed in 8s on every commit, and **every git deploy in that window
  was skipped** — the only deploys that shipped were manual `railway up`. The workflow is
  now `workflow_dispatch` only. Read `skippedReason` before blaming `watchPatterns`.