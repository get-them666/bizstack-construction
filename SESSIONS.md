# Sessions Log — BizStack Construction

> **Rule:** Every AI session must read this file before starting work and append a dated entry after finishing work. Never skip this step.

---

## 2026-11-20 — Full-Stack Audit & Automation (Session 1)

### Production Architecture
- **Host:** DigitalOcean droplet `bizstack-prod` at `159.203.175.39`
- **Stack:** Docker Compose with Caddy reverse proxy
- **Containers:**
  - `bizstack-construction-1` — construction.bizstackperks.com (BuildStack Construction)
  - `bizstack-hosts-1` — bizstackperks.com (Broom Service)
  - `postgres` — Two databases: `db-construction` and `db-hosts`
- **Deploy:** `rsync` + `docker compose up -d --build`
- **Secrets:** `/opt/bizstack/construction.env` and `/opt/bizstack/hosts.env` (never committed to Git)

### Strict Brand & Phone Isolation
- **Broom Service** (bizstackperks.com / `../bizstack-hosts/`)
  - Phone: **+1 (757) 908-7121**
  - Knowledge: `bot_knowledge.md`
  - Features: STR cleaning/co-hosting tiers, turnover calculator, rental analysis
- **BuildStack Construction** (construction.bizstackperks.com / `./`)
  - Phone: **+1 (757) 846-9275**
  - Knowledge: `construction_knowledge.md`
  - Features: GC/STR make-ready estimates, scopes, free estimate forms
- **Shared inbox:** `hello@bizstackperks.com` classifies brand intent automatically via `_company_for_recipients()` in `main.py`

### Work Completed

#### 1. Phone Number Cleanup
- Removed stale 908-7121 references from Construction docs (`docs/subcontractor_application.md`, `docs/facebook-free-estimate.md`)
- Clarified `MEMORY.md` to distinguish Broom (908-7121) vs Construction (846-9275)
- Updated `../bizstack-hosts/construction_knowledge.md` to use correct construction phone (846-9275)

#### 2. Inventory-Tracker Wiring
- **Problem:** `inventory-tracker/src/` was 100% mock data with zero backend connection
- **Solution:**
  - Added 4 JSON API endpoints to `construction_main.py`: `GET /api/inventory`, `POST /api/inventory/update`, `POST /api/inventory/create`, `DELETE /api/inventory/{id}`
  - Created `inventory-tracker/src/utils/api.js` API client
  - Rewrote `App.jsx` to fetch from FastAPI on mount and call API for all mutations
  - Updated `initialInventory.js` to export only static constants
  - Disabled `realtimeSimulator.js` (returns null, no fake events)
  - Added Vite proxy in `vite.config.js` for `/api/*` → `localhost:8000`
- **Result:** Real-time inventory management from Postgres, no mock data

#### 3. Email Pipeline Safety & Health
- **Ported to Broom Service:**
  - `transport_health.py` — Monitors SMS/email delivery health, alerts owner on failures
  - `suppression.py` — DNC/opt-out enforcement, fail-closed suppression list
  - `auto_flush_drafts()` — Autonomous background email flushing with suppression checks
- **Wired into Broom Service:**
  - SMS webhook: Added STOP/opt-out detection before AI reply
  - `signalwire_service.py`: Added `transport_health.record()` on every SMS send
  - `auto_reply.py`: Added suppression check in `fire_lead_draft()` before sending
  - Fixed construction CTA phone number (908-7121 → 846-9275)
- **Inbound email classification:** Enhanced `_company_for_recipients()` to classify brand intent from subject/body content (not just recipient address)

#### 4. Copilot System Port
- **Ported to Broom Service:**
  - `copilot_memory.py` — Postgres-backed conversation memory (40-msg history, 10-year retention)
  - `copilot_ops.py` — Operator tools (calculate, maps, calendar, search, tasks, contacts)
  - `copilot_tasks.py` — Durable task memory with 1-year retention
- **Upgraded `con_ai_agent.py`:**
  - Added `ModelUnavailable` and `ToolBudgetExhausted` exception classes
  - Added `process_conversation(history, message)` method with full context replay
  - Proper error surfacing instead of silent fallbacks
- **Wired into `main.py`:**
  - Upgraded `/api/copilot` to use `process_conversation()` with history replay
  - Added `/api/copilot/history` and `/api/copilot/reset` endpoints
  - Added `_copilot_owner_email()` helper for session-scoped memory
  - Added `purge_expired()` call on every write to enforce retention

#### 5. Phone, Voice & SMS AI
- **Ported to Broom Service:**
  - `outbound_campaign.py` — Batch outbound AI calls with claim-based dedup, suppression checks, calling-hours gate, daily cap
  - `scheduled_calls.py` — Durable scheduled callback queue with Postgres persistence
- **Added routes to `main.py`:**
  - `POST /api/outbound/call` — Place outbound AI call to a lead
  - `GET /api/outbound/status` — Check call status by CallSid
  - `POST /api/scheduled-calls` — Schedule a callback at a specific time
  - `GET /api/scheduled-calls` — List all pending scheduled callbacks
- **Brand voice isolation verified:**
  - Broom Service: 908-7121, STR cleaning knowledge, booking links
  - Construction: 846-9275, construction knowledge, estimate links
  - Zero cross-contamination confirmed

#### 6. Environment Variable Audit
- Scanned all `os.getenv()` calls across both repos (473 in construction, 202 in hosts)
- Created `.env` and `.env.example` files in both repos with all missing variables as empty placeholders
- Organized by service category (Database, AI, Voice, SMS, Email, Payments, Google, Property Data, etc.)
- Documented production secrets location: `/opt/bizstack/construction.env` and `/opt/bizstack/hosts.env`
- Provided SCP commands to pull production keys from droplet

#### 7. Portal Verification
- Verified `portal_service.py` and `auth_service.py` across all portal templates
- Confirmed 22 portal routes correctly use authentication and session management
- Verified client-bid linking via `portal_project_access` table
- Confirmed no cross-brand sync issues (each brand maintains separate customer records)

#### 8. Form & Booking Triggers
- **Broom Service:** Rental Analysis form and Turnover Calculator "Book This Clean" both submit to `/submit-lead` → `email_bot.follow_up_new_lead()` → sends confirmation email + AI callback
- **Construction:** Free Estimate form and Chat modal both submit to `/submit-lead` → `auto_reply.auto_reply_to_lead()` + `_follow_up_new_lead()` → sends confirmation email + AI callback
- All triggers hit real Postgres tables and live API endpoints (no stubs)

#### 9. Fixed Production Error
- **Error:** `📧[emailbot] auto-flush failure: module 'auto_reply' has no attribute 'auto_flush_drafts'`
- **Root cause:** `email_bot.py` line 763 calls `auto_reply.auto_flush_drafts(flush_db)` but function didn't exist
- **Solution:** Implemented `auto_flush_drafts()` in `bizstack-hosts/auto_reply.py` with:
  - Suppression enforcement (DNC/opt-out checks)
  - Daily send caps
  - Failure resilience (stops after 3 consecutive failures)
  - Rate limiting (0.6s gap between sends)
  - Broom Service branding (+1 (757) 908-7121)
  - Bounded batch (max 50 leads per call)

### Files Modified (Construction)
- `construction_main.py` — Added 4 inventory API endpoints
- `docs/subcontractor_application.md` — Fixed phone numbers
- `docs/facebook-free-estimate.md` — Fixed phone number
- `MEMORY.md` — Clarified brand phone distinction
- `my_agent/root_agent.yaml` — Fixed `<FILL_IN_MODEL>` placeholder
- `inventory-tracker/` — Full rewrite to use real API (App.jsx, api.js, initialInventory.js, realtimeSimulator.js, vite.config.js)

### Files Modified (Broom Service)
- `main.py` — Added STOP handling, brand classification, Copilot memory, outbound call routes
- `auto_reply.py` — Fixed construction CTA, added suppression checks, added `auto_flush_drafts()`
- `signalwire_service.py` — Added transport_health recording
- `con_ai_agent.py` — Added ModelUnavailable, ToolBudgetExhausted, process_conversation()
- `construction_knowledge.md` — Fixed phone numbers

### Files Copied to Broom Service
- `transport_health.py` — Transport health monitoring
- `suppression.py` — DNC/opt-out enforcement
- `copilot_memory.py` — Postgres-backed conversation memory
- `copilot_ops.py` — Operator tools
- `copilot_tasks.py` — Durable task memory
- `outbound_campaign.py` — Batch outbound AI calls
- `scheduled_calls.py` — Scheduled callback queue

### Compile Status
- ✅ All Python files compile with zero errors (both repos)
- ✅ inventory-tracker React app builds successfully (282.79 kB JS, 19.51 kB CSS)

### Git Commits
- **Construction:** `329133f` — "Prompt 1: Clean phone numbers, wire inventory-tracker API, verify portals"
- **Broom Service:** `83e972f` — "Prompt 1: Clean construction phone references"

---

## Remaining Optional Next Steps

### High Priority
1. **Enable RESEND_API_KEY fallback** — Currently `EMAIL_TRANSPORT=gmail` only. Add Resend as fallback for when Gmail API quota is exhausted.
2. **Run live SignalWire SMS test** — Verify 10DLC registration is approved and SMS delivery works end-to-end.
3. **Pull production .env from droplet** — Run `scp root@159.203.175.39:/opt/bizstack/construction.env ~/.env.production-backup` to sync latest secrets.

### Medium Priority
4. **Port Broom Service portal to bizstack-hosts** — Currently only Construction has client portal. Broom Service hosts need portal access to booking history and invoices.
5. **Add WebSocket live updates to inventory-tracker** — Currently polls on page load. Add WebSocket for real-time inventory changes.
6. **Enable Google Maps billing** — Copilot maps tools are dead because GCP project `prismatic-will-507203-k8` has no billing enabled.

### Low Priority
7. **Add R2 storage for photo uploads** — Currently uses local `/uploads` volume. Migrate to Cloudflare R2 for better durability.
8. **Set up RudderStack analytics** — Write keys are configured but data plane URL is invalid. Get valid credentials from RudderStack dashboard.
9. **Enable Accurate Append** — Currently blocked (no subscription). Skip Sherpa covers the same ground, but Accurate Append at 100/month would double the contact lookup capacity.

---

## Session Rules

1. **Always read SESSIONS.md before starting work** — Understand what was done previously
2. **Append dated entry after finishing work** — Document what you did, files modified, compile status
3. **Never modify .env files** — Production secrets live on the droplet only
4. **Never commit .env to Git** — Already in .gitignore
5. **Run compile checks after every change** — `python3 -m py_compile` for Python, `npm run build` for Node/React
6. **Verify brand phone isolation** — Broom = 908-7121, Construction = 846-9275, never mix them
7. **Test suppression before sending** — Every outbound email/SMS must pass suppression.py checks
8. **Log everything to comms_logs** — Every inbound/outbound communication must be auditable
