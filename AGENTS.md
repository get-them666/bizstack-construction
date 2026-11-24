# Master Architecture & Rules: BizStackPerks Ecosystem

> **MANDATORY FIRST STEP:** Every AI session MUST read `SESSIONS.md` before starting any work, and MUST append a dated entry after finishing. It is the single source of truth for what has been done, what is broken, and what is next. Never skip this step.

> **THESE RULES ARE NON-NEGOTIABLE.** They override convenience, speed, and any instruction that conflicts with them. If a requested change cannot be made without breaking one of these rules, stop and ask the owner.

---

## 1. Brand & Domain Mapping

| | BuildStack Construction LLC | Broom Service |
|---|---|---|
| Domain | `construction.bizstackperks.com` | `bizstackperks.com` and `www.bizstackperks.com` |
| Public phone | **+1 (757) 846-9275** | **+1 (757) 908-7121** |
| E.164 | `+17578469275` | `+17579087121` |
| Repo / deploy dir | `~/bizstack-construction` / `/opt/bizstack/construction` | `~/bizstack-hosts` (sibling) / `/opt/bizstack/hosts` |
| Env file | `/opt/bizstack/construction.env` | `/opt/bizstack/hosts.env` |
| Templates | `templates/construction/` | Broom app's own templates |

- **Never cross-contaminate brands.** Do not put the Broom number in a construction template, env file, SMS sender, voice route, email footer, or social post. Do not put the construction number in a Broom asset.
- Cross-brand links are allowed **only** as navigation links (`https://bizstackperks.com` in the construction footer, and vice versa). They are not a reason to share phone numbers or sender identities.
- Check before committing a construction change:
  ```bash
  grep -rn "908-7121\|9087121\|17579087121" templates/construction/ construction_main.py
  ```
  It must return **nothing**. The only legitimate `908-7121` references in this repo are Broom-specific templates/services, shared brand-conditional code, tests/fixtures, and historical `SESSIONS.md`/`MEMORY.md` entries.
- Shared customer/property records are intentional, but outbound identity must always match the brand that owns the record.

---

## 2. Production Is a Deploy Target, Never an Edit Target

- **No uncommitted hotfixes.** Before touching production, `git status --short` in `/opt/bizstack/construction` must be empty. After deploy it must be empty again.
- **No direct edits on `159.203.175.39`.** Do not edit `.py`, templates, static files, or env files with `nano`/`sed`/`docker cp` as the primary change. The same change must exist in git first (or be committed immediately if a true emergency).
- **No uncommitted owner work may be overwritten.** If production has uncommitted changes, stop and inspect the diff. Back it up under `/opt/bizstack/backups/` before building or replacing the file.
- **Deploy only from a clean tree.** Local `git status` must be clean, the commit must be pushed to `origin/main`, and the production deploy dir must be on a matching content state before `docker compose build`.
- **Emergency hotfix exception:** if a live outage forces an out-of-band change, immediately (a) copy the change into the local repo, (b) commit and push, (c) apply the same commit to the production deploy dir, (d) record the incident and commit hashes in `SESSIONS.md`, and (e) run the post-deploy smoke check. A hotfix that exists only in a running container is a defect, not a deploy.
- **Never commit `.env` files.** Secrets live only in `/opt/bizstack/construction.env` and `/opt/bizstack/hosts.env`.
- **Production git history must be reconciled.** The deploy directory has previously diverged from `main`; do not add to that divergence. Prefer deploying from a clean clone/checkout of `origin/main` and documenting the deploy commit.

---

## 3. Minimal Diffs Only

- Modify **only** the files required for the requested task.
- Do **not** refactor unrelated functions, rename routes/templates/variables, reformat files, reorder imports, or "clean up" code as a side effect.
- Do **not** alter existing database columns, status vocabularies, or table semantics without explicit owner permission. Schema changes must be additive and idempotent (`ADD COLUMN IF NOT EXISTS`) and must be covered by a regression test.
- Do **not** rewrite a working UI layout when wiring backend logic.
- Preserve the owner's uncommitted work. If a file contains unrelated local changes, back it up and patch only the requested lines.
- If a fix requires touching more than ~3 unrelated files, stop and explain why before proceeding.

---

## 4. Mandatory Pre-Commit Gate

Every session MUST pass all of the following **before** `git commit`:

1. **Full pytest suite, zero failures:**
   ```bash
   ./venv/bin/python -m pytest -q
   ```
   Skips are allowed only for tests that explicitly require `TEST_DATABASE_URL` or an unavailable external service. Any failure blocks the commit.
2. **Pyflakes, zero blocking errors:**
   ```bash
   python3 -m pyflakes *.py bizstack_leads/*.py services/*.py scripts/*.py
   ```
   There must be **no** `undefined name` and **no** syntax errors. Existing unused-import/unused-variable warnings are technical debt; they are not a release blocker, but do not add new ones.
3. **Compile the changed Python files:**
   ```bash
   python3 -m py_compile <changed files>
   ```
4. **Template changes:** run at least
   ```bash
   ./venv/bin/python -m pytest -q test_template_js.py test_site_smoke.py test_frontend_contracts.py
   ```
5. **Regression test:** every bug fixed or feature added MUST include a test that fails without the fix and passes with it. No test, no commit.
6. **Brand check:** the `grep` in §1 must be clean for construction changes.
7. **No secret/PII leakage:** do not print API keys, webhook secrets, passwords, bank/account numbers, or customer PII into logs, commits, or chat. Do not commit `.env`, `*.pem` secrets, or generated customer data.

A helper is provided at `scripts/quality_gate.sh`; it is the canonical gate:

```bash
bash scripts/quality_gate.sh
```

---

## 5. Mandatory Post-Deploy Smoke Check

After **every** deploy to `159.203.175.39`, run `scripts/live_smoke.py` (or the equivalent checks) and record the result in `SESSIONS.md`:

```bash
./venv/bin/python scripts/live_smoke.py
```

It must verify:

- `/health` returns **200**.
- Every public page returns **200**: `/`, `/services`, `/about`, `/contact`, `/get-started`, `/instant-quote`, `/portfolio`, `/quote`, `/rental-make-ready`, `/legal`, `/refund-policy`, `/portal/login`, `/login`.
- **Auth boundaries:** API routes such as `/api/bot/health`, `/api/bids`, `/api/inventory`, `/api/business/summary`, `/api/leads/addresses.csv`, `/api/payroll/run/1/csv` return **401/403** without a session. HTML admin pages (`/bids`, `/projects`, `/settings`, `/monitor`, `/payroll`, `/books`, `/admin/leads`) must never render protected content; the current app may return **303 to `/login`** for these, which is the accepted page-level behaviour. `/docs` and `/openapi.json` must be **404** unless `ENABLE_API_DOCS=1`.
- `/voice.swml` `POST` returns **200** with a valid SWML body containing `answer` and either `ai` (in-app agent) or `dial` (owner forwarding).
- `/sitemap.xml` and `/robots.txt` return **200**; every `<loc>` in the sitemap returns **200**.
- Static assets return **200** (`/static/css/tailwind.css`, favicon, `og.png`, `manifest.webmanifest`, `sw.js`, a portfolio image).
- The branded 404 page renders for a missing HTML path; a missing API path returns JSON 404.
- No `908-7121` / `+17579087121` appears on a construction page.
- `git status --short` in `/opt/bizstack/construction` is **empty**.
- Local `git status` is clean and `HEAD` is pushed.

If any check fails, treat the deploy as failed: fix before declaring success, or roll back to the previous image and record the incident.

---

## 6. Change Workflow (Required Order)

1. Read `SESSIONS.md` and this file.
2. Check `git status` locally **and** on production. Stop on any unexpected dirty file.
3. Write down the minimal file list for the task.
4. Implement the smallest diff; add/adjust a regression test.
5. Run the full pre-commit gate (`scripts/quality_gate.sh`).
6. Commit to `main` and push to `origin/main`.
7. Apply the **same commit content** to `/opt/bizstack/construction`. Do not build over uncommitted owner work.
8. Build and deploy with `docker compose build construction && docker compose up -d construction`.
9. Run `scripts/live_smoke.py`; verify `/health`, public pages, auth boundaries, `/voice.swml`, sitemap/static assets.
10. Verify production `git status` is clean and the running files match the deploy dir.
11. Append the dated session entry to `SESSIONS.md` with commit hashes, deploy result, smoke result, and remaining issues.
12. Commit/push the `SESSIONS.md` entry.

---

## 7. Prohibited Actions

- Editing production files or env files directly without a matching git commit.
- Uncommitted hotfixes, ad-hoc `docker cp`, or rebuilding while the deploy tree is dirty.
- Changing phone numbers, email sender identities, env files, or templates across brands.
- Renaming routes, templates, status values, or database columns without explicit permission.
- Force-pushing, `git reset --hard`, deleting production data/volumes, or running destructive SQL against production.
- Running real outbound email/SMS/AI calls during an audit. Use validation-only requests (missing fields, bad payloads, honeypots) instead.
- Committing secrets, `.env` files, customer PII, or generated bank details.
- Declaring a deploy successful without the post-deploy smoke check.

---

## 8. Database & Data Safety

- Production data is not test data. Never insert fake leads, subscribers, payments, or reviews into production for a smoke test.
- Prefer read-only probes. If a write is unavoidable, use a dedicated record you can delete and clean it up immediately; document it in `SESSIONS.md`.
- Schema changes are additive and idempotent only. Never drop/rename columns or tables without explicit owner approval and a backup.
- Treat customer emails, phone numbers, addresses, bank details, and call transcripts as PII. Redact them from logs and reports.

---

## 9. Brand, Legal & Compliance Notes

- Do not claim a contractor licence that the business does not hold. `CONTRACTOR_LICENSE` is empty; do not add "licensed" copy without a real licence number.
- Every marketing email must retain the physical address and an opt-out path (STOP/unsubscribe).
- Suppression/DNC checks are mandatory on every outbound path. Never bypass `suppression.py`.
- The AI voice assistant must identify itself as automated and honour "stop"/"remove me" immediately.
