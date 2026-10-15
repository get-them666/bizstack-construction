"""Correctness tests for the double-entry ledger.

Plain pytest, collected normally. These assert the invariants that make the
module an accounting system rather than a table of numbers:

  - every entry balances, and a non-balancing one writes nothing
  - a line cannot be both debit and credit, or neither
  - money is integer cents; sub-cent precision is refused
  - the trial balance sums to zero after anything
  - assets = liabilities + equity, including earnings not yet closed
  - closing a period moves earnings to equity and changes nothing else
  - reposting a source record is a no-op
  - crew wages are accrued before they are paid

Uses a scratch database. No network, no production.
"""
import os

import pytest
from pytest import importorskip

psycopg = pytest.importorskip("psycopg")

DB = os.getenv("LEDGER_TEST_DB", "bz_ledger_test")
URL = f"postgresql://{os.getenv('USER', 'postgres')}@localhost:5432/{DB}"

ledger = pytest.importorskip("ledger")
bridge = importorskip("ledger_bridge")

BID = {"id": 7, "name": "Hilton Drywall"}
# 40h @ $38 + 32h @ $32
PAY_LINES = [{"crew": "Devon", "hours": 40, "rate": 38.00, "bid_id": 7},
             {"crew": "Sam", "hours": 32, "rate": 32.00, "bid_id": 7}]
GROSS_LINES = [{"crew": "Devon", "gross": 1520.00}, {"crew": "Sam", "gross": 1024.00}]
LABOR_CENTS = 40 * 3800 + 32 * 3200
OPENING_CASH = 2_500_000


@pytest.fixture()
def conn():
    import subprocess
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
    subprocess.run(["createdb", DB], check=True)
    c = psycopg.connect(URL, row_factory=psycopg.rows.dict_row)
    ledger.ensure_schema(c)
    yield c
    c.close()
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)


def _book_a_job(c, materials=7500.00, overhead=1200.00, deposit=500.00):
    bridge.post_invoice(c, BID, {"id": 1, "invoice_number": "INV-2026-0001",
                                 "amount": 150000.00, "issued_date": "2026-10-01"})
    bridge.accrue_payroll(c, {"id": 3, "period_start": "2026-10-01",
                              "period_end": "2026-10-15"}, PAY_LINES)
    if materials:
        bridge.post_cost_line(c, BID, {"id": 10, "category": "material",
                                       "description": "USG board", "actual": materials,
                                       "date": "2026-10-05"})
    if overhead:
        bridge.record_operating_expense(c, "6030", overhead, "Liability premium", "2026-10-01")
    if deposit:
        bridge.post_deposit(c, {"id": 50, "name": "Homeowner"}, deposit)


# --- money is integer cents -------------------------------------------------
@pytest.mark.parametrize("raw,cents", [
    (500.00, 50000), (0.07, 7), ("$1,234.56", 123456),
    ("(50.00)", -5000), ("($1,000.00)", -100000),
])
def test_cents_are_exact(raw, cents):
    assert ledger._c(raw) == cents


@pytest.mark.parametrize("bad", [0.001, True, None, "abc", [], {}])
def test_non_money_is_refused(bad):
    with pytest.raises(ledger.LedgerError):
        ledger._c(bad)


# --- an entry must balance --------------------------------------------------
@pytest.mark.parametrize("lines", [
    [{"account": "1010", "debit": 50000}],
    [{"account": "4010", "credit": 50000}],
    [{"account": "1010", "debit": 50000}, {"account": "4010", "credit": 49999}],
])
def test_unbalanced_entry_is_refused(conn, lines):
    with pytest.raises(ledger.LedgerError):
        ledger.post(conn, lines)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ledger_entries")
        assert cur.fetchone()["n"] == 0, "a refused post must write nothing"


@pytest.mark.parametrize("line", [
    {"account": "1010", "debit": 1, "credit": 1},
    {"account": "1010", "debit": 0, "credit": 0},
])
def test_line_cannot_be_both_or_neither(conn, line):
    with pytest.raises(ledger.LedgerError):
        ledger.post(conn, [line, {"account": "4010", "credit": 5}])


def test_unknown_account_is_refused(conn):
    with pytest.raises(ledger.LedgerError):
        ledger.post(conn, [{"account": "9999", "debit": 1}, {"account": "1010", "credit": 1}])


# --- the accrual, which is the reason for doing this at all -----------------
def test_wages_are_on_the_books_before_the_money_moves(conn):
    bridge.post_invoice(conn, BID, {"id": 1, "invoice_number": "INV-1",
                                    "amount": 150000.00, "issued_date": "2026-10-01"})
    bridge.accrue_payroll(conn, {"id": 3, "period_start": "2026-10-01",
                                 "period_end": "2026-10-15"}, PAY_LINES)
    bs = ledger.balance_sheet(conn)
    ist = ledger.income_statement(conn)
    labor = [x for x in ist["expense"] if x["code"] == "5020"][0]["amount"]

    assert labor == LABOR_CENTS / 100, "wage must be an expense before it is paid"
    assert bs["assets"]["1010"]["balance"] == 0, "accrual must not move cash"
    assert bs["liabilities"]["2150"]["balance"] == LABOR_CENTS / 100
    # and the job's margin is already true mid-project
    assert ledger.job_pnl(conn)[0]["gross_margin"] == 150000.00 - LABOR_CENTS / 100


def test_paying_relieves_the_liability(conn):
    bridge.accrue_payroll(conn, {"id": 3}, PAY_LINES)
    bridge.pay_payroll(conn, {"id": 3, "paid_date": "2026-10-16"}, GROSS_LINES)
    bs = ledger.balance_sheet(conn)
    assert bs["liabilities"]["2150"]["balance"] == 0
    assert bs["assets"]["1010"]["balance"] == -LABOR_CENTS / 100


def test_labor_cannot_be_typed_in_as_a_cost_line(conn):
    """It accrues from timesheets; letting it also be a cost line double counts it."""
    assert bridge.post_cost_line(conn, BID, {"id": 99, "category": "labor",
                                              "actual": 500.00})["posted"] is False


def test_payroll_line_shape_is_validated(conn):
    for bad in ("nope", [1, 2]):
        with pytest.raises(ledger.LedgerError):
            bridge.accrue_payroll(conn, {"id": 3}, bad)


# --- the accounting equation ------------------------------------------------
def test_empty_books_balance(conn):
    ledger.open_books(conn, OPENING_CASH, when="2026-10-01")
    bs = ledger.balance_sheet(conn, as_of="2026-09-30")
    assert bs["balanced"]


def test_balance_sheet_includes_unclosed_earnings(conn):
    ledger.open_books(conn, OPENING_CASH, when="2026-10-01")
    _book_a_job(conn)
    bs = ledger.balance_sheet(conn)
    assert bs["current_earnings"] != 0
    assert bs["balanced"], f"A={bs['total_assets']} L+E={bs['total_liabilities'] + bs['total_equity']}"
    assert abs(bs["difference"]) < 0.01


def test_every_chart_account_appears_even_at_zero(conn):
    ledger.open_books(conn, OPENING_CASH)
    bs = ledger.balance_sheet(conn)
    assert "1010" in bs["assets"] and "1250" in bs["assets"]
    assert "2150" in bs["liabilities"]


def test_trial_balance_always_sums_to_zero(conn):
    ledger.open_books(conn, OPENING_CASH)
    _book_a_job(conn)
    bridge.pay_payroll(conn, {"id": 3, "paid_date": "2026-10-16"}, GROSS_LINES)
    tb = ledger.trial_balance(conn)
    assert tb["balanced"], tb["difference"]
    assert abs(tb["difference"]) < 0.01


def test_job_pnl(conn):
    ledger.open_books(conn, OPENING_CASH)
    _book_a_job(conn, materials=7500.00)
    row = ledger.job_pnl(conn)[0]
    assert row["revenue"] == 150000.00
    assert row["direct_cost"] == LABOR_CENTS / 100 + 7500.00
    assert row["gross_margin"] == 150000.00 - (LABOR_CENTS / 100 + 7500.00)


# --- idempotency ------------------------------------------------------------
def test_reposting_a_source_is_a_no_op(conn):
    ledger.open_books(conn, OPENING_CASH)
    _book_a_job(conn)
    before = ledger.income_statement(conn)["net_income"]
    for _ in range(3):
        bridge.post_invoice(conn, BID, {"id": 1, "invoice_number": "INV-1",
                                        "amount": 150000.00, "issued_date": "2026-10-01"})
        bridge.accrue_payroll(conn, {"id": 3, "period_start": "2026-10-01",
                                     "period_end": "2026-10-15"}, PAY_LINES)
        bridge.post_cost_line(conn, BID, {"id": 10, "category": "material",
                                          "description": "USG board", "actual": 7500.00})
    assert ledger.income_statement(conn)["net_income"] == before
    assert ledger.trial_balance(conn)["balanced"]


# --- close the books --------------------------------------------------------
def test_closing_moves_earnings_and_changes_nothing_else(conn):
    ledger.open_books(conn, OPENING_CASH)
    _book_a_job(conn)
    before = ledger.balance_sheet(conn)

    assert ledger.close_period(conn, "2026-12-31")["posted"]

    after = ledger.balance_sheet(conn)
    ist = ledger.income_statement(conn)
    assert abs(ist["total_revenue"]) < 0.01 and abs(ist["total_expense"]) < 0.01
    assert after["balanced"]
    assert abs(after["total_assets"] - before["total_assets"]) < 0.01, "closing is a reclassification"
    assert ledger.trial_balance(conn)["balanced"]


# --- corrections ------------------------------------------------------------
def test_entries_are_corrected_by_reversal_not_by_editing(conn):
    ledger.open_books(conn, OPENING_CASH)
    _book_a_job(conn, materials=7500.00)
    eid = [e for e in ledger.entries(conn) if e["source_type"] == "cost"][0]["id"]
    assert ledger.reverse_entry(conn, eid, memo="wrong job")["posted"]
    tb = ledger.trial_balance(conn)
    assert tb["balanced"], tb["difference"]


def test_chart_covers_construction_costs():
    codes = {c for c, _, _ in ledger.CHART}
    for required in ("1010", "1100", "2150", "4010", "5010", "5020", "5030"):
        assert required in codes, f"{required} missing from the chart"
    for cat in ledger.CATEGORY_ACCOUNT:
        assert ledger.CATEGORY_ACCOUNT[cat] in codes