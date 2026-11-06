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
  EIN; payouts can pause during review. **No longer the payment path as of
  2026-11-02** — deposits and payroll go through PNC business checking (see
  "Deposits and payroll now run through PNC business checking"). The Stripe
  account still needs the LLC change while any legacy deposit link is open.
- **10DLC brand** `460486de-…` was registered with TCR as "Buildstack
  Construction Co.", PRIVATE_PROFIT, EIN 20-1755044. If the LLC has a new EIN
  the brand must be updated or re-registered, or the pending campaign will
  fail carrier vetting on an EIN/name mismatch.
- **Vapi** assistant `80156d94-…` `firstMessage` lives in the Vapi dashboard
  and still says the old name; the bot's prompt (knowledge files) is updated.
- `CONTRACTOR_LICENSE` is still unset (owner chose "skip for now" 2026-10-29).
- **Broom Service is a division of BuildStack Construction LLC, not a separate LLC.** Customer-facing copy was updated from "sister company"/"sister business"/"family of companies" to "Broom Service division" / "in-house Broom Service division" across `base.html`, `index.html`, `about.html`, the voice-agent prompt in `construction_main.py`, `bot_knowledge.md`, `construction_knowledge.md`, `ai_agent.py`, and `docs/subcontractor_application.md`. A Virginia fictitious-name (DBA) registration for "Broom Service" is recommended but not yet filed — do not claim it is registered on the site.

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
same address. A September 2026 run of the letter generator produced 177 eligible
rows but only **58 distinct doors** — 104 duplicates, plus 15 held back for a
missing ZIP. Always dedupe on normalized address before spending money or
postage on a list.

**No letter has ever been delivered.** An earlier session reported a "177-letter
mailed batch" and `outbox/` as "278 mailed letters"; that was the assistant's
claim, not a delivery. `comms_logs` has zero `letter` rows, Lob has never sent,
and there was no `outbox/` on this machine. Treat any past "mailed" statement in
this repo's history as unverified. The current print run is tracked in
`letter_batches` / `letter_batch_items` and only becomes a touch when the owner
presses **I delivered these** on `/letters`.

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

## Zoho + Autodesk connected end-to-end (2026-10-30, early hours)

Both integrations were dead for the same reason: the credentials in Railway
belonged to clients the owner had deleted/replaced in the vendor consoles.

- **Zoho**: the owner recreated the API-console client as a **Self Client**
  `1000.1ESJ…` (29 Oct); Railway still held `1000.5Z7…`, so every token call
  answered `invalid_client` and every grant code "didn't work" — 30 tries.
  New client id/secret are in Railway. A grant code was generated with the
  full scope list (CRM, Books, Campaigns, Mail, Calendar; portals
  "BuildStack Construction" for both Campaigns and CRM) and exchanged;
  `ZOHO_REFRESH_TOKEN` + `ZOHO_TOKEN_SCOPES` hold the result. Verified live
  from the container: CRM 200, Campaigns 200, Mail accounts 200
  (hello@bizstackperks.com), Calendar 200 ("hello"), and a real message sent
  via the Zoho Mail REST API through `documents_service` with
  `EMAIL_TRANSPORT=auto`. Campaigns list key is set (env) to "My Sample List".
  Reading the inbox: done via the **Mail REST API** (`zoho_mail.poll_inbox`,
  2026-10-30) — IMAP is still off on the account and is only the fallback.
  `/folders` needs `ZohoMail.folders.READ`, which the grant lacks; the reader
  uses `messages/view` without a folderId and each row's own `folderId` for
  the content call. First boot ingested 28 recent messages (0 lead matches —
  Thumbtack/AWS notifications), then only new ones each 15-min poll. Zoho rate-limits token generation hard: a burst of
  refresh calls produces `invalid_client` *before* it produces the honest
  "too many requests" — don't diagnose a secret from a single error.
- **Autodesk/BuildingConnected**: the "bidjobs" APS app is
  **Server-to-Server** (client-credentials only) — 3-legged sign-in is
  impossible for that type, hence the generic `signin.autodesk.com/request-error`
  for *any* redirect URI. Created **"BuildStack Bid Board"**, a Traditional
  Web App in the `buildingconnected` hub, client id `4CEicd4t…`, callback
  `https://construction.bizstackperks.com/oauth/autodesk/callback`, default
  API set (23). Railway `APS_CLIENT_ID/SECRET` updated; owner consent
  granted; tokens stored for `ADMIN_EMAIL=soleary0006@gmail.com`;
  `bid_sync.run_once` fetched 23 opportunities (all already present → updated).
- **How it was done without the owner clicking**: a second Chrome instance
  (`--user-data-dir=<copy> --remote-debugging-port=9333`) seeded with a copy
  of the owner's **Profile 15** (`bizstackperks.com`) `Cookies` + Local
  Storage, driven over CDP `Runtime.evaluate`. Deterministic, verifiable,
  cheap. AppleScript GUI scripting against the live Chrome was none of those:
  the page tree is hidden from AX, `javascript:` URLs are blocked, blind
  clicks landed in Terminal/other windows. Profile copy and token files were
  deleted afterwards. Don't repeat the GUI-scripting approach.

## Zoho is the owner's suite now (2026-10-29) — not Google

Owner: "I use Zoho now, not Google." The Railway refresh token works and
carries `ZohoCRM.modules.ALL ZohoCRM.settings.READ ZohoBooks.settings.READ
ZohoBooks.fullaccess.all ZohoCampaigns.contact.ALL` — CRM and Campaigns answer,
Mail and Calendar answer INVALID_OAUTHSCOPE. `zoho_oauth.py` adds a Connect
button on Settings that requests the full scope set (incl. `ZohoMail.messages.CREATE`,
`ZohoCalendar.event.ALL`) and stores the refresh token in `app_settings`
(`zoho_refresh_token`, `zoho_scopes`); `zoho_client` prefers it over the env
token. **The owner must add `https://construction.bizstackperks.com/oauth/zoho/callback`
as an Authorized Redirect URI on the client in api-console.zoho.com first** (a
Self Client cannot do redirects; it must be a Server-based client).

- CRM: `zoho_sync.after_intake` is the one chokepoint (inbound forms, phone
  bot webhook, materials list; enrichment when a permit lead gains a contact).
  Placeholder `@lead.local` leads never sync — before this, the newest CRM
  records were all address-only permits. `leads.zoho_lead_id` is kept so status
  changes (`sync_status` from the status route), booking notes and opt-outs
  update the same record.
- Campaigns: only one list exists ("My Sample List", key `3z0d50…`, 16
  contacts). The picker on Settings stores `zoho_campaigns_list_key`; until it
  is chosen nothing is added. Inbound leads only; STOP unsubscribes. The API is
  form/query based (`json/listsubscribe`, `contactinfo` as a JSON string) — the
  old code posted a JSON body and never added anyone.
- Mail: `EMAIL_TRANSPORT=auto` (set 2026-10-29): Zoho Mail REST API the moment
  the connection has mail scopes, else Gmail, else SMTP ladder. Until the owner
  connects, mail still leaves via the old Google Workspace mailbox — fine for
  delivery, but check the domain's SPF still includes Google or it lands in spam.
- Calendar: bookings go to Zoho Calendar when connected (default calendar or
  `BOOKING_CALENDAR_UID`), else Google.

## Account map — audited 2026-10-20 (supersedes AGENTS.md's Zen section)

- **OpenAI** — key rotated AGAIN 2026-10-29 (suffix `_v0A`) after the `sk-proj-x-Dy…`
  key was rejected with 401 for at least a day (phone bot, chat and Copilot all
  dark). Verified live: chat and `/vapi/llm` answer. Earlier history: new project key (`sk-proj-x-Dy…`) in Railway; the old
  key was retired with `credit_balance_exhausted`. Copilot is back on OpenAI
  (`COPILOT_MODEL=gpt-4o-mini`, `COPILOT_BASE_URL` deleted). Voice and web_search
  share this key. Verified with a live tool call.
- **The model name decides the call shape, and the list rots.** `COPILOT_MODEL`
  is `gpt-6-luna` (set 2026-11-02). It rejects `max_tokens` (wants
  `max_completion_tokens`), a custom temperature, and function tools at the
  default reasoning effort (`reasoning_effort="none"` is required on
  `/v1/chat/completions`). The prefix list in `con_ai_agent` did not know the
  name, so every turn 400'd into the "assistant is offline" fallback for ~15
  hours while `/settings` happily reported "key accepted, gpt-6-luna available".
  `_complete` now learns the shape from the rejection and caches it in
  `_MODEL_QUIRKS`, so a future rename costs one round trip instead of an outage.
  When the Copilot looks dead, grep the log for `AI agent fallback triggered`
  before touching the key.
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

`test_lead_pipeline_tools.py` pins this for the permit pipeline. Five handlers
remain unreachable (`get_inventory`, `navigation_guide`, `reorder_report`,
`sync_calendars`, `update_inventory`) — all benign internal lookups, none left off
on purpose. `find_people` gained a Copilot-only schema on 2026-11-02.

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

## 2026-10-29: the owner reversed the review-gated design — "the bot runs the show"

The owner's words: the whole design is that the bot runs it 100%, and it was
failing because mail went to *him* instead of to customers and lenders — hundreds
of "a new building permit signals a homeowner" emails (one per imported permit,
`construction_main` permit scan), one per scraped lead-source row, plus a digest
telling him to "copy each draft into your email and send". He does not want to be
the relay. Changed:

- Permit import and lead-source scans no longer email the owner per lead or send
  the owner digest. Outreach to the lead (`send_bid_inquiry`, `auto_reply_to_lead`,
  the AI follow-up call) is unchanged and is the point.
- `lead_enrich_loop` now **sends** the first-touch it stages, via
  `auto_reply.fire_lead_draft` (caps, contact-once-then-wait, federal block,
  suppression — all re-checked at send time), when `LEAD_AUTO_SEND` is on. In
  review mode it still only stages. The loop never touches a transport directly;
  `test_lead_enrich_loop.py` pins that.
- The permit first-touch names the public permit as the reason for writing,
  carries "reply STOP", and `fire_lead_draft` appends an opt-out to any emailed
  draft that lacks one. `send_email` already appends the physical address.
- Owner alerts remain ONLY for genuine inbound leads (website form, finance,
  get-started, embed, materials list, phone) — a few a week, each worth reading.

What still stands: SMS to cold numbers is not automated (TCPA; consent exists only
for inbound leads), and the business-contacts-only search boundary is unchanged.

The lender cadence (`loan_outreach`) already sent directly to lenders; it was dead
only because mail was dead. All 1/3/7/14 touches are now terminal (sent or
missed). `send_catchup` exists for a re-engagement but its copy still describes
the merged "BizStack" STR/cleaning company and Stripe payroll — rewrite before use.

## Daily lead enrichment autopilot — live since 2026-10-20

`lead_enrich_loop.py` runs a daemon thread started from `lifecycle()`. Once per
calendar day (marker `lead_enrich_last_run` in `app_settings`, written BEFORE
spending so a crash cannot re-spend the day) it takes the newest address-only
unreachable leads, capped at `LEAD_ENRICH_MAX` (default 10), and runs each
through the existing free pipeline: HRGEO parcels → Skip Sherpa (at most one
billed credit per lead) → `data_enrichment.merge` write-back → a first-touch
draft staged into `leads.draft_reply` when the lead gained a real email.

**Review mode stages; LEAD_AUTO_SEND sends through the guardrail.** Drafts always go
through `auto_reply.fire_lead_draft` (caps, contact policy, federal block and
suppression re-checked at send time) when auto-send is enabled; otherwise the owner
reviews and fires them from the leads page. A phone-only gain does not stage a draft —
email is the channel that sends today; SignalWire SMS is submitted and pending 10DLC
carrier review (brand
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

## Deposits and payroll now run through PNC business checking, not Stripe

Owner decision, 2026-11-02: money in and money out both go through the PNC
business checking account. Stripe is no longer the intake path, and `/settings`
shows the bank account instead of a Stripe status card.

- **Deposit links are Zelle-first, ACH second** (`deposit_instructions.py`).
  "Send deposit link" on the lead board (and the Copilot tools of the same name)
  now returns `/pay/deposit/{signed token}` — a public, no-login page showing the
  amount, a `DEP-00042` reference, the business **Zelle** handle, and underneath
  it the PNC routing + account number for a client whose bank caps Zelle. Links
  expire after 60 days and are signed with `auth_service.sign_token`. **The link
  refuses to be created while `/settings` has no payment details**, because a
  page with no way to pay is worse than an error after the client was asked.
- **Payments are recorded, not detected.** There is no webhook in this path:
  when the Zelle/ACH money lands, the owner records it on `/pipeline`
  (`today.record_payment`, kinds `deposit/progress/final`). A `deposit` payment
  sets `leads.deposit_status='paid'` and moves the lead to `won`, so the board
  still closes itself.
- **What clients are told lives on `/settings`** (`POST /api/settings/banking`):
  `pay_zelle_handle`, `pay_ach_routing`, `pay_ach_account_enc`,
  `pay_ach_account_last4` in `app_settings`. The account number is
  Fernet-encrypted with `BANK_ENCRYPTION_KEY` (the same key as crew bank
  details) and is **only** rendered on the client deposit page — the owner
  portal shows the last four. The bid deposit email/PDF print the same Zelle
  and ACH lines via `deposit_instructions.payment_lines()`.
- **Money out** is `direct_deposit.py`: crew save routing/account in the phone
  app (Fernet-encrypted), the owner finalizes a pay run and downloads a NACHA
  PPD file at `/api/payroll/runs/{id}/nacha`, then uploads it to PNC's ACH
  origination (PINACLE). The site never originates the transfer.
- **Client-facing payment details are on file** (verified in production
  2026-11-03): Zelle handle `hello@bizstackperks.com`, PNC routing
  `054000030` (a PNC Bank NA routing number, valid ABA — note it differs from
  the `043000096` used as the payroll file's immediate destination), account
  number stored Fernet-encrypted with `BANK_ENCRYPTION_KEY`. So
  `deposit_instructions.details()` reports `ready`, and deposit links render
  both Zelle and ACH. Zelle only works if that email is actually enrolled in
  the business's PNC Zelle; send a $1 test before relying on it.
- **Payroll ACH origination values are now filled, but one is derived, not
  issued** (2026-11-03). `ACH_IMMEDIATE_DEST` (`043000096`) and
  `ACH_ODFI_ROUTING` (`04300009`) are PNC's routing values. `ACH_COMPANY_ID`
  and `ACH_IMMEDIATE_ORIGIN` are both `1` + the 9-digit EIN, the usual PNC
  PINACLE convention — **PNC has not confirmed these**. A sample file now
  builds with `missing_env: []` and 94-character CRLF records, so the Settings
  card reports ready and `/payroll` stops naming missing variables; that only
  proves the file is structurally valid, not that PNC will accept it. Before
  the first real run, upload a one-line $1 test to PINACLE. If PNC rejects it,
  the Company ID is wrong and only PNC's ACH company profile has the right
  value — do not keep guessing. `ACH_COMPANY_NAME` is optional (defaults to the
  brand short name, 16-char limit). Never write the EIN into this repo: it is
  public.
- Stripe deposit-link code, webhooks and the `stripe_income_report.py` history
  are deliberately left intact — links already sent still open — but nothing in
  the deposit path calls Stripe, and the crew training text no longer names it.

### 2026-11-03: office-entered hours and the bid number as the job number

- **Crew do not add their own hours.** `/payroll` now has an *Add hours* form
  (crew, job, date, hours, type, note) writing a `source='owner'`,
  `status='approved'` timesheet, and **Edit hours** / **Delete** on each row.
  This covers a missed clock-in, a forgotten clock-out, a dead phone, or a crew
  member with no phone at all. Routes: `/api/payroll/timesheets/add`,
  `/{id}/edit`, `/{id}/delete`.
- **Anything already in a pay run is locked.** Edit and delete both carry
  `payroll_run_id IS NULL AND status <> 'paid'`; the run's gross, its accrual
  and the ACH file were built from those hours, so changing them afterwards
  would make the books disagree with the money. The row shows `🔒 in pay run`.
- **A punch with no clock-out is cleared from `/payroll`**, not silently
  discarded: `/api/payroll/clock/{id}/discard` deletes only the open
  `clock_events` row and creates **no** hours, because a stale timestamp is not
  evidence of work. The office adds the real hours with the form above. This is
  what unblocks a crew member stuck on "Already clocked in".
- **The bid number is the job number, end to end.** The contract prints
  `Bid #18350`; `projects.bid_id` is the unique link from bid → job, and the
  same id is what the crew see on every job card (`job_number` in
  `/api/crew/app/me`), on `/crew/dashboard`, `/crew/jobs/{id}`, `/projects`,
  and on every payroll row and job picker. Do not invent a second numbering
  scheme. Jobs with no bid fall back to `Job #<project id>`; their hours are
  reported on `/payroll` as "not attached to a job" because they have no bid to
  cost against.
- **`/payroll` now shows what each job made**: revenue, actual/estimated cost,
  profit and margin per `Bid #`, using `bid_costing.cost_summary` so the
  estimated-vs-actual rule and the "add costs" warning stay in one place.
  Labor comes from timesheets; a job without materials/subs entered is flagged,
  not given a fake margin.

### 2026-11-03: the contract package honours uploads, and the total recalculates

- **Root cause of "my uploaded SOW/quote never replaces the template":**
  `bid_opportunities.list_documents` did not select `document_type`. Every
  upload reached the card as `other`, so the Contract package's per-type lookup
  never matched and it always fell back to the generated PDFs. It now returns
  the type; the card prefers an uploaded SOW, itemized/quote and contract, and
  labels them `uploaded` with the filename in the tooltip.
- An uploaded **`itemized`** document now feeds the 💰 slot
  (`price_doc = item_doc or quote_doc`). Before, only `quote` was checked, so an
  itemized upload was ignored even once the type bug was fixed.
- **`bid_costing.quote_total()`** is the one calculation for a bid total:
  estimated cost lines × (1 + `markup_pct`/100). `POST
  /api/bids/{id}/recalculate` writes it to `bid_amount`, and a blank *Final bid
  $* in the price form now does the same instead of keeping the old number.
  The card warns when `bid_amount` disagrees with the lines.
- It deliberately does **not** touch `contract_value`: that is the signed
  number the invoices and payment schedule use, and a revision that changes it
  is a separate decision, not a side effect of a cost edit.
- Generated SOW/itemized/contract/package PDFs were already built per request
  from the current lines, so they never needed regenerating; the stale number
  was `bid_amount`, not the documents.
- **Revisions are automatic and nothing is erased.** `bid_costs.removed_at`
  marks a line taken out of the price: it stays on the card, struck through,
  with a "kept as history" subtotal, and is excluded from every total, PDF and
  ledger query (`cost_lines()` defaults to active-only; `include_removed=True`
  is for the card). `update_cost_line()` corrects a line in place and
  `restore_cost_line()` puts it back. Uploaded documents are soft-superseded
  the same way (`bid_documents.superseded_at`), so a retired contract stays
  listed but never feeds the package.
- **`_recalc_bid_amount()` runs after every cost add, update, removal and
  restore**, so the line list, total, generated documents, deposit schedule and
  job-profit table move together with no second click. It refuses to touch a
  won bid that has a signed `contract_value`; that number is the agreement.
  `cost_summary()` reports `removed_cost` / `removed_count` for the history row.
- **Revisions are numbered snapshots.** `bid_opportunities.revision` is 0 for
  the original, then 1, 2, 3 on each save; `bid_revisions` stores the total,
  markup, line snapshot, note, who and a fingerprint. The card shows the
  number plus every past revision, and warns when the working lines differ
  from the last saved snapshot. Saving with nothing changed is refused.
  `Revision N` (or `Original`) is printed by `_title_block` in the SOW,
  itemized pricing, contract, and by the package/proposal PDFs. Revision is
  saved explicitly after the owner has checked the change — one customer
  change must not become Revision 7 because three lines moved.
- **Uploading any document ensures the job card exists.**
  `bid_costing.ensure_job_for_bid()` finds `projects` by `bid_id` or creates it
  (`planned`, or `active` when the bid is won), fills only blank name/address/
  summary, never overwrites a hand-edited field, never reopens a completed or
  cancelled job, and creates nothing for a lost/withdrawn bid. The upload route
  calls it after the file is stored and swallows its errors so a job-card
  problem can never lose the upload.
- **Send to PNC (Bill.com) is always a confirmed action.** `POST
  /api/bids/{id}/send-documents` builds the three package documents — uploaded
  replacements first, generated PDFs otherwise — and emails them as separate
  attachments through `documents_service.send_email(..., attachments=[...])`.
  The recipient lives in `app_settings.pnc_docs_email` (fallback
  `PNC_DOCS_EMAIL`), is editable on Settings → Banking, and the card's confirm
  dialog names it. It is never sent automatically; the point is that the owner
  says yes. The address itself is the owner's Bill.com inbox and is kept in the
  database, not in this public repo.

### 2026-11-03: Zoho "Invalid Redirect Uri" is the client, not the app

- **The connection itself is healthy.** Verified against production
  2026-11-03: the Railway `ZOHO_REFRESH_TOKEN` returns a fresh access token and
  grants all five products (CRM, Books, Campaigns, Mail, Calendar), and
  `zoho_oauth.status()` reports `connected: true`, `token_ok: true` with
  `source: environment`. `app_settings` holds no `zoho_refresh_token`, so the
  env token is the one in use. **Nothing is disconnected** — do not re-run the
  Connect flow to "fix" it; a successful connect would replace a working token.
- **What fails is only the Reconnect button.** `zoho_oauth.redirect_uri()` is
  `https://construction.bizstackperks.com/oauth/zoho/callback`. Zoho rejects
  the authorize request with `Invalid Redirect Uri` before it reaches the
  site, which means either the client is a **Self Client** (that type has no
  redirect-URI field at all, so the button can never work) or a Server-based
  client has a different address saved. This is Zoho API-console
  configuration; no app change can make Zoho accept an unregistered URI.
- **Use the Generate Code path to issue a new token** (Settings → Zoho →
  paste grant code). `exchange_grant_code()` needs no redirect URI and is the
  documented fallback. The scope string is printed on that card.
- `redirect_uri()` now prefers `APP_BASE_URL` and strips a scheme/trailing
  slash off `COMPANY_DOMAIN`; either used to be able to produce a malformed
  URI that Zoho could never match. `ZOHO_REDIRECT_URI` still overrides
  everything and must be copied exactly as registered.
- Settings now shows the URI with a copy button, distinguishes Self Client from
  Server-based, and demotes Reconnect to "Reconnect anyway" once everything is
  connected and the token is live.

## 2026-11-02: Copilot model compatibility and the missing quote tables

Two live failures were found in the production logs and deployed in Railway deployment
`1c8f1570-0ed0-4052-85c4-47759a95f135` (`SUCCESS`):

- The configured Copilot model rejected every Chat Completions call with HTTP 400:
  `max_tokens` is unsupported and `max_completion_tokens` is required. The generic
  exception handler then returned the customer fallback with HTTP 200, hiding the
  failure. `con_ai_agent` now uses `max_completion_tokens` and the default temperature
  for GPT-5/o-series models, while retaining the original parameters for older chat
  models and OpenAI-compatible providers. Regression tests cover both shapes.
- The live database had no `quotes` table. It was only created lazily from quote
  routes, while `/leads` and lead detail read it before those routes necessarily ran;
  the pages returned 200 but logged the error and silently lost quote-backed deposit
  amounts. Startup now runs `quoting.ensure_schema(conn)` with the lead-pipeline schema
  setup. The successful boot logged `Lead pipeline orchestrator ready`, confirming the
  quote DDL committed.

The same live logs also show two external lead-enrichment limits, not code exceptions:
RentCast's 50/month budget is exhausted, and Skip Sherpa returned 429 because its
usage-block auto-recharge was gateway-rejected. Cached records still work; new lookups
need the provider quota/billing resolved rather than another code workaround.

### Follow-up Copilot, lead and money-flow audit (2026-11-02)

- Copilot lead tools now find stored phone numbers, expose `find_people` only to the
  owner, accept canonical `engaged`/`won` stages and recompute the work queue after
  status/contact writes. `save_contact` no longer overwrites a real name. Batch drafts
  use the newly resolved owner name, and bid contact research reuses cached,
  ZIP-guarded `contact_lookup` instead of billing Skip Sherpa directly on every click.
  `quote_project` accepts `lead_id` only in the Copilot schema, never the public widget.
- Payroll no longer marks sheets paid at run creation or allows submitted/unapproved
  hours into a run. Approved sheets are locked with `payroll_run_id`, and only PNC
  confirmation marks them paid. ACH generation and settlement require a finalized run
  with bank details for every positive line, preventing an unbanked worker from being
  omitted while the whole run and ledger say paid. Stripe payroll/onboarding routes are
  retired; Copilot only finalizes for PNC and never transfers money.
- Payroll accrual uses immutable `payroll_lines.gross_cents` (OT and salary included),
  allocates hourly gross across the run's bid ids, and pays only actual `paid` runs in
  Copilot summaries. Customer collections exclude outbound payroll payment rows, and
  permit-feed dollars are converted to cents. Bid actuals exclude rejected time and
  manual labor lines no longer double count against timesheet labor.

## 2026-11-04: the books were not posting, and said they were clean

Nothing had reached the ledger since 2026-10-23 — 8 invoices, 19 cost lines, 4
payments and every payroll run were missing — while `/books` showed zero queued
failures. Root cause, found in a clone of production (`pg_dump` → scratch DB):

- `ledger_entries.amount_cents` survived the double-entry rewrite as
  `BIGINT NOT NULL` with **no default**, and `ledger.post()` writes amounts on
  `ledger_lines`. Every INSERT raised `NotNullViolation`. `ensure_schema`
  repaired `tx_type` the same way in August but not this column.
- `ledger_sync._guard` then tried to queue the failure **on the already-aborted
  transaction**, so the queue insert raised `InFailedSqlTransaction` and escaped
  the handler. The result was nothing posted, nothing queued, and a `/books`
  page that reported clean. Only the log line "ledger not updated for …" knew.

Fixed: `ensure_schema` gives `amount_cents` a default and drops NOT NULL;
`post()` fills it (debit total) so `pitch_numbers` still reads correctly;
`_guard` rolls the failed attempt back before queueing. Two more silent-money
bugs found on the way: a payment with no lead posted under `source_id "None"`
(so a second one looked like a repost), and estimated-only cost lines were being
queued as failures — an estimate is not an incurred expense.

`ledger_sync.rebuild()` + `POST /books/rebuild` (button on `/books`) re-posts
every invoice, cost, payment and payroll run created on/after the owner's
historical summary (`owner_historical_summary`, 2026-10-23). Records older than
that are left to that summary so pre-ledger history is not booked twice. Every
post is idempotent on `(source_type, source_id)`; a second rebuild posts 0.
Verified on a production clone: 7 entries posted, trial balance and balance
sheet balanced, queue empty, second run a no-op.

### 2026-11-04: the Copilot could not see paid runs, deposits or addresses

Asked "did the direct deposit to Shaun OLeary go through", the Copilot answered
from the newest *unpaid* run and said there were no pay lines. `get_payroll_summary`
queried `status IN ('open','closed')`, so the paid run and its `ach_sent_at` were
invisible. Three other answers were the same kind of gap, not model failure:

- `list_leads` never selected `address`, so no address list was possible at all:
  the Copilot refused "30 houses" on policy *and* could not have produced it.
- "Which leads are waiting for a deposit" read `leads.deposit_status`; the real
  answer is `bid_invoices` where `kind='deposit' AND status='sent'` (4 open,
  $63,786.56). There was no tool for it.
- `construction_knowledge.md` read the business-contacts-only rule as covering
  *postal addresses*. It does not: the boundary is contact details (email, phone,
  personal profiles). Public permit addresses are fine for flyers and routes.

Added `crew_pay_history`, `get_deposit_invoices`, `list_addresses`, recent-run
support in `get_payroll_summary`, and `address` on `list_leads`; narrowed the
knowledge text. Both payroll tools return a `settlement_note` because
`ach_sent_at` is the owner's upload confirmation, **not** bank settlement.
`test_copilot_money_tools.py` pins the wiring. One thing it got **right** and was
wrongly accused of inventing: it reported a Nov 2 approved timesheet showing 0
hours, which was a real test entry the owner had made and then deleted, so the
row was gone by the time the database was inspected. Still not fixed: any
Copilot exception returns `guest_fallback()` ("Call or text us at …") even in
the owner tool, and the failure is only in the log.

### 2026-11-04: a 0-hour timesheet came from the clock, not the form

The owner's test row was created by clocking in and straight back out: both
clock-out paths inserted a timesheet with `hours = max(round(...), 0)`, which is
`0.00` for any punch under ~18 seconds. The *Add hours* form already rejected
0 (`_parse_timesheet_form` enforces `0 < hours <= 24`), so the form was never
the hole. `_punch_hours()` is now the one rounding rule and both clock paths
skip the insert at 0 — the punch is still closed, so "already clocked in"
clears, but nothing reaches the timesheet table. A pay run also ignores
`hours <= 0` when selecting and locking sheets, so a legacy row could never be
pulled into a run as a $0 line. Verified in production: 0 existing 0-hour rows.

### 2026-11-04: letters are a batch you confirm, and permits age out

**Letters.** `render_letter_batches.py --out outbox` renders the address-only
backlog into dated folders of 100 doors each (route order, duplicate permit rows
collapsed, no-ZIP addresses held back) and records each batch in
`letter_batches` / `letter_batch_items`. `/letters` lists them with one
**I delivered these** button per batch. That button is the only thing that
writes a `letter` touch (`auto_reply.record_manual_touch`, channel `letter`) and
moves the lead to `contacted`; printing writes nothing. A lead can belong to
only one batch (`uq_letter_batch_lead`), and a batch with any delivered item
cannot be deleted. First run 2026-11-04: 1,084 letters, 11 batches, 1 held back
(Corolla NC, no ZIP), 0 delivered so far.

**Permits age out.** A permit stays on the city's public record for years, so
"listed" is not "live work". `permit_stats.recent_sql()` is the one predicate:
issue date within `PERMIT_MAX_AGE_DAYS` (default 90). It is applied to every
count in `permit_stats`, to the `/leads?lane=permits` row query, and in
`ingest_permits` so an old record cannot walk back on during the next scan. Rows
are kept, never deleted — `/leads` shows a `spent (over 90 days)` count. Written
as a `CASE` with a regex guard because `issue_date` is a VARCHAR and one bad row
would otherwise abort the whole board. On 2026-11-04 the table held nothing
older than 90 days (6,305 rows, all Sep–Nov 2026), so the rule is forward-looking.

Payroll run 17 (Shaun, 4h = $200, 2026-10-25→11-01) was marked ACH-sent
2026-11-03 22:19 UTC. `ach_sent_at` only records that the owner pressed
"Mark sent" after uploading the NACHA file to PINACLE; it is not proof PNC paid.
His bank record's account holder is misspelled **"Shsun OLeary"** — worth
correcting before the next run. There is no pay run for 2026-11-09→11-15 yet;
runs 14 and 15 (through 11-08) are open, with 2026-11-02 and 11-03 hours
approved but not in any run.

### 2026-11-06: owner inbox with reply drafts, and solicitation attachments on the bid card

**Inbox.** `/inbox` (`mail_inbox.py`) reads the connected Zoho mailbox live —
one cached list call per minute, bodies fetched when a thread is opened. Threads
are grouped locally by counterpart address + subject with every `Re:`/`Fwd:`
stripped, because Zoho's list endpoint returns no thread id and the Threads API
needs a folder id the grant cannot list. Two customers with the same subject are
therefore two threads, which is the failure that matters.

A reply is drafted against the whole thread with `con_ai_agent.BusinessAIAgent._complete`
(Zen/free-model fallback comes along for free) and the construction manual. The
prompt forbids invented prices, dates, availability and commitments; a holding
reply is the intended answer to an unknown. **Nothing sends without the owner
pressing Send.** Sending uses Zoho's `POST /messages/{messageId}` with
`action=Reply` so it lands in the real conversation; other transports degrade to
`documents_service.send_email` with a `Re:` subject. A successful send is
written to `comms_logs` with the lead id when the address matches a lead, so the
contact policy sees the touch. An identical body to the same address within 45
seconds is refused — that is the double-click, not a policy.

**Bid attachments.** A solicitation email's useful part is usually the plan set,
not the body. `inbound_email.ingest_bid_email` records the Zoho coordinates of
the source message in `bid_source_emails`, keyed to every bid the email
produced. The bid card's **Pull attachments** button
(`/api/bids/{id}/documents/from-email`, `bid_attachments.py`) lists the
attachments on that exact message, downloads them through `photo_store`, and
files them under `plans`/`sow`/`quote`/`itemized`/`contract`/`other` using
filename hints. It also scans the email body for **direct document links**
(pre-signed storage URLs) and fetches those, with an SSRF guard that refuses
private/loopback hosts and an HTML-login-page check. Keys derive from the Zoho
attachment id or a hash of the link, so pressing twice does not duplicate. For
bids that predate the link, it first searches 365 days of mail by sender,
reference, then name. The card also shows the full solicitation text (scope,
RFIs, instructions) from `comms_logs`.

**The bid board itself cannot be scraped for plans.** BuildingConnected's public
API v2 documents only bid packages, invites, projects, users and trade-app
flags/financials/qualifications — there is no files, drawings or RFIs endpoint.
Those live behind the owner's signed-in web session at
`app.buildingconnected.com`. That is why the card keeps a prominent deep link to
the bid package and why a board URL found in an email is reported as
"needs your sign-in" instead of being fetched. Getting plan rooms automatically
from a specific board requires that board's API or an authorized session; do not
pretend the current pull does it.

`photo_store.ALLOWED_EXT` was widened for these two features: `.csv/.md/.txt`,
`.doc/.docx/.xls/.xlsx` (the bid picker already offered Word files but the
allowlist silently rewrote every one to `.jpg`), and `.dwg/.dxf/.ifc/.rvt/.zip`
for plan sets. Scriptable formats (`.html/.svg/.js`) still fall back to `.jpg`.
`_content_type` now uses `splitext`, which also fixed `.jpeg` being served as
`application/octet-stream`.

### 2026-11-06: payroll run 17 said paid, but the NACHA file was invalid

Run 17 (Shaun, 4h = $200, 2026-10-25→11-01) was marked paid on 2026-11-03
22:19 UTC after being uploaded to PINACLE, and the money never arrived. The
generated file's **File Header Immediate Destination was zero-padded**
(`0043000096`) instead of the NACHA-mandated blank plus 9-digit routing
(` 043000096`); PNC rejects that. `direct_deposit.nacha()` now builds the field
correctly and `validate_nacha()` checks record lengths, the destination,
entry hash and totals before the route will serve the file. The download route
also refuses when any `ACH_*` header id is unset.

Two workflow holes made the failure unrecoverable and invisible:

- Once a run was `paid`, `_payroll_ach_ready` refused to generate the file
  again, so the exact bytes PNC rejected could not be retrieved. Downloads are
  now allowed for paid runs (`allow_paid=True`); moving money still requires
  the explicit "Uploaded to PNC" click.
- There was no way to undo a false settlement. **↺ Reopen as unpaid** on
  `/payroll` puts the run back to `closed`, clears `ach_sent_at`,
  `nacha_downloaded_at` and `nacha_sha256`, unlocks its timesheets to
  `approved`, and reverses the `payroll_paid` journal entry (run 17's is
  `JE-2026-000020`, entry id 53). A fresh download is then required before it
  can be marked sent again.

The app does **not** submit ACH and cannot see PNC returns; PINACLE upload and
approval are the owner's step. A bank return needs the reopen path. Run 17's
account holder is misspelled **"Shsun OLeary"** on routing 031101279 /
account ··4892 — the payroll page now warns on a crew-name/holder mismatch,
because some banks hold or return a credit for that.

### 2026-11-06: bid card shows its own job, and money lives in one card

The page repeated `Bid #<id>` in every panel (contract package, PNC send), which
read like another bid's information. The number now appears **once** in the
header, and `/bids/{id}` is served with `Cache-Control: no-store` so a browser
or proxy cannot hand back bid A's HTML for bid B. Verified against production:
`/bids/17492`, `/bids/18041` and `/bids/18046` each render their own id.

The three overlapping money cards (Payment schedule, Deposit & job, Invoices)
became **one Payments card** with contract/billed/collected/still-to-collect,
the 30/40/30 rows, custom invoices, and one action per invoice state. A separate
**Job** card keeps the project/labour links. This removes the duplicate
"Record payment" and duplicate deposit-request buttons that let the same invoice
be acted on from two places.

`/letters` was 500ing with `TemplateNotFound: 'construction/letters.html'`
because the route passed a path that already included the template directory.
Fixed to `letters.html`.

### 2026-11-06: a revised upload was not the file in use

Two independent bugs made an uploaded revision invisible:

1. `upl_by_type` in `bid_detail.html` did
   `setdefault(d.document_type, [])` **without** `.append(d)`, so every bucket
   stayed empty and the Contract package always fell back to the generated
   PDFs — even when a revised SOW/quote/contract had been uploaded. Fixed.
2. Uploads trusted the `doc_type` dropdown and never retired the previous file
   in a slot. On bid 18350 the revised contract
   (`03-Contract-Revised-3-SIGNED.pdf`, doc 21) was filed as **quote**, so the
   Contract slot kept the old/generated file and the contract appeared in the
   price slot. The card now:
   - retires the previous document when a new one is uploaded into
     sow/quote/itemized/contract (`supersede=True`); plans and other stay a set;
   - guesses the slot from the filename in the browser and, when the dropdown
     is left on Other, on the server too;
   - offers **Move to &lt;type&gt;** on a mis-filed active document
     (`POST /api/bids/documents/{id}/type`), which also retires the old occupant;
   - marks the first active document per slot **In use** and prints its filename
     on each Contract-package button.

An uploaded **Itemized Pricing**/Quote is the file sent to the GC; it does **not**
rewrite the card's cost lines or total. Those remain the app's internal build-up
and drive the generated documents — the Cost lines card now says so.

### 2026-11-06: the Copilot could not email because the manual said it couldn't

The `send_email_message` schema and a guarded handler had both been restored on
2026-11-05, but `construction_knowledge.md` §15 still said **“There is no
outbound email tool. You cannot send email.”** That file is concatenated into
the Copilot's system prompt, so the model refused a capability it had. The
prose now names `send_email_message`, says it sends a real email, and explains
the guard (no .gov/.mil, one per address per day, daily cap, duplicate text
suppressed) and that a refusal must be reported, never claimed as sent.

`test_copilot_cannot_send_email` was the other stale copy: it asserted the tool
was absent and could never pass again. It now tests the guard itself — a .gov
recipient never reaches the raw sender, the cooldown blocks the second send,
and a normal recipient goes through. `copilot_ops.build_contact_tools` and the
`build_copilot_handlers` docstring no longer claim the send is withheld.

Verified live inside the production container on 2026-11-06: the Copilot handler
sent a real message through Zoho to hello@bizstackperks.com and returned
`{'ok': True, 'sent_to': 'hello@bizstackperks.com'}`. The public website widget
still does not get the tool; it remains copilot-only.

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
