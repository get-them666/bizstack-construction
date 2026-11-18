# AGENTS.md

BuildStack Construction — a small construction-business back office and public site.
FastAPI + Postgres + Jinja, deployed to **DigitalOcean** (Docker on Droplet `bizstack-prod`).
~190 flat Python modules at the repo root.

> **Production is DigitalOcean.** The Droplet is `bizstack-prod` at `159.203.175.39`,
> running Docker Compose with Caddy as reverse proxy. Two apps (`bizstack-construction`
> and `bizstack-hosts`) each with their own Postgres 18 database (pgvector-enabled).
> Deploy is `rsync` + `docker compose build` + `docker compose up -d`. There is no other
> hosting path.

**Read `MEMORY.md` before touching business logic.** It documents decisions that are not
recoverable from the code: phone numbers, the business-contacts-only boundary, Stripe
account history, why Skip Trace is a deliberate exception, why Accurate Append is blocked.
Treat it as authoritative and keep it current.

---

## This is ONE of TWO separate companies — do not merge them

BuildStack Construction and BuildStack Hosts (`../bizstack-hosts`) are **separate businesses
with separate domains, separate Docker containers and separate databases on the same
Droplet.** Not a monorepo, not a shared package, not two services over one database.

| | Construction (this repo) | Hosts |
|---|---|---|
| DO container | **`bizstack-construction-1`** | **`bizstack-hosts-1`** |
| Postgres container | **`bizstack-db-construction-1`**; db name `construction` | **`bizstack-db-hosts-1`**; db name `hosts` |
| Domain (via Caddy) | `construction.bizstackperks.com` | `bizstackperks.com` / `www.bizstackperks.com` |
| Repo | `get-them666/bizstack-construction` | `get-them666/bizstack-hosts` |
| Droplet | `bizstack-prod` (`159.203.175.39`, NYC3) | same Droplet |

- **Never add a dependency on another repository.** `requirements.txt` must not name a git
  URL. `bizstack_leads/` is a vendored copy of the old shared lead package and **is live** —
  13 modules import it (`from bizstack_leads import store as lead_store` is the leads board).
  Edit it here; never re-point it at a git URL or at `../bizstack-hosts`.
- **Never point this app at the hosts database, or vice versa.**
- **Never assume a file exists in both repos because it used to.** They diverge on purpose.
- **Deploy:** `rsync` the repo to `/opt/bizstack/construction/` on the Droplet, then
  `docker compose build construction && docker compose up -d construction`. Confirm with
  `docker ps` and `docker logs bizstack-construction-1 --tail 20`. There is no auto-deploy;
  every deploy is manual via SSH + `doctl compute ssh bizstack-prod`.
- The crew app (`/app`, timesheets, payroll) is **this company's** crew tooling. Broom Service
  has its own separate installable app in the hosts repo — do not touch it from here.

---

## Commands

**Only `venv/bin/python` works.** It is Python 3.12 and carries `fastapi`/`psycopg`/`jinja2`/
`openai` *and* pytest. `pytest` is not in `requirements.txt` (runtime deps only). There is no
`.venv/`; system `python3` lacks the deps.

```bash
venv/bin/python -m uvicorn construction_main:app --reload   # dev server
npm run css                                                  # rebuild static/css/tailwind.css after editing templates
venv/bin/python -m pytest -q                                 # 896 pass / 37 skip, ~17s — see trap below
venv/bin/python test_sql_safety.py                           # script-style file: run directly
venv/bin/python -m pytest test_lead_pipeline_tools.py -v     # single file
```

**The trap: `pytest` alone skips 21 of the 70 `test_*.py` files.** They carry their tests
inside an `if __name__ == "__main__":` block, so pytest collects 0 from them and still
reports success. `pytest.ini` exists only to set `norecursedirs`; it does not fix this.

Zero-collected — run by hand, or they never run: `test_bid_inquiry` `test_contact_policy`
`test_deposit_atomicity` `test_email_send_accuracy` `test_email_transport_fallback`
`test_federal_block` `test_finance_intake` `test_flag_semantics` `test_founder_copy`
`test_greeting` `test_lob_send` `test_mail_letters` `test_manual_touch`
`test_permit_sources` `test_schema_safety` `test_scrap_io` `test_send_guard`
`test_site_smoke` `test_sql_safety` `test_template_js` `test_vision_quote`.

```bash
venv/bin/python -m pytest -q
venv/bin/python -m pytest --collect-only -q | grep '::' | cut -d: -f1 | sort -u > /tmp/collected
for f in test_*.py; do
  grep -qx "$f" /tmp/collected || venv/bin/python "$f" --quiet || echo "FAILED: $f"
done
```

No test CI exists. The only workflow in `.github/` is a `workflow_dispatch`-only Cloud Run
deploy that must **never** get a `push:` trigger again (see Deploy).

### Known pre-existing failures (not yours)

- `test_flag_semantics.py` → expects a `_disabled()` helper that `construction_main` never
  grew; the source still uses bare `os.getenv("DISABLE_*")` truthiness. Stale test.
- `test_sql_safety.py` → 2 failures; AST scan flags `%s` inside quoted SQL literals.
- `test_lob_send.py` → needs `/tmp/letters_final/manifest.csv` from the already-mailed
  177-letter run. Fails anywhere but the original machine.
- `test_site_smoke.py` → 2 permit-card assertion failures.
- `test_open_geo.py::test_geocode_survives_a_broken_upstream` fails **only with working
  outbound network** — incomplete mocking (`open_geo.py` tries Census/`zip_lookup` first).

`test_template_js` needs `node` on PATH (exit 2 if absent).

### `test_site_smoke.py` is the only real-DB test — don't run it casually

It **writes rows** and imports the app, firing `lifecycle` and all background threads. It
`setdefault`s `DATABASE_URL=postgresql://localhost/bz_scratch`; prior runs seeded junk leads
and 1,598 scraped `job_leads` into a live DB. Always be explicit, and set it **before** import:

```bash
createdb bz_test
DATABASE_URL=postgresql://$(whoami)@localhost:5432/bz_test venv/bin/python test_site_smoke.py
```

`test_permit_sources.py` hits live permit feeds only when `PERMIT_LIVE=1`.

### Other environment gotchas

- No test calls `load_dotenv()`; `.env` is not read by the suite. `PDL_API_KEY` in `.env` is
  real and changes module-level behaviour in `pdl_contact_service`.
- Tests monkeypatch network entry points to raisers (e.g. `property_service._api_call`).
  RentCast's free tier is 50 calls/**month** — never let a test reach it.
- Outbound TLS from this machine needs `SSL_CERT_FILE=/etc/ssl/cert.pem`.
- Docker daemon isn't running here — Dockerfile edits are verified by building on the
  Droplet (`doctl compute ssh bizstack-prod` then `cd /opt/bizstack && docker compose build`).
- `git commit` occasionally fails with `fatal: /: '/' is outside repository`; retry.
- `opencode.jsonc` (tracked) only registers the Catalyst MCP. `opencode.json` is gitignored.

---

## Architecture

`construction_main.py` is ~12,000 lines with **273 inline route handlers** and no
`APIRouter` — one module scope, divided by `# --- Section ---` banner comments. Landmarks:
`lifecycle()` `:360` (startup DDL + every background thread), `app` `:868`, `get_db()` `:1009`,
`build_tool_handlers` `:1505`, `build_copilot_handlers` `:2318`, `_bid_document_tools` `:2365`,
books `:3949`, bid invoices/PDFs `:4021-4297`, bid detail/board `:9383-9681`,
crew admin + crew PWA API `:10118-10878`, payroll `:11134+`. Line numbers drift; grep.

Everything else is a flat sibling module. There is no layered architecture.

### Crew app → timesheets → payroll → books (the money flow)

This is how the pieces actually connect. Keep each hand-off intact when editing any one of them.

- **Crew PWA** is `templates/construction/app.html` served at `/app`, talking to
  `/api/crew/app/*` (login by phone+PIN, `clock`, `hours`, `photos`, `messages`, `training`).
  Manifest `static/manifest.webmanifest`, service worker `static/sw.js` with a
  `CACHE = "buildstack-crew-vN"` constant — **bump N on every change to `app.html`,
  `sw.js`, or `tailwind.css`** or installed phones keep the stale shell. Use `self.location`,
  not `location`, inside `sw.js`. iOS never fires `beforeinstallprompt`; Chrome shows its
  own install icon only when manifest + SW + icons all validate.
- **Owner↔crew messaging** is `team_messages.py` (visibility rules), `/messages` (owner UI),
  `/api/crew/app/messages` (crew UI), `/ws/messages`. It works; don't rebuild it.
- **Timesheets** come from `/api/crew/app/clock` and `/api/crew/app/hours` and sit in
  `timesheets`/`clock_events`. Owner approves at `/payroll` →
  `/api/payroll/timesheets/{id}/approve|reject`. Only approved, unassigned sheets enter a
  run. Run creation sets `timesheets.payroll_run_id` (locking them against double payment)
  but leaves them `approved`; they become `paid` only after the PNC settlement confirmation.
- **Payroll** `/api/payroll/run` → `/api/payroll/run/{id}/finalize` → PNC NACHA/ACH CSV →
  `/api/payroll/run/{run_id}/ach-sent`. Stripe payroll and Stripe onboarding are retired.
  ACH creation and settlement require a finalized run with PNC bank details for **every**
  positive line; otherwise no worker may be silently omitted while the whole run is marked
  paid. Settlement **must** call `ledger_sync.sync_payroll_paid(conn, run_id)` so books stay
  in step. Copilot can finalize for PNC but cannot move money.
- **Bids → job → invoices**: `bid_opportunities.py` (bid rows + per-bid `documents` with
  `document_type` ∈ `sow/quote/itemized/contract/plans/other`), `bid_costing.py`
  (`open_job_for_bid`, cost lines, `create_invoice`/`set_invoice_status`), `bid_deposit.py`
  (30% deposit invoice + PDF/email), `build_bid_documents.py` (SOW/itemized/contract PDFs
  with walkthrough photos). Payment schedule is **30% deposit / 40% at midpoint / 30% at
  final walkthrough**: `/api/bids/{id}/invoices/progress` and `/final` issue the 40 and 30.
  Uploaded per-bid docs take priority over generated templates in the contract package.
- **Books** (`/books`, `ledger.py`, `ledger_sync.py`, `zoho_books.py`): every money event
  (`sync_deposit`, `sync_invoice`, `sync_invoice_status`, `sync_cost_line`,
  `sync_bid_outcome`, `sync_payroll_paid`, `sync_payroll_accrual`) posts a journal entry via
  `ledger_sync`. Failures queue in `ledger_sync.pending`; `/books/retry` and
  `/api/bids/{id}/books/repair` replay them. **Any new route that moves money must call the
  matching `ledger_sync.*` function** or the books silently drift.
- **Bid document Copilot tools** `research_bid_contact` / `generate_bid_documents` are wired
  in `_bid_document_tools` and advertised in `con_ai_agent.py`; keep both sides in step (see
  the three-place rule below). Bid contact research uses the same cached, ZIP-resolving
  `contact_lookup` path as lead enrichment so repeat clicks do not rebill or guess without a ZIP.

### Database

`psycopg` v3, raw SQL, no ORM, **no connection pool**. `get_db()` opens a connection per
request; ~45 modules open their own ad hoc. `DATABASE_URL` everywhere; `CON_DATABASE_URL`
overrides it in `google_oauth.py` only. Postgres `max_connections` is 500 on purpose.

**Schema is created at startup, not migrated**: `lifecycle()` runs a big idempotent
`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block, then calls
each module's `ensure_schema(conn)` (`bid_opportunities`, `takeoff`, `supplier_prices`,
`ledger_sync`, …). That DDL block is **one transaction** — an `ALTER` placed before its
`CREATE` aborts and rolls back the whole block. `inbound_email._safe_ddl` is the pinned
pattern (`test_schema_safety.py`); use it for new DDL that may race or hit a missing table.

### Templates

- Jinja2's `first` filter exists, but on an empty sequence returns `Undefined` which
  renders blank — prefer `xs[0] if xs else none` when you want a visible fallback. Nested
  `{% '{:,.1f}'.format(x) %}` is invalid; use `{{ }}`. Both have shipped 500s this month.
- **CSS is a static Tailwind v3 build, committed** (`static/css/tailwind.css`, from
  `tailwind.config.js` scanning `templates/**`, `*.py`, `static/**/*.js`). The image has no
  node. After using a class no template used before, run `npm run css` and commit the result
  or it renders unstyled in production. Public pages load the static build via
  `<link rel="stylesheet" href="/static/css/tailwind.css">` in `base.html`; the Tailwind
  Play CDN that previously loaded on every public page has been removed.

### AI agents — three places must agree

Schemas live in `con_ai_agent.py` (`_safe_tools` guest subset, `_full_tools` copilot);
handlers in `construction_main.py` (`build_tool_handlers`, `build_copilot_handlers`) and
`copilot_ops.py`; and `construction_knowledge.md` §15 lists the toolkit in prose that is
concatenated into the system prompt (`con_ai_agent.py:127-128`). **Nothing enforces
agreement.** A handler with no schema is unreachable; a schema with no handler is a button
that can only error; the model *reports a missing capability* rather than failing loudly.

```python
advertised = {t["function"]["name"] for t in BusinessAIAgent(subset="copilot")._tools()}
handlers   = set(build_copilot_handlers(db, owner_email))
advertised - handlers   # can only fail
handlers - advertised   # silently does nothing
```

`test_lead_pipeline_tools.py` pins this for the permit pipeline. When removing a tool, remove
its schema too.

Env: `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-4o-mini`), `COPILOT_TOOL_ATTEMPTS` (2),
`COPILOT_TOOL_DEADLINE_SECONDS` (90). Search uses the **Responses API**;
`gpt-4o-mini-search-preview` and `web_search_preview` were shut down 2026-07-23 — do not
reintroduce them. `ToolBudgetExhausted` → HTTP 502, `ModelUnavailable` → HTTP 503, on purpose.

**OpenCode Zen:** `COPILOT_BASE_URL=https://opencode.ai/zen/v1` repoints only the Copilot
(deliberately not `OPENAI_BASE_URL` — voice and search stay on OpenAI). Zen free models need
an **empty** bearer; a non-empty key gets 401. Fallback list `COPILOT_FREE_FALLBACK_MODELS`
engages only on Zen, never on auth errors, and rots — re-check `/zen/v1/models`.

### Background work

Hand-rolled `threading.Thread(daemon=True)` loops started from `lifecycle()`: permit importer,
lead-source scheduler, Gmail inbound poll, acquisition radar, bid-inquiry firing,
`loan_outreach`, `lead_enrich_loop`, and `bid_autopilot` (hourly: fills contact
name/email/phone on owner-entered bids via skip trace, builds SOW/itemized/contract once a
bid has cost lines; `source='manual'` only, 7-day retry log in `bid_autopilot_log`,
`BID_AUTOPILOT_MAX` traces per pass). Kill switches `DISABLE_PERMIT_IMPORT`,
`DISABLE_LEAD_SCAN`, `DISABLE_LOAN_OUTREACH`, `DISABLE_LEAD_ENRICH`, `DISABLE_BID_AUTOPILOT`.
Env vars may be set as the string `"false"` (which is truthy) — parse explicitly, never bare
`os.getenv(...)` truthiness. Use `str(os.getenv("DISABLE_X", "")).lower() in ("1", "true", "yes")`.

### Outbound messaging

`auto_reply.py` is the guardrail layer (contact-once-then-5-days, cooldowns, daily caps,
federal-domain block, review mode; fails closed). Read it before adding any send path.
Delivery is `documents_service.send_email` (`EMAIL_TRANSPORT`: `gmail` / `resend` / `smtp`).
`send_sms_message` is wired to the Copilot with **no** rate limit or confirmation.

---

## Real customer value — do not lose or commit this

Much of this repo is real money and real people's data and is **not reproducible**.

- **Already tracked (treat the repo as public):** `pnc_statement*.pdf`,
  `send_today/LOAN_PACKAGE.pdf`, `docs/SBA_7a_LOAN_PITCH.pdf`, `docs/INVOICE_*`/`RECEIPT_*`
  HTML naming real customers, and `static/listing/projects/` (63 real customer job photos —
  `test_site_smoke.py` depends on them). Do not add further customer media.
- **Untracked, NOT gitignored — `git add -A` would commit it:**
  `Hampton_Roads_Regional_Parcels.csv` (179 MB parcel data) and `zmail-cli.jar`.
  Always `git add` specific paths.
- **Gitignored — never commit, never delete:** `outbox/` (letter PDFs and batch
  folders; nothing in it was ever delivered — see MEMORY), `docs/stripe/`,
  `.env`, `SESSION_STATE.md`, `SESSION_NOTES.md`, `.agents/`, `.claude/`, `opencode.json`,
  `eva_intel.json`.
- **Never delete anything in this repo without asking.**

---

## Conventions

- Two phone numbers; widening either is a real error: `757-908-7121` on every homeowner- and
  customer-facing surface; `252-665-5891` is Shaun's personal mobile, **lending material only**.
- Lead sourcing is **business contacts only**, enforced in
  `copilot_ops._BLOCKED_CONTACT_DOMAINS`. Skip Trace is the one owner-approved exception;
  every lookup is written to `skiptrace_audit`. `skip_sherpa_service` exposes `trace()`.
- Skip Trace waterfall providers must return `NOT_FOUND` for both 400 and 404 (RentCast uses
  both for "not in my database").
- `photo_store.save(data, filename, key_prefix=...)` — pass a bare filename, not a full key.
- Prefer a focused new module with its own `ensure_schema()` over growing
  `construction_main.py`.
- Keep syntax 3.10-compatible even though both venv and the image are 3.12.

## Deploy

**DigitalOcean Droplet `bizstack-prod`** at `159.203.175.39`. Docker Compose with Caddy
reverse proxy. No auto-deploy.

- **Deploy steps:**
  1. `rsync -avz --exclude='venv*' --exclude='chrome-devtools-mcp' --exclude='__pycache__' --exclude='.git' --exclude='uploads' --exclude='outbox' --exclude='*.pyc' --exclude='.env' --delete -e "ssh" . root@159.203.175.39:/opt/bizstack/construction/`
  2. `doctl compute ssh bizstack-prod --ssh-command "cd /opt/bizstack && docker compose build construction && docker compose up -d construction"`
  3. Verify: `docker logs bizstack-construction-1 --tail 20` — look for `🎯 bid_opportunities ready.`
- **Caddy** routes `construction.bizstackperks.com` → `construction:8000` and
  `bizstackperks.com` / `www.bizstackperks.com` → `hosts:8000`.
- **Database** is Postgres 18 with pgvector in container `bizstack-db-construction-1`,
  database name `construction`. The hosts app has its own container and database.
- Schema is created at boot by `lifecycle()`; there is no migration command.
- **Env vars** live in `/opt/bizstack/construction.env` on the Droplet. To add or change
  one: `doctl compute ssh bizstack-prod`, edit the file, then `docker compose restart construction`.
- **Backups:** `/opt/bizstack/backup.sh` dumps both databases daily; backups go to
  `/opt/bizstack/backups/`.
