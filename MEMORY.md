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

## Open threads

- `ATTIC_API_KEY` / `SERVICEKANI_API_KEY` / `REGRID_API_KEY` / `BATCHLEADS_API_KEY`
  are all unset, and `lead_research.py` in the **hosts** repo is the thing to
  point a key at — not the PDL path in that same repo. It runs live and returns
  `0 enriched, 10 unresolved` on every pass purely because skip-trace is inert.
  It is the only code that produces a homeowner name/phone.
- SAM.gov is off (`LEAD_SOURCES_SAM_GOV=0`) and the operator does not want it.
  The federal block in `blocked_reason` is still armed and deliberate.
- `mail_letters.py` is offline by design: it never writes `comms_logs`. Marking
  a letter as mailed is deliberate, via `record_manual_touch` (the four buttons
  on each lead card) or `lob_send.py` doing it automatically.
