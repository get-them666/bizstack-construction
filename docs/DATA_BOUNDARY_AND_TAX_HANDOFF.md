# BuildStack data boundary and tax handoff

BuildStack Construction is its own application and operational data boundary. It must not read or write Broom Service leads, customers, host/property records, workers, messages, credentials, calendar entries, or marketing audiences. A common Zoho login is not permission to mix records.

## Integration separation

- Use the BuildStack deployment/database and a BuildStack-specific Zoho CRM pipeline, Campaigns list, Books organization, and Calendar where the Zoho products support those boundaries.
- Keep provider tokens, webhook secrets, callback URLs, sending identities, and contact preferences configured per deployment. Do not copy a refresh token or audience between sites just because the UI is similar.
- Calendar events belong to the business that booked them. Construction walkthroughs/projects go to the BuildStack calendar; Broom turnovers and reservations remain in Broom’s calendar.
- Do not route Broom requests into construction services or vice versa. A user must explicitly request a referral before sharing any lead information across businesses.

## Tax-only handoff

Share only an accountant-approved tax export, not a live database connection or general Zoho access. The export should contain the entity/tax-year, transaction date, amount, tax category, payment method, vendor/payee if needed for tax reporting, and a stable transaction reference. Exclude customer marketing lists, personal contact enrichment, messages, calendar details, credentials, and unrelated operational notes. Review business-unit allocation and remove duplicates/transfers before submitting.

An accountant must confirm the allowed fields, legal entity mapping, and retention. Never treat a summary or automated category as tax advice. Preserve source books and an audit trail; corrections should be traceable rather than overwriting evidence.

## Migration guardrail

Do not point either application at the other’s production database. Before any isolation/cutover, take verified backups, inventory tables and integrations, map records to the correct company, rehearse a one-way migration into separate databases, compare counts and sample workflows, and retain a rollback plan. No production migration is performed by this document.
