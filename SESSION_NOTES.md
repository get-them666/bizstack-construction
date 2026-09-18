# BizStack x SBA Microloan — Session Notes

_Saved Sept 16, 2026. Resume file — read this first next session._
_Updated Sept 18, 2026 — Construction Postgres FIXED + app fully operational._

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

## Next session priorities (Sept 18+)
1. **Send outreach emails** — drafted & ready in `docs/OUTREACH_EMAILS.md` (SBDC → LISC → VCC → VSBFA).
   Not sent yet; send from hello@bizstackperks.com.
2. **Record the $22,500 bank deposit + $2,200 cash + Sunnywood $7,200** as payments/deposit_status
   in DB so lender dashboards & `collected` totals are complete.
3. **Get business:** site is fully operational — drive traffic (nextdoor, FB marketplace, Google
   Business Profile, VA/NC permit boards) and route leads into the `/leads` pipeline.
4. Optional: deep clean ($275) + Shipyard contract income lines in the pitch._

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
- [x] **Outreach emails drafted & ready to send** (`docs/OUTREACH_EMAILS.md`, order
      SBDC → LISC → VCC → VSBFA). **NOT SENT YET** — send from hello@bizstackperks.com.
- [x] **⚠️ Construction Postgres outage (Sept 17)** — **FIXED Sept 18** (see top of file). Cause:
      auto-deploy rebuilt Postgres service from the app's Dockerfile (uvicorn on 5432, not
      postgres). Fix: `railway redeploy -s Postgres --from-source -y` + `railway up
      -s friendly-appreciation`. All DB routes verified 200; data intact; dates re-swapped.
- [ ] **Record payments in DB**: $22,500 (PNC 03/19), $2,200 Old Ironsides remainder, $7,200
      Sunnywood cash — so `/backlog` collected + lender dashboards are complete.
- [ ] **Get business / send outreach:** docs ready — **SBDC first**, then LISC → VCC → VSBFA;
      then local lead-gen (Nextdoor, FB Marketplace, Google Business Profile).
- [ ] Optional: add the **deep clean ($275)** — property/date still unconfirmed.
- [ ] Optional: Shipyard **contract income** details (employer, dates, gross) — ended.

## Files
- `SBA_7a_LOAN_PITCH.md` — pitch + verified reality-check + documentation section.
- `LOAN_OUTREACH_SEQUENCE.md` — email/text cadence, SBDC-first email, reply scripts,
  verified lender contacts.
- `pitch_numbers.py` — read-only DB extractor (auto-detects DB type).
- `main.py` (this folder) — construction app; `../main.py` — Broom Service app.
