# Production is on Render, not Railway

Railway is retired. If a doc, script, or agent says "deploy to Railway", it is
wrong. `render.yaml` at the repo root is the deployment.

Verified 2026-11-13 with the Render CLI (`render services list`,
`render deploys list`, `render postgres get`, and `pg_stat_statements`).

---

## The resources

| Thing | Value |
|---|---|
| Workspace / project | `My project` (Production), owner `soleary0006@gmail.com` |
| Web service | **`bizstack-construction`** — `srv-dbo1a53m8hqs73f9humg` |
| Postgres instance | **`bizstack-construction-db`** — `dpg-dbo168bm8hqs73f97b8g-a` |
| Region (both) | **virginia** — service and database must stay in the same region |
| Runtime | Docker (`Dockerfile`, `python:3.12-slim`, `uvicorn construction_main:app`) |
| Plan | web `1c-2g`; Postgres `free` (created 2026-11-08, **expires 2026-12-08**) |
| Health check | `/health` |
| Auto-deploy | `autoDeployTrigger: commit` — **push to `main` deploys** |
| Live commit (2026-11-13) | `5b34bf4` (portal/signing/payments deploy verified) |
| Hosts web service | `bizstack-hosts` — `srv-dbps9p0ae00c73chdqs0` (separate business) |

## The database trap — this is the important part

The Postgres **instance** `bizstack-construction-db` contains several
databases:

| Database | What it is | Use it? |
|---|---|---|
| **`construction_live`** | **the live construction app database** | **YES** |
| `construction_mv91` | the datastore's default database (and what `render psql <db>` connects to) | no |
| `hosts` | the BuildStack Hosts business | **never** |
| `postgres` | maintenance | no |

Confirmed by querying `pg_stat_statements`: the `portal_clients`,
`bid_opportunities`, and ledger statements all execute in **`construction_live`**
(≈487,000 calls), while `construction_mv91` has only a few thousand. The live
app's `DATABASE_URL` therefore points at `/construction_live`.

**Therefore:**

- `render psql bizstack-construction-db` lands in **`construction_mv91`** and
  shows 995 bids but **no portal tables**. That is not the live database.
- Do not "fix" a missing table by creating it in `construction_mv91`. It will
  look wrong in the app and diverge from production.
- The blueprint (`render.yaml`) declares
  `DATABASE_URL: fromDatabase: bizstack-construction-db` — that resolves to the
  datastore default, `construction_mv91`. The live service is running with an
  explicit `DATABASE_URL` that ends in **`/construction_live`**. If a Blueprint
  sync ever overwrites that variable, the app will silently switch databases.
  Check the value in **Dashboard → bizstack-construction → Environment** before
  and after any Blueprint sync; it must end in `/construction_live`.

### How to query the live database

`render psql` has no database picker. Either:

1. **Dashboard → `bizstack-construction-db` → Connect → External Database URL**,
   then run `psql "<that URL>"`. It ends in `/construction_live`; treat it as a
   secret and do not paste it into the repo or a chat.
2. Run psql **inside the web service** with its own `DATABASE_URL` (SSH key
   required), e.g. `psql "$DATABASE_URL" -c "..."`.
3. For a read-only sanity check from the default database, use
   `pg_stat_statements` (see the query below) — never infer the live DB from
   the name `construction_mv91`.

Read-only check that the app has been writing in the construction DB:

```sql
SELECT d.datname, count(*), sum(s.calls)
FROM pg_stat_statements s
JOIN pg_database d ON d.oid = s.dbid
GROUP BY 1 ORDER BY 3 DESC;
```

## Deploying

1. Commit and push to `main` (the service auto-deploys on every commit).
2. Confirm the deploy finished:

```bash
render deploys list srv-dbo1a53m8hqs73f9humg -o text
```

The newest row should say `Live`, with the new commit SHA.

3. Watch the boot log for the schema line. New tables/columns are created by
   `lifecycle()` on every boot — there is no separate migration step:

```bash
render logs -r srv-dbo1a53m8hqs73f9humg --limit 300 -o text \
  | grep -E "bid_opportunities ready|Client portal account|Traceback"
```

`🎯 bid_opportunities ready.` means the whole startup DDL block committed,
including `portal_service.ensure_schema()` and `contract_signing.ensure_schema()`.

4. Verify the new objects in the **live** database:

```sql
SELECT to_regclass('public.portal_payments'),
       to_regclass('public.portal_payment_sessions'),
       to_regclass('public.bid_sign_requests');
```

All three must be non-null. If they are null and the log said "ready", you are
connected to the wrong database (most likely `construction_mv91`).

## Environment variables

Secrets live in the Render Dashboard (group `construction-secrets`), not in git.
`render.yaml` uses `sync: false`, so a Blueprint sync prompts for values.

Added for the client portal (2026-11-13):

| Key | Where |
|---|---|
| `ZOHO_PAYMENTS_ACCOUNT_ID` | Zoho Payments dashboard |
| `ZOHO_PAYMENTS_API_KEY` | Zoho Payments → Settings → Developer Space (public widget key) |
| `ZOHO_PAYMENTS_WEBHOOK_KEY` | the HMAC key registered for `/api/payments/zoho/webhook` |
| `ZOHO_PAYMENTS_DOMAIN` | `US` (already set in `render.yaml`) |
| `PORTAL_OTP_ENABLED` | `true` (already set; client email codes) |

After adding them, **reconnect Zoho in Settings** — `ZohoPay.payments.CREATE`
and `ZohoPay.payments.READ` were added to `zoho_oauth.SCOPES`. The old refresh
token does not carry those scopes and returns `INVALID_OAUTHSCOPE`.

Zelle and bank details are not environment variables: they are stored under
Settings → Banking (`pay_zelle_handle`, `pay_ach_*`). The client portal shows
only the Zelle handle and the last four digits of any account.

Register the Zoho Payments webhook in Zoho:

```
https://construction.bizstackperks.com/api/payments/zoho/webhook
```

## Render-specific behaviour that bites

- **Free Postgres is deleted 30 days after creation.** This instance expires
  **2026-12-08**. Upgrade the plan or take a dump before then.
- **Free web services spin down after 15 idle minutes**, which kills the
  `lifecycle()` daemon threads (permit import, lead scan, bid autopilot,
  enrichment, loan outreach). A sleeping service looks healthy and silently
  stops importing permits. The paid `1c-2g` plan avoids this.
- The filesystem is **ephemeral**. Crew photos and documents require the R2
  variables; without them every deploy deletes the files.
- Region is not cosmetic. A service and its database in different regions
  cannot use Render's private network (`failed to resolve host 'dpg-...'` while
  `/health` still returns 200).
- `render psql` connects to the datastore default, not the live database. See
  the database trap above.

## Useful CLI commands

```bash
render services list
render deploys list srv-dbo1a53m8hqs73f9humg -o text
render logs -r srv-dbo1a53m8hqs73f9humg --limit 200 -o text
render restart srv-dbo1a53m8hqs73f9humg
render postgres get bizstack-construction-db -o json   # note databaseName
```

Never run `render services delete`, `render postgres delete`, or a Blueprint
sync that rewrites `DATABASE_URL` without checking the live database name first.
