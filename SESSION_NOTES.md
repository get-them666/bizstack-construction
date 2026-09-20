# BizStack x SBA Microloan — Session Notes

_Saved Sept 16, 2026. Resume file — read this first next session._
_Updated Sept 19, 2026 — Loan outreach scheduler LIVE end-to-end._

## 🚀 Loan outreach automation — LIVE (Sept 19, 2026)

**Day 0 emails were already sent Sept 18** (SBDC, LISC, VCC, VSBFA with pitch PDF).
**Day 1 follow-ups SENT this morning to all three lenders.** The scheduler now runs
automatically on Railway and fires the cadence with no manual steps.

**What was broken and fixed:**
- **Scheduler never started in production.** `DISABLE_LOAN_OUTREACH` was set to the
  string `"false"`, which is truthy in Python — so `main.py` always skipped
  `start_outreach_tasks()`. Guard now only disables on `"1"/"true"/"yes"`.
- **Day 1 / Day 7 "text" touches were silently dropped** — every lender was configured
  email-only, and the cadence loop `continue`d past text-kind touches. Now text touches
  fall back to email (and would SMS if a lender phone + SignalWire sender were set).
- **SMTP is firewall-blocked inside Railway.** Railway's egress blocks ALL outbound
  SMTP ports (verified: 25/465/587/2525 timeout for privateemail, SendGrid, AWS SES,
  Brevo, Mailgun, Mailjet, Postmark). HTTPS (443) and IMAPS (993) are open. So sending
  moved to the **Resend HTTPS API** (`RESEND_API_KEY` set on the service; domain
  `bizstackperks.com` verified in Resend). IMAP inbox polling for lender replies works
  over 993.
- **Resend proxy detail:** requests MUST include a `User-Agent` header or Resend's
  Cloudflare returns `403 error code: 1010`.

**Cadence schedule (from campaign start `loan_campaign_start = 2026-09-18`):**
- Day 1 (text nudge, sent as email) — **SENT Sept 19** to LISC, VCC, VSBFA
- Day 3 (email 2) — due **Sept 21**
- Day 7 (text check-in) — due **Sept 25**
- Day 14 (email 3, close loop) — due **Oct 2**, then stop; revisit in 60 days

Status is tracked in the construction DB: `outreach_touches` (cadence log) and
`outreach_replies` (AI auto-replies to lender emails). Failed touches are retried on the
next hourly pass.

**Also added:** `opencode.json` with the Resend MCP server (`https://mcp.resend.com/mcp`)
so future sessions can read/send mail via opencode directly.

## ⚠️ Construction Postgres — FIXED (Sept 18, 2026)

**Root cause:** Railway auto-deploy (Sept 17 06:30 EDT) rebuilt the "Postgres" service from the
repo's `/Dockerfile` instead of the `ghcr.io/railwayapp-templates/postgres-ssl:18` image — the
service was running the **FastAPI app (uvicorn on port 5432)**, not PostgreSQL. All DB routes 500'd.

**Fix:** `railway redeploy -s Postgres --from-source -y` (deployment `06ec409d`, Postgres 18.6
started, volume `postgres-volume` data intact: "PostgreSQL Database directory appears to contain
a database; Skipping initialization"). Then `railway up -s friendly-appreciation` to deploy the
app. All routes now return 200 (public + auth pages).

**Data verification (direct DB via tcp-proxy):**
- 120 Old Ironsides Rd = $24,700 full build, created **Mar 4** ✓
- 5509 Sunnywood Dr = $7,200 flood reno, created **May 6** ✓
- (dates were swapped in DB; fixed directly to match SESSION_NOTES correction)
- `/backlog` pipeline `$31,900`, collected `$0`; 2 completed jobs.
- Paid-in / deposit_status not yet recorded in payments table ($22,500 PNC bank-trailed).
- Money display bug fixed: `_money` now converts cents→dollars (was showing `$2,470,000`).

**Last-known-good Postgres deployment before outage:** `ddbfa988` (Sept 16). Broken: `69da57ed`
(Sept 17). If Postgres ever looks "Online" but DB routes 500 again, check `railway logs` for
uvicorn instead of PostgreSQL startup.

## Next session priorities (Sept 19+)
1. **Follow-up cadence is now AUTOMATED and live on Railway** — no manual sending.
   Day 1 sent Sept 19; Day 3 fires Sept 21, Day 7 Sept 25, Day 14 Oct 2. Watch
   `outreach_touches`/`outreach_replies`; the AI reply poller answers lender emails over
   IMAP 993.
2. **Packages pending lender replies:** invoices + signed receipts (cash jobs), insurance
   scope paperwork (Sunnywood), bank statement for the $10K equity. Have these ready to
   send same-day when Resend replies/requests docs.
3. **Payments recorded in DB** — 3 rows: $22,500 (Old Ironsides, 03/19) + $2,200 (cash,
   03/12) + $7,200 (Sunnywood, 05/17) = **$31,900 collected**. `/backlog` dashboard reflects it.
4. **Drive local lead-gen** now that site + forms are live end-to-end.
5. Optional: deep clean ($275) + Shipyard contract income lines in the pitch._

## Objective
Land a **$50,000 SBA Microloan** for Shaun O'Leary's startup, **BizStack**
(bizstackperks.com) — one company, two revenue lines:

- **Buildstack Construction** — licensed GC: renovations, kitchens/baths, roofing/siding,
  decks, STR make-readies. Williamsburg–Hampton Roads, VA + Currituck County / Elizabeth
  City, NC.
- **Broom Service** — STR turnover cleaning + co-hosting (15% of stay revenue) at
  Sunnywood and Old Ironsides.

Loan facts: startup, FICO low 600s, 25 years in the trade, target payment ~$930/mo
(6-yr microloan). Contact: **hello@bizstackperks.com** · **252-665-5891**.

## Status: verified 2026 revenue = $32,516.50

> **CORRECTION Sept 17:** the builds were logged backwards. Correct mapping —
> **120 Old Ironsides Rd = $24,700** full build (done **March** 4–12) and
> **5509 Sunnywood Dr = $7,200** insurance flood reno (done **May** 6–17).
> Pitch, invoices, receipts, and photo log are all updated. When the Construction
> Postgres comes back online, the backlog/project/payment records need the same swap.

| Line | Dates | Amount |
|---|---|---|
| Construction — 5509 Sunnywood Dr (insurance flood reno) | May 6–17 | $7,200 |
| Construction — 120 Old Ironsides Rd (garage → 1BR apt full build) | Mar 4–12 | $24,700 |
| Broom Service — 3 turnovers @ $175 | Sep 3, 6, 11 | $525 |
| Broom Service — co-host commission 15% ($430 + $180 stays) | Sep 6, 11 | $91.50 |

Live DB state (Sept 16, 2026):
- Construction: 2 leads, 2 quotes, 2 completed projects, **$0 open pipeline**.
- Broom Service: 3 real hosts (Sunnywood/Rebecca Mastic, Old Ironsides/Carl Simmons,
  Sea Gate/Dorothy O'Leary), 3 bookings, ledger income $616.50, expenses $50, net $566.50.

All of the above was back-logged into the live databases on its real dates via the new
`/backlog` pages (see below).

## CRITICAL documentation issue — partial fix (Sept 17)
- **Bank trail found for the March job:** the PNC statement (period 02/26–03/27, page 3 of 5,
  acct ...5216) shows a **$22,500 deposit on 03/19/2026** — one week after Old Ironsides
  finished (Mar 4–12). Per Shaun this is the Old Ironsides payment, so **$22,500 of the
  $31,900 is now bank-trailed**. PNC labeled it "ACH Branch Cash Deposit" (bank-app
  formatting quirk — "ACH" is not the real type; the deposit itself is on the record).
- **Still cash, no trail:** the **$2,200 remainder on Old Ironsides** (confirm what it was —
  materials/held cash) and **all of Sunnywood's $7,200**. Lender needs per job: invoice marked
  "Paid — cash", signed receipt, before/after photos, and (Sunnywood) the homeowner's insurance
  claim/scope paperwork. Income must also appear on tax returns.
- **Equity note:** the $22,500 deposit verifies *revenue*; the **$10K owner-equity** still needs
  its own story (your own funds contributed to the business) — see `docs/CHECKLIST.md`.
- **Going forward:** open a dedicated business bank account; deposit all payments there.

## Code changes made this session
- **Both apps: new `/backlog` page** (admin) to enter past-dated records.
  - Construction: `GET /backlog`, `POST /api/backlog`, `POST /api/backlog/{id}/delete` +
    `templates/backlog.html` + nav link. Writes `leads` (source='backlog') with real
    `created_at`, plus `projects` (start_date) and `payments`.
  - Broom Service: `GET /backlog`, `POST /api/backlog/job`, `POST /api/backlog/expense`,
    `POST /api/backlog/{id}/delete` + `templates/backlog.html` + nav link. Writes
    `customers`, `bookings`, and `ledger_entries` (income/expense) with real dates.
- **Bug fix:** ledger revenue type was inconsistent — the Stripe payment flow writes
  `tx_type='income'` but the AI tool and `pitch_numbers.py` used `'revenue'`, so real
  income was invisible in summaries. Now both `('income','revenue')` are counted and
  `add_ledger_entry` normalizes to `'income'`.
- **Data cleanup:** deleted a test lead ("Railway Tester", fake $45K pipeline) and a
  placeholder host ("Linkedin · Matt Redmon", `@lead.local`).

**These code changes are LOCAL only** — both apps need a redeploy on Railway for the new
`/backlog` pages to appear in production. (Back-log data was inserted directly, so it's
already live regardless.)

## How to re-pull the numbers
Working CLI (NOT `/usr/local/bin/railway` — broken symlink): `~/.railway/bin/railway`.
Project IDs: Broom Service `653fd47f-c670-436d-a108-7bca5dc4e38f`,
Buildstack Construction `913e36b5-fe1f-4d73-80c5-aa0dd74f47be`.

1. `railway link -p <PROJECT_ID> -s Postgres -e production`
2. `railway tcp-proxy create --port 5432 -s Postgres --json` → note host:port + id
3. `export DATABASE_URL="postgresql://<PGUSER>:<PGPASSWORD>@<host>:<port>/<PGDATABASE>"`
4. `python pitch_numbers.py`
5. **`railway tcp-proxy delete <proxy-id> -s Postgres --yes`** — always close it.

## Open items
- [x] **Deploy both apps** so `/backlog` is live — deployed Sept 17, 2026 via `railway up`.
- [x] **Cash-job documentation templates created** (`docs/INVOICE_*.html`, `docs/RECEIPT_*.html`,
      `docs/PHOTO_LOG.html`, `docs/CHECKLIST.md`) — still need homeowner signatures.
- [x] **$10,000 equity** — Shaun confirmed he can get a current bank statement (pull & file it).
- [x] **Outreach emails sent — SBDC + LISC + VCC + VSBFA on Sept 18**, pitch PDF attached.
- [x] **Follow-up cadence automated on Railway** — Day 1 sent Sept 19; Day 3/7/14 auto-fire
      (scheduler in `loan_outreach.py`, sends via Resend HTTPS API, replies polled over IMAP).
- [x] **⚠️ Construction Postgres outage (Sept 17)** — **FIXED Sept 18** (see top of file). Cause:
      auto-deploy rebuilt Postgres service from the app's Dockerfile (uvicorn on 5432, not
      postgres). Fix: `railway redeploy -s Postgres --from-source -y` + `railway up
      -s friendly-appreciation`. All DB routes verified 200; data intact; dates re-swapped.
- [ ] **Get business / respond to lenders** — docs ready same-day; then local lead-gen
      (Nextdoor, FB Marketplace, Google Business Profile).
- [ ] Optional: add the **deep clean ($275)** — property/date still unconfirmed.
- [ ] Optional: Shipyard **contract income** details (employer, dates, gross) — ended.

## Files
- `SBA_7a_LOAN_PITCH.md` — pitch + verified reality-check + documentation section.
- `LOAN_OUTREACH_SEQUENCE.md` — email/text cadence, SBDC-first email, reply scripts,
  verified lender contacts.
- `pitch_numbers.py` — read-only DB extractor (auto-detects DB type).
- `main.py` (this folder) — construction app; `../main.py` — Broom Service app.
