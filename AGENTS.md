# AGENTS.md

BuildStack Construction — a small construction-business back office and public site.
FastAPI + Postgres + Jinja, deployed to Railway. ~50 flat Python modules at the repo root.

**Read `MEMORY.md` before touching business logic.** It documents decisions that are not
recoverable from the code: phone numbers, the business-contacts-only boundary, Stripe
account history, why Skip Trace is a deliberate exception, why Accurate Append is blocked.
Treat it as authoritative and keep it current.

---

## Commands

**Only `venv/bin/python` works.** Python 3.10.11, has `fastapi`/`psycopg`/`jinja2`/`openai`
*and* pytest. `.venv/` and system `python3` are 3.14 with none of those — imports fail.
`pytest` is not in `requirements.txt` (runtime deps only); it exists solely in `venv/`.

```bash
venv/bin/python -m uvicorn construction_main:app --reload   # dev server
venv/bin/python -m pytest -v                                 # 342 tests — see the trap below
venv/bin/python test_sql_safety.py                           # script-style: one file at a time
venv/bin/python -m pytest test_lead_pipeline_tools.py -v     # single file
venv/bin/python -m pytest test_model_unavailable.py::test_copilot_raises_on_empty_credits -v
```

**The trap: `pytest` alone runs less than half the suite.** 36 `test_*.py` files, and there is
no `conftest.py`, `pytest.ini`, or `pyproject.toml` to unify them.

- 19 files are pure pytest/unittest — collected normally. 342 tests total.
- 24 files carry an `if __name__ == "__main__":` block. For **17** of those the tests live
  *inside* that block, so **pytest collects 0 from them and still reports success.** The other
  7 have tests at module level too and are collected normally, so the guard alone is not the
  signal — test what pytest actually collects.

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

No CI exists (`.github/` is absent) — nothing runs these for you.

### Known failures at HEAD `1a42bcc` (clean tree)

Do not assume you broke these:

- `test_flag_semantics.py` → `AttributeError: module 'construction_main' has no attribute '_disabled'`
  The test expects a `_disabled()` helper; the source still uses bare
  `os.getenv("DISABLE_*")` truthiness at `construction_main.py:673,680,686`. Test is stale.
- `test_sql_safety.py` → 2 failures. Its AST scan flags `%s` inside quoted SQL literals and a
  pattern still present in `copilot_tasks`.

`test_schema_safety`, `test_send_guard`, `test_template_js` pass. `test_template_js` needs
`node` on PATH (exit 2 if absent).

- `test_open_geo.py::test_geocode_survives_a_broken_upstream` fails **only when this machine
  has working outbound network.** It stubs `_nominatim` to raise, but `open_geo.geocode()`
  tries the Census/`zip_lookup` path *first* (`open_geo.py:104-120`) and that path is not
  stubbed — so it makes a live call, gets a hit, and returns `ok=True`. The test's premise
  only holds offline. Incomplete mocking, not a regression.

### `test_site_smoke.py` is the only real-DB test — don't run it casually

It **writes rows** and imports the app, which fires the `lifecycle` startup and all background
threads. It `setdefault`s `DISABLE_PERMIT_IMPORT=1`, `DISABLE_LEAD_SCAN=1`,
`DISABLE_LOAN_OUTREACH=1` for that reason. Prior runs seeded 24 junk leads, 6 junk permits and
1,598 scraped `job_leads` into a live database. Always point it at a scratch DB:

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

`construction_main.py` is ~8,100 lines with **161 inline route handlers** and no `APIRouter`
— one module scope, divided by `# --- Section ---` banner comments. Notable blocks:
`lifecycle()` at `:304` (startup DDL + every background thread), `app` at `:711`,
AI tool handlers `:995-1810`, pipeline board `:2564-3836`, live voice/SWML agent `:4230-5158`.

Everything else is a flat sibling module. Read `MEMORY.md` plus the relevant module rather
than expecting a clean layered architecture — there isn't one.

### Database

`psycopg` v3, raw SQL, no ORM, **no connection pool**. `get_db()` (`construction_main.py:743`)
opens a connection per request; ~45 modules open their own ad hoc.

`DATABASE_URL` (27 call sites, fallback at `:76`). `CON_DATABASE_URL` overrides it in
`google_oauth.py` only.

**Schema is created at startup**, not migrated: the lifespan runs a large idempotent
`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block
(`:308-661`), plus `_ensure_schema` in `inbound_email`, `copilot_memory`, `copilot_tasks`.
~12 other modules lazily self-init their own tables on first use.

That whole DDL block is **one transaction**. An `ALTER` placed before its `CREATE` aborts the
entire block and rolls back all of it — there's an explicit comment about this past bug at
`:383-399`. `test_schema_safety.py` pins it: DDL must route through `_safe_ddl`, retry a lock
deadlock once, and tolerate a missing `leads` table.

### AI agents — handlers and schemas are reconciled by hand

Schemas live in `con_ai_agent.py` (`_safe_tools:230` guest subset, `_full_tools:521` copilot,
`_operator_tools:354` copilot-only). Handlers live in `construction_main.py`
(`build_tool_handlers:995`, `build_copilot_handlers:1767`) and `copilot_ops.py`.

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
`con_ai_agent.py:82-83` concatenates `bot_knowledge.md` + `construction_knowledge.md` into the
prompt, and `construction_knowledge.md` §15 ("Available owner tools") hand-lists the toolkit in
prose. Nothing checks it, and **it is currently drifted** as of `1a42bcc`: it advertises
`send_email`, which no longer exists under any name, and omits the whole permit pipeline
(`skip_trace_owner`, `lookup_zip`, `pdl_contact`, `save_contact`, `draft_lead_email`) that
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

`OPENAI_BASE_URL=https://opencode.ai/zen/v1` repoints every chat-completions call in
`BusinessAIAgent` at Zen. No code change: the OpenAI SDK reads that variable when the
client is built. The key still goes in `OPENAI_API_KEY`. This does **not** move the
voice path (`construction_main.py` builds its own client) or `web_search`, which uses
the Responses API and stays on OpenAI.

On a rate limit, 5xx or timeout, `BusinessAIAgent._complete` falls through
`COPILOT_FREE_FALLBACK_MODELS` (default `space-bunny-free`, `big-pickle`,
`ling-3.1-flash-free`, `nemotron-3.5-lightning-free`). Two deliberate limits:

- The free tier engages **only** on Zen. `opencode.ai` with an OpenAI key is a
  guaranteed 401, so off-Zen there is nothing to fall back to.
- An **auth** failure is never retried — a wrong key fails on every model, so
  retrying only multiplies one clear error.

The default list will rot: Zen documents every free model as "available for a limited
time". Re-check `https://opencode.ai/zen/v1/models` and update it. Model quality on the
free tier is unproven for this workload — general chat models are being asked to drive
161 tool handlers, so the tool-call path is only as good as the stealth model behind it.

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
`pnc_statement.pdf` (real bank statements), `LOAN_PACKAGE.pdf`, `docs/SBA_7a_LOAN_PITCH.pdf`,
and `docs/INVOICE_*` / `RECEIPT_*` / `OWNER-EQUITY-STATEMENT` / `NO-EMPLOYEE-OWNER-LETTER`
HTML, which name real customers (Barbara Mastic, Carl Simmons) and carry the EIN.
`static/listing/projects/*.jpg` are the real before/after job photos that the portfolio page
renders — `test_site_smoke.py` will fail if templates stop resolving them.

**Untracked but NOT gitignored — `git add -A` would commit these:**

| Path | What |
|---|---|
| `Hampton_Roads_Regional_Parcels.csv` | 179 MB of parcel/owner data |
| `static/listing/projects/`, `static/listing/past jobs.jpeg` | real job photos |
| `pnc_statement copy.pdf`, `pnc_statement copy 2.pdf` | duplicate bank statements |
| `fe898c9f-... (1).html`, `static/listing/compressed-files.pdf` | scraped artifacts |
| `my_agent/` | agent config |
| `Dockerfile.enrich`, `enrich_api.py` | work in progress |

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
- Python 3.10 target in `venv/`, 3.12 in the Dockerfile (`Dockerfile`, `python:3.12-slim`). Keep
  syntax compatible with 3.10.
- Deploy is Railway, RAILPACK builder, `uvicorn construction_main:app` (`Procfile`).
  `railway.json` watches only `construction_main.py` for rebuilds.