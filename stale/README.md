# stale/ — superseded loan material

**Nothing here may be sent to a lender.** These are earlier builds kept for the record.

## LOAN_PACKAGE.SUPERSEDED-2026-10-06.pdf

Built 2026-10-06 by `build_loan_package.py`, before the 2026-10-10 corrections. It contains:

| Wrong figure | Corrected to | Why |
|---|---|---|
| `$1,000` owner contribution (×2) | **removed entirely** | Never reconciled against a bank record. `MEMORY.md` records no owner deposit in the PNC statement at all — both deposits are customer revenue. |
| `$930/month` payment (×4) | **$967/month** | `$930` matched no real amortization. See below. |
| "owner contribution" section | **§6 "Owner fit"** | Section rewritten to carry the bank-documented revenue argument instead. |

### Why $967 is right

The original `$930` was internally inconsistent — it did not correspond to any amortization of
$50,000 at 6%. VSBFA's published Microloan terms are **6% fixed** (3% Veterans), **5 years
unsecured / 7 years secured**:

| Term | Payment | DSCR |
|---|---|---|
| 5 yr unsecured | **$967/mo** | 1.29x |
| 7 yr secured | $730/mo | 1.71x |

So the *old* pitch's `$930` was wrong, and the `$829` briefly written in its place was also
wrong — that assumed a 6-year term the program does not offer. `$967` is correct.

### Regenerating

`build_loan_package.py` now **computes** the payment from `LOAN_RATE` / `LOAN_SECURED`
rather than hardcoding it, so these figures cannot silently drift out of sync again.

```bash
venv/bin/python build_loan_package.py --check                      # verify inputs
venv/bin/python build_loan_package.py --out send_today/LOAN_PACKAGE.pdf
```

Change the payment by editing `LOAN_RATE` and `LOAN_SECURED` near the top of the builder,
then rebuild. Confirm the live terms with Karen T. White before filing.

## Git

`LOAN_PACKAGE.pdf` was moved here with `git mv`, so its history is intact. The original
2026-10-06 bytes are recoverable at any time:

```bash
git show 1a42bcc:LOAN_PACKAGE.pdf > recovered.pdf
```