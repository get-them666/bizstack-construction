# The lead pipeline, as it actually runs

This file used to describe an "event-driven cascade" that auto-enriched, auto-texted,
auto-quoted and auto-created projects. That code existed, but it was the wrong shape for
this business and it was actively harmful: a permit row with no homeowner contact could be
sent a cheerful SMS and quoted at a made-up midpoint, with nobody reviewing either. The
cascade is gone. This is what replaced it.

## The one lifecycle

`bizstack_leads/lifecycle.py` defines the stages and the next action:

| Stage | Meaning |
|---|---|
| `new` | arrived, nothing sent |
| `contacted` | at least one outbound touch, no reply |
| `engaged` | they replied; the ball is in our court |
| `quoted` | a price was sent |
| `won` | converted |
| `lost` | closed, or asked us to stop |

Legacy statuses (`deposit`, `in_progress`, `completed`, `analyzed`, …) translate on read.
Nothing else may write `next_action`, `next_action_at`, `last_outbound_at`, `touch_count`.
Only `bizstack_leads/store.recompute` writes them, so the queue cannot drift from the rules.

## Where the owner works

- **`/pipeline`** — today: the 12 people to call (replies first, then follow-ups, then
  first touches), the canonical funnel, permit-door supply, and open pipeline value.
- **`/leads`** — the full work queue, grouped by next action, plus the permits lane.
  `?action=first_touch|follow_up|respond|enrich`, `?status=<stage>`, `?lane=permits`.
- **`/leads/{id}`** — one lead: activity, notes, draft, deposit, stage.
- **`/bids`** — solicitations. Real pull data and owner-entered bids only.
- **`/job-leads`** — permit rows, including the ones that have not become leads.

## What is automatic

Only these, and each is capped or review-gated:

| Job | Code | Guard |
|---|---|---|
| Permit import | `run_permit_import` → `ingest_permits` | `PERMIT_SCAN_HOURS`, live open-data feeds only |
| Contact enrichment | `lead_enrich_loop` | `LEAD_ENRICH_MAX` (25/day), one Skip Sherpa credit per address |
| Lead-source scan | `lead_sources.scan` | Scrap.io only; no federal sources |
| Inbound email poll | `_gmail_inbound_loop` | 900s |
| Loan outreach | `loan_outreach` | hourly, own caps |

What is **not** automatic any more:

- Creating a lead does not enrich it, text it, or quote it. `lead_pipeline.on_lead_created`
  only records the event and recomputes the queue state.
- `on_lead_enriched`, `on_lead_contacted` and `on_lead_quoted` are inert. They remain so
  old callers do not break.
- `on_quote_accepted` and `on_bid_won` still create projects. Those follow a human
  accepting money, not a guess.

## Contact supply

1. `skiptrace_service` reads the public parcel record: owner **name**, free, cached.
2. `skip_sherpa_service` returns phone/email for one address. It is billable, so the batch
   button is capped at 10 and the daily loop at `LEAD_ENRICH_MAX`.
3. `data_enrichment.lookup_address` runs them in cost order, caches misses, and writes back
   only non-empty fields.
4. `data_enrichment.merge` then `store.recompute` turns a gained phone into a first-touch
   action immediately. A name-only result does **not** count as a hit.

RentCast is not the contact provider. Its 50-call monthly budget is exhausted and it never
had phone/email anyway.

## Permits are supply, not a call list

`job_leads` holds real residential permits from the Virginia Beach and Norfolk open-data
feeds. Most have no homeowner phone. They appear on `/pipeline` as **doors**, and on
`/leads?lane=permits` for mailing/door-knocking. The importer now creates **one lead per
normalized address**, not one per permit, so a house with four trade permits is one door.

There is no demo fallback. When no source is configured or reachable, the importer returns
nothing and says so.

## What was removed and why

- `permit_service.DEMO` / `_seed_demo`: five fabricated permits, one at a real address.
- The fabricated `Month 1 … Month 4` bids and the ten projects created from them: these
  populated the "won" column with work that never happened.
- LinkedIn/Reddit radar rows with no address, phone, email or communication history: names
  scraped from a post, not customers. Backed up before deletion by
  `tools/purge_mock_data.py`.
