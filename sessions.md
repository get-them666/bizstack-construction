# sessions.md — operating notes for both BizStack sites

**Two brands, one platform.** Every change here ships to both.

| | |
|---|---|
| **Buildstack Construction** | `construction.bizstackperks.com` — licensed GC, residential + STR make-readies |
| **Broom Service** | part of `bizstackperks.com` — STR turnover cleaning, co-hosting, power washing |

Shared codebase in this repo, one database, one Railway service, rows separated by
`leads.company` (`'construction'` / `'broom'`). Anything phrased as "both sites" means
both brands move together — do not split them unless asked.

---

## The one-line state of play

**We can see demand but we cannot call it.** 529 building permits and 690 leads are
tracked, but no permit source publishes a homeowner's name, phone or email, so the
pipeline is addressable rather than contactable. Working capital is what converts it.

---

## Live numbers (verify against the boot digest before quoting them)

The `[pipeline-digest]` log line is the source of truth. Last read:

| Metric | Value |
|---|---|
| Leads under management (construction) | **690** — 679 new · 6 contacted · 1 quoted · 2 completed · 2 host |
| Permits monitored | **529** (Virginia Beach + Norfolk) |
| Jobs at quoted stage | **1** |
| Completed projects | **2** |
| Verified 2026 revenue | **$32,516.50** |
| Broom leads | 1 new · 1 host |

Never quote a number that isn't in the digest. Both loan material and lender conversations
depend on these being defensible.

---

## Email — Gmail only

**Status: working.** `[outreach] mail preflight: OK — Gmail token valid for
construction`. This is the first time outbound mail has worked in this app's
history. Confirm it any time with that log line.

**Everything outbound goes through the Gmail API.** The other transports are retired
because they were limiting us, not because they were broken:

| Transport | Status | Why |
|---|---|---|
| **Gmail API** | **active, only path** | HTTPS, rides the OAuth token already used for inbound |
| SES | retired | Sandbox identity, cannot mail external recipients |
| Resend | retired | Daily quota shared with the lead bot, gets exhausted |
| Zoho | retired | Times out on 25/587/465 from Railway egress |

Set `EMAIL_TRANSPORT=legacy` to re-enable the old ladder. Don't, unless debugging.

Key vars: `GMAIL_SEND_SERVICES` (default `construction,broom`), `GMAIL_OWNER`,
`CON_GOOGLE_CLIENT_ID`, `CON_GOOGLE_CLIENT_SECRET`.

Inbound polls every 900s (`GMAIL_INBOUND_INTERVAL_SECONDS`). Confirm with
`[gmail-inbound]` lines in the log.

---

## Lead sources

| Source | State | Notes |
|---|---|---|
| **Virginia Beach permits** | active | ArcGIS FeatureServer, no key |
| **Norfolk permits** | active | Socrata `fahm-yuh4`, no key |
| Shovels | configured, unused by default | Open data wins when present |
| Reddit / LinkedIn / SAM.gov / bid boards | active | `SAM.gov is OFF` — see below |
| Website + inbound Gmail | active | The only source that yields real contact details |

### Permit list quality (new — Sept 30)

Both city feeds publish a residential/commercial signal (Norfolk `use_class`,
Virginia Beach `ConstructionType`). `wants_lead()` read it at ingest and the
`INSERT` then **dropped it**, so it could never be displayed or re-filtered. Now
persisted and shown.

- `use_class` — our normalized verdict: `residential` / `commercial` / `unknown`
- `property_type` — the raw feed value, kept separate (VB packs values like
  "Roof and or Siding" and "Asbestos" into that column, so overloading it merged
  two different things and produced mixed-case garbage like `Residential` vs
  `residential`)

The permits lane shows a "List quality" bar: residential / commercial /
unreviewed / no-address counts. Commercial work usually already has a contractor,
so those are knock-wasted.

Measured on a full re-ingest (1,592 permits): **~1,025 residential · 363
commercial · 204 unreviewed.** Roughly **23% of the list is commercial** — worth
filtering before spending gas.

`permit_service.classify_use()` is a keyword pass, not a model. Deliberately
returns `unknown` rather than guessing, so anything it can't place is visibly
unreviewed rather than silently filed as a lead. 12/12 on the test set,
including the "detached garage is residential / repair garage is commercial"
case.

**SAM.gov is switched off** for both companies (`LEAD_SOURCES_SAM_GOV=0`). The 124
existing `sam-gov` leads are hidden from every owner view, not deleted. Flip the var to
`1` to restore. Same switch hides them in the dashboard, lead list, `/pipeline`,
copilot search, and boot digest.

**Chesapeake** falls back to clearly-marked demo rows on purpose — its open ArcGIS layer
is land-use actions with a 2022 newest entry and no applicant fields. There is a test
pinning this. Don't wire it in.

---

## The loan — SBA microloan, $50K

This is the live ask. State of play:

- **The pitch is strong.** `SBA_7a_LOAN_PITCH.md` — the differentiator is 25 years of
  trade experience, two documented insured renovations, and real dated revenue.
- **The pipeline gap is closed.** The pitch previously said "no open pipeline." It now
  carries a Sept 30 update: 690 leads / 529 permits monitored, with the honest
  explanation that the money funds *contact and conversion*, not more lead generation.
- **Sending now works via Gmail.** `loan_outreach.py` sends Day 0/1/3/7/14 by email to
  LISC, VCC and VSBFA, and auto-drafts replies when they answer.

**Two bugs hid this for weeks, both worth remembering:**

1. `missed` touchpoints are **terminal**. While mail was undeliverable, every
   touchpoint got marked `missed`, so the cadence gave up permanently — while the log
   still read only `scheduler started`, which looks identical to a healthy campaign.
   Dead mail now leaves touchpoints open so the sequence recovers. Verified both ways
   against a real database: dead mail preserves all 12 touchpoints; working mail still
   honours the stale-copy window.

2. The `mail_ready()` preflight itself was silent — it only ran inside the
   overdue-touchpoint branch, so on day 0 and any day with nothing due it never
   executed. It now logs on every pass, before any early return. A dead campaign must
   never again be indistinguishable from a working one.

Check that log line before assuming the sequence is live.

Lender contacts are current and verified: `smallbusiness@lisc.org`, `jbarnes@vccva.org`
(`@vccva.org` is still live for Virginia Community Capital), `VSBFA@sbsd.virginia.gov`.
`docs/records-request.md` holds ready VFOIA letters for VB, Norfolk and Chesapeake.

---

## Still to do — picked up next session

1. **Geocoding pass.** Needs `GOOGLE_MAPS_SERVER_KEY`. Two steps:
   - Console → Credentials → Create → API key
   - **No application restriction** (the existing key is browser-referrer only,
     which Google refuses server-side: `REQUEST_DENIED / API keys with referer
     restrictions cannot be used with this API`)
   - API restrictions: Geocoding + Distance Matrix only
   - Then: `railway variables --set GOOGLE_MAPS_SERVER_KEY=<key>`
   Fills lat/lng and the missing Norfolk ZIPs (the Socrata feed publishes no ZIP
   at all), flags addresses that don't resolve. `google_maps.py` already reads
   `GOOGLE_MAPS_SERVER_KEY` and `batch_travel_minutes()` chunks at 25/request.
   Degrades to today's behaviour if the key is absent.

2. **Drive time.** Low priority — you said 45 minutes is acceptable, so it
   filters nothing. Useful only for route ordering within a day. Don't build it
   before #1.

3. **Permit scope research.** `permit_scope.py` written, schema landed
   (`scope_summary`, `scope_category`, `scope_confidence`, `scope_researched_at`),
   nothing calls it yet, and the board doesn't render it. This is what turns an
   address into "21x15 sunroom, roof replacement".

4. **Google Maps places/calendar/drive** — all idle, all scoped in
   `google_maps.py` / `google_calendar.py`.

## The pipeline board

`/pipeline` — primary nav item, three streams as one kanban.

- **Leads** — new → contacted → quoted → deposit → in progress → completed → lost
- **Permits** — not worked → mailed → knocked → reached them → won → lost → archived
- **Back-log** — jobs you've decided to do, on the lead status vocabulary

Drag a card to move it. Columns cap at 60 cards and say "+N more" with a search link,
because a silently truncated column reads as data loss.

Permit cards carry a scope summary and category once researched
(`job_leads.scope_summary` / `scope_category`). **Not yet wired to the board UI** — the
schema and `permit_scope.py` exist, the research pass is not triggered.

---

## Google APIs in use vs. idle

| API | State |
|---|---|
| Gmail | **active** — inbound polling + all outbound |
| OAuth | **active** — token storage, refresh |
| Maps | **idle** — `google_maps.py` has geocode, places, distance matrix, travel time. Nothing imports it. This is the obvious next win: travel-time-from-office on every permit is a real prioritisation signal. |
| Calendar | **idle** — `google_calendar.py` unused |

---

## Tests

Six files, all expected green. Run against a scratch DB:

```bash
createdb bz_test
DATABASE_URL=postgresql://$(whoami)@localhost:5432/bz_test python test_site_smoke.py
PERMIT_LIVE=1 python test_permit_sources.py   # PERMIT_LIVE=1 hits the real city feeds
```

| File | Guards |
|---|---|
| `test_site_smoke.py` | every template resolves + every page renders <500, pipeline moves, disabled sources hide |
| `test_permit_sources.py` | open-data normalisation, lead filter, live feed shape |
| `test_documents_service.py` | Gmail-only transport + the retained legacy ladder |
| `test_loan_outreach.py` | cadence, allowlist, reply path, dead-mail guard |
| `test_schema_safety.py` | startup DDL under lock contention |
| `test_vapi_materials.py` | Vapi materials tools |

---

## Deploying

Railway does **not** auto-deploy on push here. Both steps are required:

```bash
git push origin main
railway up --service BizStack-Construction
```

Confirm the new deployment went SUCCESS, then check the startup lines:

```bash
railway deployment list --service BizStack-Construction
railway logs --service BizStack-Construction --deployment <id>
```

Look for `Application startup complete`, `[outreach] mail preflight: OK`, and no
`TemplateNotFound`.

Note: unauthenticated `curl` returns **303** to `/login`, never 200. Polling for 200
cannot detect a broken page — render or authenticate instead.

---

## Printing letters for address-only leads (new — Sept 30)

The permit feeds give us an address and nothing else — no homeowner name, phone or
email — so those leads have **no channel the email/text bot can use**. They are at
zero touches, never contacted, because there was nothing to send to.

`mail_letters.py` (construction repo) renders one Letter-format PDF per such lead so
the owner can print and mail them by hand:

```bash
python mail_letters.py --list          # who's eligible, writes nothing
python mail_letters.py --out outbox    # PDFs + manifest.csv
```

Eligible = has a postal address, no usable email (blank or `@lead.local`), no usable
phone. Outputs one PDF per lead plus `manifest.csv` (id, name, address, project_type,
source, status, filename) for the print run.

**It is offline by design.** It reads `leads`, writes PDFs to disk, and never touches
`comms_logs` — so nothing it does can move a touch count, and the owner keeps the
decision on every letter. Marking letters as mailed is a separate, deliberate step
(see below); do not add a send inside this script.

### The letterhead

Brand values come from env, not code. Current defaults:

| Var | Value |
|---|---|
| `LETTER_BRAND_NAME` | BizStack |
| `LETTER_BRAND_LEGAL` | Buildstack Construction |
| `LETTER_BRAND_PHONE` | 252-665-5891 |
| `LETTER_BRAND_EMAIL` | hello@bizstackperks.com |
| `LETTER_BRAND_WEB` | bizstackperks.com |
| `LETTER_BRAND_STREET` | 701 Dana Dr (owner's home, also the business address) |
| `LETTER_BRAND_CITYSTATEZIP` | Chesapeake, VA 23321 |
| `LETTER_BRAND_LICENSE` | *(empty — business ID deliberately not published)* |
| `LETTER_SENDER_NAME` | Shaun O'Leary |

Phone, email and the return address are printed. **The business ID / license number is
intentionally withheld** — the owner supplies it themselves if a homeowner chooses to
hire them. Every letter carries an opt-out line naming only channels that are actually
printed, plus the return address, which is what makes a bounced letter actionable.

Only a missing **return address** trips the red `DRAFT - letterhead incomplete` warning.
A blank license is not "incomplete" and must never trip it.

### A mailed letter counts as one touch

This is the rule to hold onto: **the letter is the lead's first touch and consumes the
same single touch an email would.** If it did not, a lead could get two letters *and*
two emails — four contacts against a once-then-5-days rule.

`comms_logs` had no `lead_id` and counted only per `(channel, recipient)`, so a letter
logged as `channel='mail'` opened its own counter at zero. Two changes, mirrored in
**both repos**:

- `comms_logs.lead_id INTEGER` (nullable) + partial index, added by idempotent
  `ALTER TABLE ... IF NOT EXISTS`. Nullable on purpose: historical rows stay NULL so
  this backfills forward instead of resetting anyone's touch history.
- `contact_policy_allows` now counts every outbound touch to the lead across **all**
  channels when `lead_id` is known, and takes the **max** of that and the old
  per-`(channel, recipient)` count. The max matters: older rows have `lead_id = NULL`,
  so a lead-wide-only count would read them as zero touches and re-contact people who
  already ignored us.

Behaviour with a letter on file (verified on the hosts copy):

| State | Result |
|---|---|
| letter mailed 0d ago | email blocked — `follow-up waits 5.0d` |
| letter mailed 6d ago | email allowed (the one timed follow-up) |
| 2 letters, any age | closed permanently — `contacted 2x with no reply` |

`test_contact_policy.py` covers the cross-channel count: a letter blocks a same-day
email, permits the one 5-day follow-up, closes the lead after two, and legacy
NULL-`lead_id` history still counts. 22 pass. The suite previously asserted
`policy is per-channel: text starts fresh`, which passed *vacuously* — the fake DB
returned the same count for every query, so it could not have detected the
per-channel bug. Verified the new assertions fail (3 failures) when the
cross-channel branch is disabled.

### Not verified

The lead query has never run against the production database (no `DATABASE_URL` in the
dev environment). Run `--list` and eyeball the names and addresses before rendering.

---

## House rules

- Real beats plausible. A stale or invented number in lender material is worse than a
  missing one.
- Prefer keysless open data over scraping anything that refuses programmatic access.
  Two city systems explicitly reject direct API calls; that is a no, not an obstacle.
- If a fix touches one brand's logic it almost always touches both. Verify both.
- Say what you verified and how. Several real bugs here were invisible to logs and only
  surfaced by rendering or running the code.
