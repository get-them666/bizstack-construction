# BizStack x SBA Microloan — Session Notes

_Saved Sept 16, 2026. Resume file — read this first next session._

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

| Line | Dates | Amount |
|---|---|---|
| Construction — 5509 Sunnywood Dr (insurance flood reno) | Mar 4–12 | $7,200 |
| Construction — 120 Old Ironsides Rd (garage → 1BR apt full build) | May 6–17 | $24,700 |
| Broom Service — 3 turnovers @ $175 | Sep 3, 6, 11 | $525 |
| Broom Service — co-host commission 15% ($430 + $180 stays) | Sep 6, 11 | $91.50 |

Live DB state (Sept 16, 2026):
- Construction: 2 leads, 2 quotes, 2 completed projects, **$0 open pipeline**.
- Broom Service: 3 real hosts (Sunnywood/Rebecca Mastic, Old Ironsides/Carl Simmons,
  Sea Gate/Dorothy O'Leary), 3 bookings, ledger income $616.50, expenses $50, net $566.50.

All of the above was back-logged into the live databases on its real dates via the new
`/backlog` pages (see below).

## CRITICAL documentation issue
- **Both construction jobs were paid in CASH** and the **homeowner held the insurance**
  (not Shaun). No bank-deposit trail exists for the $31,900. Cash income can't be counted
  by a lender without proof. Needed per job: invoice marked "Paid — cash", signed receipt,
  before/after photos, and (Sunnywood) the homeowner's insurance claim/scope paperwork.
  Income must also appear on tax returns.
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
- [ ] **Deploy both apps** so `/backlog` is live.
- [ ] Collect documentation for the cash construction jobs (invoices, receipts, photos,
      insurance scope).
- [ ] Confirm the **$10,000 equity injection** is real and documentable.
- [ ] Optional: add the **deep clean ($275)** — property/date still unconfirmed.
- [ ] Optional: Shipyard **contract income** details (employer, dates, gross) — ended.
- [ ] Send outreach in order: **Hampton Roads SBDC → LISC → VCC → VSBFA**.

## Files
- `SBA_7a_LOAN_PITCH.md` — pitch + verified reality-check + documentation section.
- `LOAN_OUTREACH_SEQUENCE.md` — email/text cadence, SBDC-first email, reply scripts,
  verified lender contacts.
- `pitch_numbers.py` — read-only DB extractor (auto-detects DB type).
- `main.py` (this folder) — construction app; `../main.py` — Broom Service app.
