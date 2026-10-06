# Memory

## Project facts that aren't obvious from the code

**Two phone numbers, and which is which.** `757-908-7121` is the business line
and appears on every homeowner- and customer-facing surface: lead auto-replies,
bid inquiries, the website, printed letters, invoices, receipts, the photo log.
`252-665-5891` is Shaun's personal mobile and appears **only** in lending
material — `loan_outreach.py`, `LOAN_OUTREACH_SEQUENCE.md`,
`SBA_7a_LOAN_PITCH.md`, and the lender sections of `docs/OUTREACH_EMAILS.md`.
The reasoning: a lender asking about a $50K microloan wants to talk to Shaun; a
homeowner receiving an invoice is already in a self-service conversation with
the bot. Never widen the 252 without asking.

**Return address** is `701 Dana Dr, Chesapeake, VA 23321` (owner-confirmed).
It prints on the letterhead and is the `from` address on mailed pieces, so USPS
routes undeliverables back there.

**Lob** is the print-and-mail provider (`lob_send.py`). There is no USPS
print-and-mail API. Lob's letter format is 8.5x11, which is what
`mail_letters.py` already renders. Developer tier only — the $260/mo Startup
tier is not worth it for a few hundred pieces.

**Lob has never been used to send.** The first `--send` should be `--limit 1`,
because the multipart request shape is unverified against their live endpoint.

## The lead data is thinner than the row count suggests

`leads` mixes rows that are the same property. Permit feeds emit one row per
permit, so a house with a deck permit and a roof permit is two leads with the
same address. The Sept 2026 print batch was 177 letters over **58 deliverable
doors** — 104 duplicates, and 15 more held back for a missing ZIP. Always
dedupe on normalized address before spending money or postage on a list.

Neither open permit feed publishes an applicant or contractor name. Virginia
Beach exposes `CreatedBy` as `PUBLICUSER<n>`; Norfolk has no applicant field at
all. So `leads.name` holds a **city** for permit leads, which is why letters
greet as "Dear the homeowner," and why an empty `contractor_name` means
*unpublished*, not *self-applied*. Applicant identity only comes from the VFOIA
request drafted in `docs/records-request.md`, which is still unsigned.

## How to verify against the real system

No local `psycopg` or `fpdf`, and the venvs are broken (`myenv/bin/pip` has a
stale shebang, no `pip` module). Working interpreter for DB and PDF work:

    python3 -m venv /tmp/lettersenv
    /tmp/lettersenv/bin/pip install "fpdf2==2.8.8" "psycopg[binary]" jinja2 stripe pypdf

`railway run` executes **locally**, not in the container, so `pgbouncer.railway.internal`
never resolves from this machine. To reach the live DB, open a tcp-proxy
(`railway tcp-proxy create --port 5432 -s Postgres --json`) and build the URL from
the `PGUSER`/`PGPASSWORD`/`PGDATABASE` vars on the Postgres service. Close it and
delete the URL afterwards.

`railway ssh` does not work (no key registered). `docker build` cannot run — the
Docker daemon is not running, so Dockerfile changes are unverified.

Outbound TLS from this machine needs `SSL_CERT_FILE=/etc/ssl/cert.pem`; the
system Python has no CA bundle. The same gap is why commit ff8e842 added
ca-certificates to the image.

## Lead sourcing is business contacts only

The owner chose the narrow path on 2026-10-02: Copilot researches **business**
contacts (company site, public business listing, VA SCC entity record, eVA
vendor data) and does not harvest personal profiles. A roofer or plumber with a
real business line is the lead; a homeowner is not.

This is enforced in `copilot_ops._BLOCKED_CONTACT_DOMAINS`, which rides in the
`web_search` tool's `filters.blocked_domains` — not in a prompt. A prompt
instruction is a preference the model can be talked out of; a blocked domain is a
boundary. `SEARCH_BLOCKED_DOMAINS` (comma list) overrides it.

Note the two sources Copilot *offered* when search was down — LinkedIn and
"public records listing property owners" — are exactly the two this blocks. If
search ever fails again, that offer is the model falling back to the boundary, not
a workaround.

## OpenAI retired the search-preview models

`gpt-4o-mini-search-preview` / `gpt-4o-search-preview` and the
`web_search_preview` tool type were **shut down 2026-07-23**. `web_search` on a
normal model (`gpt-4.1-mini`, `gpt-4.1`, `gpt-5.5`) is the supported path.
`copilot_ops.build_web_search_tools()` had the dead names hardcoded, so every
call 404'd and the Copilot reported search as unavailable for weeks. It also
read `.part` off a message item's `content`, which is a *list* — so citations
were silently always `[]` even on success.

`OPENAI_SEARCH_MODEL` pins one model. Note `openai` is not installed in either
repo venv, so this path is only exercisable in production; `test_web_search_tool.py`
injects a fake client instead.

## Skip trace is live on the site, and it is a deliberate exception

`skiptrace_service.py` + `POST /api/skiptrace` (admin-only) resolve a permit
address to an owner of record via RentCast. This **contradicts** the
business-contacts-only decision above, and the owner chose it knowingly on
2026-10-03. The reason it is visible rather than buried: every lookup writes to
`skiptrace_audit` with the requesting user, and results carry `is_residential`,
so the exception is legible in the data. To reverse it, filter on that flag —
nothing else needs to change.

Two things make it survivable. **RentCast's free plan is 50 calls per MONTH**,
so lookups are cached on the normalized address forever, misses included; a
repeat lookup is a table read. And the endpoint is POST, so homeowner PII never
lands in access logs or a Referer header.

`RENTCAST_API_KEY` must be set in the Railway environment. The local tool at
`~/skipTraced/main.py` has the key **hardcoded in source** at `main.py:99` —
that is the leak to fix if this repo or that file is ever shared.

### Why 400 and 404 are answers, not errors

RentCast returns 404 for "address not in my database" and 400 for "cannot parse
this address". For a tier-1 provider that is most of Virginia. The original
local tool raised a 502 on any non-200, which aborted the whole waterfall — so
the addresses that most needed a fallback were exactly the ones that threw.
That was the reported "works sometimes, otherwise errors". Any new provider
layer must return `NOT_FOUND` for both.

## Accurate Append — the contact step, currently blocked on billing

`accurate_append.py` turns a name + address into an email and a phone. It exists
because the skip trace structurally cannot: RentCast returns an owner NAME from
the assessor record, and **no public record in the US contains an email address
or a phone number.** A name is not a contact. This is the same conclusion the
"going to the source" idea reached, and it is a fact about registries rather than
a limitation of a particular tool — county assessors, deeds and court records
publish ownership, never contact details.

**Blocked as of 2026-10-03.** Both `AppendEmail` and `AppendPhone/SBMMobile`
answer 401:

> You do not have an active subscription or are not authorized to access this
> endpoint. Please contact customer support.

**It is not knowable from outside whether the key is valid.** A deliberately
invalid key returns the *identical* 401 — verified by sending
`00000000-0000-0000-0000-000000000000` and getting the same body. So this could
be a bad key OR a valid key with no active subscription, and the API gives no
way to tell them apart. An earlier note here claimed the key was recognised
because the error was not `"License key is required"`; that inference was wrong
and has been tested and retracted.

To resolve: confirm `2e900471…` appears as the License Key at
`clients.accurateappend.com`. If it does, the trial/subscription needs
activating — the owner believes 100 free lookups a month are available, and they
are not currently on this account. Until then
`POST /api/leads/{id}/enrich-contact` returns the provider's message and spends
nothing.

Two design points that matter when it is switched on:

- **B1/B2 match levels are rejected.** Household-level matches are frequently a
  different adult at the same address. Attaching one of those numbers to a named
  lead means cold-calling a stranger who believes they are a customer. They are
  counted in `rejected_weak`, not silently dropped.
- **Phones are recorded, never dialled.** These are residential numbers. A DNC
  check is a legal requirement before calling or texting, not a preference.

`SKIP_SHERPA_API_KEY` remains unset and remains the other route to the same data.
Accurate Append at 100/month is roughly twice Skip Sherpa's free allowance, so
prefer it first and keep Skip Sherpa as the fallback.

## Stripe is on the SECOND account, and the first one was empty

Live account is now **`acct_1UAeKoJ6MbAT3Jcc`** ("BuildStack Construction"),
switched 2026-10-05. The old `acct_1U21rk7FqkxpR5Dt` ("Shaun OLeary") is
**retired** — don't point anything back at it.

The reason for switching was not a preference. The old account had charges and
payouts enabled but had only ever produced **3 failed charges** ($49 each, Aug
27–30) and **4 `payout_failure`** balance transactions at -$99.50, meaning the
bank was rejecting withdrawals. It also billed **-$90/mo for the Stripe Tax
product subscription** while collecting nothing. Zero successful revenue, so it
was worthless as income evidence for a lender.

**Four endpoints, and the two brands need different secrets.** Each service holds
the secret for its *own* endpoint; swapping them 400s every event:

| | construction | bizstackperks |
|---|---|---|
| payments | `we_1UNNMNJ6MbAT3JccolNX76Dc` | `we_1UNNMNJ6MbAT3JccAw20YIYy` |
| connect | `we_1UNNMOJ6MbAT3JccR5Ms69nj` | `we_1UNNMOJ6MbAT3Jcc5UYJ1SSa` |

The old account had exactly ONE endpoint, pointed at `bizstackperks.com` only —
which is why construction deposits never marked a lead paid. Both brands now
receive events; that was a deliberate owner decision, not an oversight.

Two bugs found while wiring this, both the same root cause:

- **`connect_webhook` 500'd on every event.** `eobj.get("id")` on a stripe>=8
  `Account` raises `AttributeError`, so Stripe retried and gave up, silently
  stranding `crew.bank_status`. The identical bug in the payments webhook was
  fixed in 3caecb7; this one was missed. Fixed in 4a6953d.
- **`STRIPE_CONNECT_WEBHOOK_SECRET` was not a Stripe secret** — a 32-char hex
  value with no `whsec_` prefix. It could never verify anything.

`bizstack-perks` hardcodes a fallback price ID at `main.py:1583` and the hosts
service had no `PRICE_ID_99`/`PRICE_ID_49`, so swapping the key alone would have
broken Broom Service subscriptions silently. Both are now set to prices
recreated on the new account.

**Verified by signed probe, not by inspection:** a hand-signed
`checkout.session.completed` returns 200 on both payment endpoints, and an
`account.updated` returns 200 on both connect endpoints. The probe payload has
`lead_id` empty so it writes nothing.

## Pulling income evidence for a lender

`stripe_income_report.py` reads the live account and writes a dated markdown
report plus raw JSON to `docs/stripe/` (gitignored — charge metadata carries real
customer names):

    STRIPE_SECRET_KEY=sk_live_... python3 stripe_income_report.py

It reports **payouts that actually landed** as the headline figure, and keeps
failed charges and failed payouts in separate sections instead of netting them
away. That split is the whole point: the old account's dashboard could have shown
a plausible-looking balance while four -$99.50 payouts were bouncing. A report
that hides that is not evidence.

It reads the key from the environment and never writes it into the report or the
JSON. Verified against the live account, and against synthetic data confirming a
failed payout is never counted as income.

Revenue recognition is a separate thing and is **not** set up. Stripe Billing is
the wrong tool for it here: construction deposits are one-time milestone payments,
not subscriptions, and Billing recognizes subscription revenue across a term for
bookkeeping. It is not a lending signal either. What builds a case is volume —
settled deposits over time.

## Capital: Stripe cannot be the source of today's funding

Worth being blunt, because it comes up. Stripe **Capital** underwrites against
payment history on the account, and this account had zero history at cutover.
There is no basis for an offer yet. Income evidence accrues from now on.

For the $31,900 already worked, the path stays `docs/CHECKLIST.md`: SBA 7(a),
where the EIN letter substitutes for the 3-year return history a new business
doesn't have. The PNC $22,500 deposit is the only real bank trail; the $9,400
cash remainder is undocumented. Getting deposits to actually flow through Stripe
is what makes the *next* loan easier, not this one.

## Open threads

- `ATTIC_API_KEY` / `SERVICEKANI_API_KEY` / `REGRID_API_KEY` / `BATCHLEADS_API_KEY`
  are all unset, and `lead_research.py` in the **hosts** repo is the thing to
  point a key at — not the PDL path in that same repo. It runs live and returns
  `0 enriched, 10 unresolved` on every pass purely because skip-trace is inert.
  It is the only code that produces a homeowner name/phone. `RENTCAST_API_KEY`
  is now in use by `skiptrace_service.py` — see above for the one line to set.
- SAM.gov is off (`LEAD_SOURCES_SAM_GOV=0`) and the operator does not want it.
  The federal block in `blocked_reason` is still armed and deliberate.
- `mail_letters.py` is offline by design: it never writes `comms_logs`. Marking
  a letter as mailed is deliberate, via `record_manual_touch` (the four buttons
  on each lead card) or `lob_send.py` doing it automatically.
