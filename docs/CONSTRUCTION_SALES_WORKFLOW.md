# BuildStack Construction: bid and lead sales workflow

This playbook is for the BuildStack Construction site only. It does not import Broom Service leads, calendars, or marketing contacts.

## Daily owner queue

1. Start at `/pipeline` and work the named, reachable leads in **reply / follow-up / first-touch** order. A permit record is an address and project signal, not proof that its owner asked to be marketed to.
2. Open `/leads?lane=permits` to review current, residential, workable permit doors. Do not treat demo, stale, commercial, or unclassified records as ready client leads.
3. For each promising permit, use free public parcel/owner lookup first. Paid contact lookup is a deliberate, per-address action; verify the match and DNC status before saving. Save a reachable contact to the lead queue only after human review.
4. Use the site’s staged draft and send controls for a one-to-one response to a person who contacted the business or otherwise has an appropriate, documented basis for contact. Never bulk-email scraped homeowner records.
5. Put a real walkthrough on the BuildStack calendar after the prospect agrees. Do not create a calendar event just because a permit or bid was imported.

## Bids: qualify, price, submit, follow through

1. Import from BuildingConnected, a connected feed, solicitation email, webhook, or paste. Check the pull history and preserve the original source link/email.
2. Triage before estimating: trade fit, service area, submission deadline, pre-bid/site-walk date, insurance/licence requirements, scope completeness, addenda/RFI dates, and contact confidence. Missing critical details are **unknown**, never guessed.
3. Research the actual GC/buyer contact using the bid’s source material. Keep bid contacts separate from homeowner leads. Never send a bid inquiry to a federal/government contact unless the opportunity and outreach path are explicitly approved.
4. Pull the plan/spec package and build takeoff lines. Price labor, materials, subs, equipment, waste, overhead, and markup from current, attributable observations. Flag estimates and stale supplier prices; do not represent a ballpark as a firm bid.
5. Review the scope, assumptions, exclusions, due date/time zone, bid amount, and documents. Save a revision, then use the explicit submit action. Record confirmation and follow-up date.
6. Track `new → drafting → sent → shortlist → won/lost/withdrawn`; an external refresh must not reset an owner’s status. Set reminders for unanswered submissions and addenda, but require owner review before sending.
7. On a win, use the existing project conversion, contract/signature, deposit, invoicing, calendar, crew, and job-costing workflow. Reconcile actual outcome and margin when the bid closes.

## Useful board improvements to prioritize

- A single “next action” card: fit score with explainable evidence, data freshness, unknown/missing fields, deadline countdown in local time, and source link.
- Saved filters for due soon, needs qualification, missing contact, waiting on answer, and ready to submit.
- Per-bid activity timeline (import, owner edits, contacts, calendar walkthrough, RFIs/addenda, submission confirmation, outcome) with idempotent imports.
- Explicit next-action date and owner; overdue work is surfaced rather than silently disappearing.
- Submission checklist and validation; block a “ready” label when required fields are missing, while allowing a human to override with a recorded reason.
- Source and outcome reporting: qualified → priced → submitted → shortlisted → won, win rate and margin based on actual outcomes, not feed-estimated value.

Do not change a bid’s status on import, fabricate a contact, infer a win from a feed, or count an assumed estimate as secured pipeline.

## No-silent-failure expectations

Every source sync should visibly report last attempt, success/failure, created/updated/rejected counts, and a short actionable error. A failed provider call must not be logged as a send or success. Preserve source payloads where appropriate, use retries only for safe/idempotent operations, and surface retries/partial failures in the UI and logs. Verify `/health`, tests, and both lead and bid workflows after deployment.
