# Memory

## The legal entity is BuildStack Construction LLC (since 2026-10-28)

Organized with the Virginia State Corporation Commission on 2026-10-28:
**BuildStack Construction LLC**, SCC entity ID **12097281**, filing no.
26102810636640, registered to Shaun David OLeary at 701 Dana Dr, Chesapeake,
VA 23321 (the owner supplied the certificate, order and $100 receipt on
2026-10-29). Capital S in "BuildStack" — the registered spelling. Before this
the business traded as "Buildstack Construction Co." (sole proprietorship,
EIN 20-1755044).

`brand.py` is the single source for the name (`name()`, `short_name()`,
`entity_id()`, `entity_line()`, `mailing_address()`), defaulting to the LLC
and overridable by `COMPANY_NAME` / `COMPANY_ENTITY_ID` /
`SENDER_PHYSICAL_ADDRESS` — all three set to the LLC in Railway on
2026-10-29, along with `SMTP_NAME`. Every customer-facing string (emails,
SMS, Stripe product, PDFs, quote seller, Zoho company, knowledge files,
templates) reads from it. `short_name()` ("BuildStack Construction") is for
greetings and subject lines; the legal page and footer carry `entity_line()`.

**Not changed, on purpose — owner decisions pending:**
- **Lending material** (`SBA_7a_LOAN_PITCH.md`, `LOAN_OUTREACH_SEQUENCE.md`,
  `build_loan_package.py`, `docs/`, `send_today/LOAN_PACKAGE.pdf`) still
  describes a sole proprietorship. An SBA application names a legal entity;
  switching it is a loan decision (new EIN? new bank account? the PNC trail is
  the owner's), not a find-and-replace.
- **Stripe** account `acct_1UAeKoJ6MbAT3Jcc` is registered as an individual /
  "BuildStack Construction". Business type must be changed to LLC in the
  Dashboard (Settings → Business details) and will re-verify with the LLC's
  EIN; payouts can pause during review.
- **10DLC brand** `460486de-…` was registered with TCR as "Buildstack
  Construction Co.", PRIVATE_PROFIT, EIN 20-1755044. If the LLC has a new EIN
  the brand must be updated or re-registered, or the pending campaign will
  fail carrier vetting on an EIN/name mismatch.
- **Vapi** assistant `80156d94-…` `firstMessage` lives in the Vapi dashboard
  and still says the old name; the bot's prompt (knowledge files) is updated.
- `CONTRACTOR_LICENSE` is still unset (owner chose "skip for now" 2026-10-29).

## Project facts that aren't obvious from the code

**Construction and Hosts are now two separate companies.** On 2026-10-11 the
owner split them: separate domains (`construction.bizstackperks.com` /
`bizstackperks.com`), separate Railway projects (`281375d7-…` /
`d5ed0396-…`), separate Postgres databases, and no shared package. This repo is
Construction. It imports nothing from `../bizstack-hosts` and `requirements.txt`
must never name a git URL. See the topology table in `AGENTS.md`.

What that split cost, and what it did **not** fix: the databases are cleanly
partitioned (construction holds 0 rows tagged `company='broom'`; the hosts
database holds 0 tagged `construction'`), but `construction_main.py` is still
the forked monolith and contains the entire Broom application — `crew`,
`timesheets`, `payroll_runs`, `clock_events`, the crew admin and training-quiz
routes, and a Broom branch in the Google OAuth callback at `:2194`. Both apps
still create all 52 tables at startup. Anyone assuming "construction repo =
construction business" will be wrong about the code, though right about the data.

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

## 2026-10-29: the money path was broken in four places at once

Found by reading the 2026-10-28 deploy logs, not by any alert — nothing in
this app pages the owner. All four were silent from the outside.

- **`OPENAI_API_KEY` is revoked.** The Railway value is still the `sk-proj-x-Dy…`
  key the 2026-10-20 audit recorded, well-formed, but OpenAI answers 401
  "Incorrect API key" (verified live from `railway run`). That one key powers
  the Vapi phone line, the site chat widget and the Copilot, so all three went
  down together. The phone failure is the worst: `/vapi/llm` let the
  `AuthenticationError` escape as a 500, and Vapi's behaviour on a failed
  custom-LLM call is greeting-then-silence. Fixed the *shape* of the failure
  (the endpoint now speaks `VOICE_MODEL_DOWN_TEXT` and returns 200; the chat
  widget answers `guest_fallback()` with the phone number instead of promising
  a follow-up nobody is told about; the Copilot raises `ModelUnavailable`
  naming the variable). **Only the owner can fix the cause: a new key at
  platform.openai.com → Railway `OPENAI_API_KEY`.**
- **`EMAIL_TRANSPORT=legacy` was left set in Railway.** `sessions.md` calls it a
  debugging switch and says "Don't". It forced every send onto Zoho SMTP,
  which times out on 587/465/25 from Railway egress, while the same boot's
  preflight logged `Gmail send verified`. Owner lead notifications, homeowner
  auto-replies and fired drafts were all dying. Set to `gmail` 2026-10-29.
- **Stripe deposit links 502'd on every click.** `stripe>=16` pins API
  `2026-09-30.endive`, which rejects `payment_method_types` on Checkout
  Session creation. Removed; payment methods now come from the Dashboard.
  `test_deposit_atomicity.py` pins the absence.
- **Fired SMS drafts logged "sent text" on a 422.** `fire_lead_draft` ignored
  `send_sms`'s False, so a SignalWire `inactive_campaign` refusal (10DLC still
  pending) was recorded as a touch and counted against the contact policy.
  Now reports `text transport refused`.

Also: `CONTRACTOR_LICENSE` is unset, so the footer says "Licensed, bonded &
insured" with no number (Virginia DPOR requires it on advertising). And the
`/.webmcp/bridge.js` script on every page (75 KB, `data-packs="c2pa,mcp-server-client"`)
is injected by Cloudflare at the edge, not by this app.

**And a fifth, which hid the other four: no git push had deployed since
2026-10-08.** Railway's "Wait for CI" was skipping every commit because a
`Deploy to Cloud Run` GitHub workflow (no GCP vars, fails in 8s) ran on each
push. Every "fix" committed in that window only reached production if someone
also ran `railway up` by hand. The workflow is manual-only now. Both this and
the watchPatterns story are in AGENTS.md under Deploy.

The live **appearance theme** (set from `/appearance`, stored in the DB) is a
custom one as of 2026-10-29: body `#0a0a0a`, accent `#48413d`, Georgia serif,
🎃 pinned bottom-right. Not any preset; `site_theme.py` applies it with
`!important` over every indigo CTA. Left alone pending the owner's say-so.

## Account map — audited 2026-10-20 (supersedes AGENTS.md's Zen section)

- **OpenAI** — funded again. New project key (`sk-proj-x-Dy…`) in Railway; the old
  key was retired with `credit_balance_exhausted`. Copilot is back on OpenAI
  (`COPILOT_MODEL=gpt-4o-mini`, `COPILOT_BASE_URL` deleted). Voice and web_search
  share this key. Verified with a live tool call.
- **OpenCode Zen** — the free tier now **403s every non-OpenCode client**
  (`FreeTierError`) regardless of key; the 2026-10-10 "verified live" note in
  AGENTS.md is stale. Console keys (`key_…` is only a tracking id; the secret is
  the `sk-…`) authenticate at `opencode.ai/inference/openai/v1`, not `/zen/v1`.
  `_DEFAULT_FREE_MODELS` in `con_ai_agent.py` was refreshed (space-bunny-free and
  ling-3.1-flash-free had rotted off the model list).
- **Skip Sherpa** — FUNDED and verified live 2026-10-20: returns owner name,
  emails, and phones with DNC flags. The Railway key was rotated (`d431d7e2-…`);
  the old `7e5446…` got a 401 after the account change. This is now the contact
  provider of record. `SKIPTRACE_ENRICH_MAX` raised 1 → 25.
- **PDL** — RETIRED 2026-10-20. The Copilot's `pdl_contact` tool was renamed
  `contact_lookup` and rebacked by Skip Sherpa; `PDL_API_KEY` was deleted from
  Railway (backed up in the owner's local `.env`), so the leftover PDL web
  routes honestly report not-configured. `data_enrichment` no longer imports
  `enrichment` at all. Do not reintroduce it — the owner's instruction was
  "not pdl", and Skip Sherpa is the fundable provider.
- **RentCast** — the pasted key `32201f…` answers (an auth failure now shows as
  404 "not in my database", not the 403 the retired key gave). It is the
  **free Developer plan: 50 requests per calendar month** — a courtesy tier,
  not a paid pipeline. All RentCast callers share ONE budget in
  `property_service` (`budget_allows`/`charge_call`, `RENTCAST_MONTHLY_LIMIT`,
  default 50). As of 2026-10-20 `skiptrace_service.fetch_rentcast` charges that
  budget too — it previously did not, and the nightly enrichment loop walking
  that fallback would have starved the property tool, the AVM and the
  sold-leads sweep within days. Over-budget fetches refuse with a
  spendable 429 `ProviderError` instead of calling.
- **Accurate Append** — still 401 (no subscription). Skip Sherpa covers the same
  ground now; leave it or cancel the trial, but do not treat it as the path.
- **Vapi** — owns the business line `+17579087121` (org `d4232da5-…`, number
  record `fc973bf0-…`, "bizstack-voice", active). It is NOT on SignalWire.
  The assistant is `80156d94-2c6a-40e3-a027-f4b48ea928f9`, voice
  `11labs/eleven_turbo_v2_5` **voiceId `ryan`**, transcriber deepgram `nova-3`.
  **Its `model.url` lives in the Vapi dashboard, not in this repo.** It was
  created 2026-09-21, when both apps shared one host, so it kept
  `https://bizstackperks.com/vapi/llm` (the *hosts* domain) after the
  2026-10-11 separation. That URL 500s, and Vapi's behaviour on a failed
  custom-LLM call is to speak the assistant's `firstMessage` and then **go
  silent for the rest of the call** — so the phone bot "answers" and then never
  replies again. Fixed 2026-10-22 by pointing it at
  `https://construction.bizstackperks.com/vapi/llm`, and
  `vapi_service._sync_assistant_url()` now repoints it on every boot and
  before every outbound call, so a future domain move cannot silently kill
  the voice line. Diagnose this first if the bot goes quiet: `GET
  /assistant/80156d94-…` and read `model.url`. Outbound calls are safe either
  way — they override `model.url` per call via `assistantOverrides`.
  The second assistant in the org, `c96b850f-…` ("Riley"), is an unrelated
  Wellness Partners scheduling agent, not ours.
- **SignalWire** — the account is space `bizstack-perks.signalwire.com` (project
  `3c6eb174-…`) and holds only the assistant line `+17578469275`.
  `SIGNALWIRE_API_TOKEN` was set 2026-10-20 (`swapi_…`, owner-created in the
  portal) and verified against the phone-numbers API. **The Campaign Registry
  is fully REST-API accessible** with this token (`/api/relay/rest/registry/beta/…`,
  Basic auth `ProjectID:APIToken`) — the old "portal-only" belief was wrong URL
  paths. 10DLC registration submitted 2026-10-21 after the owner topped up the
  SignalWire balance and added a card (TCR registration requires Auto Top-Up
  active, which is why a bare account gets `insufficient_balance`): brand
  `460486de-2b8d-4462-827d-1afcf25089ff` (Buildstack Construction Co., EIN
  20-1755044, PRIVATE_PROFIT, CONSTRUCTION), campaign `a76073da-4b44-48c3-97ee-2f765f18704d`
  ("BuildStack Construction Lead Outreach", MARKETING, lead_generation, samples
  embed (757) 908-7121), number order `0932cce2-a69d-49e6-a615-034d8818ddef`
  (+17578469275). All three were `pending` at submission; carrier review ~1–3
  days. Poll: GET brand / campaigns/{cid} / orders/{oid} (note: campaign and
  order retrieve under `/registry/beta/{campaigns,orders}/{id}`, not nested).
  The site number (757) 908-7121 stays on Vapi forever; SMS is plumbing-only
  on 846-9275.
- **Google** — GCP project `prismatic-will-507203-k8` (Maps key + Vision + the
  `CON_GOOGLE_*` OAuth login client + Gmail send). The Maps key answers
  `REQUEST_DENIED`: **billing is not enabled** on the project, so every Maps API
  call fails until billing is attached at console.cloud.google.com.
- **Railway cleanup** — 17 unused variables deleted (HOSPITABLE_PAT,
  CHECK_*, EMAIL_BOT_*, X_API_KEY with a literal trailing tab, etc.). Full
  pre-cleanup dump lives in the owner's local gitignored `.env`.

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

## SAM.gov is GONE — the owner does not do government work

On 2026-10-13 the owner removed the federal lead source entirely: "I don't want
to work government jobs." This is a business decision, not a bug, and it is
**removal, not a flag**:

- `lead_sources.py` is now Scrap.io only. The SAM.gov scanner, its NAICS
  presets, the `LEAD_SOURCES_SAM_GOV` toggle and `SAM_SEARCH` endpoint are
  deleted. Do not reintroduce them — a disabled scanner is one env var away from
  resurrecting itself, which is why it was cut out.
- All 78 `sam-gov` rows were deleted from the `leads` table (77 carried real
  `.mil`/`.gov` addresses), plus their 78 `lead_source_seen` rows. Backup:
  `~/bizstack-construction-backups/sam-gov-leads-2026-10-13.csv`.
- `auto_reply`'s federal **template** is gone. The federal **domain** block
  (`blocked_reason` → `_is_federal_domain`) deliberately STAYS: it keys off the
  address rather than the lead source, so it still stops a hand-imported or
  otherwise-sourced `.gov`/`.mil` address from ever being mailed. That guard is
  not about SAM.gov and should not be removed with it.
- `send_bid_inquiry` was **kept** — it is misleadingly named but also serves
  `permit_finder` and `scrap-io`, which are the customers actually wanted.
  Removing it would have cut commercial outreach too.

Consequence: remaining lead sources are `permit_finder` (948), `reddit` (210),
`backlog` (2), `ai-assistant` (1).

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

## A handler without a schema is invisible, and it fails silently

This is the most expensive wiring mistake in this repo, because nothing errors.
`build_copilot_handlers` populates a plain dict, `_operator_tools()` builds the
schema list the API actually offers the model, and **the two are reconciled by
hand**. A handler with no schema is dead code that looks wired.

The Copilot answered "I am unable to access the property information and names
for the leads" on 2026-10-10. Not a provider outage: `skip_trace_owner` and
`save_contact` had handlers but no schemas, and `lookup_zip`, `pdl_contact` and
`draft_lead_email` did not exist at all. Four of five pipeline steps were
unreachable. The model reports a missing capability rather than erroring, so the
only symptom is that it says it cannot do the thing.

The same bug runs the other way, and did: `send_email_message`'s handler was
popped from the Copilot on 2026-10-03 (correctly — it had looped and sent three
identical emails to a public office), but its **schema was left behind**. The
model was offered a send button whose only possible result was `No handler for
tool`. Fixed by filtering it out of `_full_tools()`.

**Check both directions before believing either is done:**

```python
advertised = {t["function"]["name"] for t in BusinessAIAgent(subset="copilot")._tools()}
dead        = advertised - set(build_copilot_handlers(db, owner_email))  # can only fail
unreachable = set(build_copilot_handlers(db, owner_email)) - advertised  # does nothing
```

`test_lead_pipeline_tools.py` pins this for the permit pipeline. Six handlers
remain unreachable (`find_people`, `get_inventory`, `navigation_guide`,
`reorder_report`, `sync_calendars`, `update_inventory`) — all benign internal
lookups, none left off on purpose.

`send_sms_message` is still fully wired to the Copilot, with the same hazard
profile as the email tool that was withheld: no rate limit, no per-recipient
dedupe, no allowlist, no confirmation. It was not named in the 2026-10-03
withholding so it was left alone — but it is the obvious next thing to decide.

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

## Daily lead enrichment autopilot — live since 2026-10-20

`lead_enrich_loop.py` runs a daemon thread started from `lifecycle()`. Once per
calendar day (marker `lead_enrich_last_run` in `app_settings`, written BEFORE
spending so a crash cannot re-spend the day) it takes the newest address-only
unreachable leads, capped at `LEAD_ENRICH_MAX` (default 10), and runs each
through the existing free pipeline: HRGEO parcels → Skip Sherpa (at most one
billed credit per lead) → `data_enrichment.merge` write-back → a first-touch
draft staged into `leads.draft_reply` when the lead gained a real email.

**Nothing here sends.** The owner reviews and fires drafts from the leads page
(`auto_reply.fire_lead_draft` re-checks caps and contact policy at send time).
A phone-only gain does not stage a draft — email is the channel that sends
today; SignalWire SMS is submitted and pending 10DLC carrier review (brand
`460486de-…`, campaign `a76073da-…`, number order `0932cce2-…`, all `pending`
as of 2026-10-21 — see the Account map).
`DISABLE_LEAD_ENRICH` kills it (explicit parsing — Railway's string "false" is
not ON). First live run: 10 examined, 7 contacts, 7 drafts, 10 Skip Sherpa
lookups.

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
  are all unset. `lead_research.py` in the **hosts** repo is the thing to
  point a key at — not the PDL path in that same repo, which now lives in
  *this* repo as `enrichment.py`. It runs live and returns
  `0 enriched, 10 unresolved` on every pass purely because skip-trace is inert.
  It is the only code that produces a homeowner name/phone. `RENTCAST_API_KEY`
  is now in use by `skiptrace_service.py` — see above for the one line to set.
- SAM.gov is off (`LEAD_SOURCES_SAM_GOV=0`) and the operator does not want it.
  The federal block in `blocked_reason` is still armed and deliberate.
- `mail_letters.py` is offline by design: it never writes `comms_logs`. Marking
  a letter as mailed is deliberate, via `record_manual_touch` (the four buttons
  on each lead card) or `lob_send.py` doing it automatically.
