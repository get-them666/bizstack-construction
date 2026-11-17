import os
import argparse
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime

try:
    import psycopg
except ImportError:
    try:
        import psycopg2 as psycopg  # type: ignore[no-redef]
    except ImportError:
        psycopg = None

LLC_LEGAL_NAME = "BuildStack Construction LLC"
DBA_NAME = "Broom Service"

# Match construction_main.py's default so this script hits the same database
# the web app writes to. Override via DATABASE_URL env var.
DB_URL = os.getenv("DATABASE_URL", "postgresql://shaun:secret@localhost:5432/buildstack")
TAX_YEAR = int(os.getenv("TAX_YEAR", datetime.now().year))
IRS_MILEAGE_RATE = float(os.getenv("IRS_MILEAGE_RATE", "0.70"))

# URL normalization records from the bank feed's vendor/payee text. The PNC
# importer already tags business_unit at import time, but these rules catch
# anything the importer missed and also serve as the canonical map for the
# tax report's own pivot.
BUSINESS_UNIT_MAP = {
    "://bizstackperks.com": "BuildStack Construction",
    "buildstack": "BuildStack Construction",
    "construction": "BuildStack Construction",
    "bizstackperks.com": "Broom Service",
    "broomservice": "Broom Service",
    "broom_service": "Broom Service",
    "broom service": "Broom Service",
}

SCHEDULE_C_RULES = {
    "Contract Labor (Line 11)": ["subcontractor", "cleaner", "crew", "payout", "1099", "labor"],
    "Supplies & Materials (Line 22)": ["home depot", "lowes", "lowe's", "sherwin", "lumber", "bleach", "linen", "cleaning supply", "supply", "tile", "drywall"],
    "Advertising & Leads (Line 8)": ["thumbtack", "google ads", "meta", "facebook", "angi", "yelp", "marketing"],
    "Software & Hosting (Line 18)": ["zoho", "openai", "anthropic", "vercel", "aws", "stripe", "turno", "hospitable", "domain", "hosting"],
    "Insurance (Line 15)": ["insurance", "liability", "workers comp", "next insurance", "hiscox"],
    "Car & Truck / Actual Fuel (Line 9)": ["shell", "exxon", "wawa", "sheetz", "fuel", "gas", "auto"],
    "Legal & Professional (Line 17)": ["scc", "cpa", "attorney", "registered agent", "license", "permit", "bpol"],
}

MILEAGE_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS mileage_logs (
    id              BIGSERIAL PRIMARY KEY,
    trip_date       DATE NOT NULL,
    business_unit   VARCHAR(80) NOT NULL DEFAULT 'BuildStack Construction',
    origin          VARCHAR(255),
    destination     VARCHAR(255),
    purpose         TEXT,
    miles           NUMERIC(10,2) NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def normalize_unit(raw_unit: str) -> str:
    if not raw_unit:
        return "BuildStack Construction"
    key = raw_unit.strip().lower()
    # The bank feed sometimes embeds the domain with the :// prefix; match it
    # before stripping so "://bizstackperks.com" resolves to Construction, not
    # Broom Service (the bare-domain rule would otherwise win).
    if "://bizstackperks.com" in key:
        return "BuildStack Construction"
    return BUSINESS_UNIT_MAP.get(key, raw_unit.strip())


def categorize_expense(description: str, payee: str = "") -> str:
    text_blob = f"{description} {payee}".lower()
    for irs_line, keywords in SCHEDULE_C_RULES.items():
        if any(kw in text_blob for kw in keywords):
            return irs_line
    return "Other Expenses (Line 27a)"


def _get_psql_connection():
    """Return a psycopg connection for schema init, or None."""
    if psycopg is None:
        return None
    if not DB_URL.startswith("postgresql"):
        return None
    try:
        return psycopg.connect(DB_URL, autocommit=True)
    except Exception as exc:
        print(f"Warning: could not connect to Postgres to ensure schema: {exc}")
        return None


def _ensure_tables():
    """Make sure transactions and mileage_logs exist before we query them.

    transactions is created by pnc_importer.ensure_schema (called on every app
    boot). mileage_logs is new — we create it here if missing. If the database
    is unreachable we fall back to an empty result rather than crashing.
    """
    conn = _get_psql_connection()
    if conn is None:
        return
    try:
        # Reuse the importer's DDL so the tax engine and the web app agree on
        # the shape of transactions. Idempotent — safe to call every run.
        try:
            import pnc_importer
            with psycopg.connect(DB_URL, autocommit=False) as tx_conn:
                pnc_importer.ensure_schema(tx_conn)
        except ImportError:
            # pnc_importer not importable — fall back to raw DDL
            with psycopg.connect(DB_URL, autocommit=False) as tx_conn:
                from pnc_importer import DDL as TX_DDL
                with tx_conn.cursor() as cur:
                    cur.execute(TX_DDL)
                tx_conn.commit()
        except Exception as exc:
            print(f"Warning: could not ensure transactions schema: {exc}")

        # mileage_logs — always safe to create; never written by the web app yet
        try:
            with psycopg.connect(DB_URL, autocommit=False) as ml_conn:
                with ml_conn.cursor() as cur:
                    cur.execute(MILEAGE_LOGS_DDL)
                ml_conn.commit()
        except Exception as exc:
            print(f"Warning: could not ensure mileage_logs schema: {exc}")
    finally:
        conn.close()


def run_tax_report(quarter: str = None):
    _ensure_tables()

    engine = create_engine(DB_URL)
    is_sqlite = DB_URL.startswith("sqlite")

    tx_query = text("""
        SELECT
            id,
            tx_date,
            COALESCE(business_unit, 'BuildStack Construction') AS business_unit,
            LOWER(tx_type) AS tx_type,
            COALESCE(vendor_or_payee, 'Unknown') AS payee,
            COALESCE(description, '') AS description,
            CAST(amount AS FLOAT) AS amount,
            COALESCE(payment_method, 'Unknown') AS payment_method,
            COALESCE(w9_on_file, 0) AS w9_on_file
        FROM transactions
        WHERE strftime('%Y', tx_date) = :year
    """ if is_sqlite else """
        SELECT
            id,
            tx_date,
            COALESCE(business_unit, 'BuildStack Construction') AS business_unit,
            LOWER(tx_type) AS tx_type,
            COALESCE(vendor_or_payee, 'Unknown') AS payee,
            COALESCE(description, '') AS description,
            CAST(amount AS NUMERIC) AS amount,
            COALESCE(payment_method, 'Unknown') AS payment_method,
            COALESCE(w9_on_file, FALSE) AS w9_on_file
        FROM transactions
        WHERE EXTRACT(YEAR FROM tx_date) = :year
    """)

    try:
        with engine.connect() as conn:
            df = pd.read_sql(tx_query, conn, params={"year": str(TAX_YEAR) if is_sqlite else TAX_YEAR})

            try:
                mile_query = text("""
                    SELECT id, trip_date, COALESCE(business_unit, 'BuildStack Construction') AS business_unit,
                           origin, destination, purpose, CAST(miles AS FLOAT) AS miles
                    FROM mileage_logs
                    WHERE strftime('%Y', trip_date) = :year
                """ if is_sqlite else """
                    SELECT id, trip_date, COALESCE(business_unit, 'BuildStack Construction') AS business_unit,
                           origin, destination, purpose, CAST(miles AS NUMERIC) AS miles
                    FROM mileage_logs
                    WHERE EXTRACT(YEAR FROM trip_date) = :year
                """)
                mile_df = pd.read_sql(mile_query, conn, params={"year": str(TAX_YEAR) if is_sqlite else TAX_YEAR})
            except Exception:
                mile_df = pd.DataFrame(columns=["id", "trip_date", "business_unit", "origin", "destination", "purpose", "miles"])
    except Exception as exc:
        print(f"Error connecting to database or querying transactions: {exc}")
        print(f"DATABASE_URL={DB_URL}")
        print("Ensure Postgres is running and DATABASE_URL is set correctly.")
        return

    if df.empty:
        print(f"No transactions found for {LLC_LEGAL_NAME} in {TAX_YEAR}.")
        print("(The transactions table exists but has no rows for this year yet.)")
        return

    df["tx_date"] = pd.to_datetime(df["tx_date"])
    if quarter:
        q_num = int(quarter.upper().replace("Q", ""))
        df = df[df["tx_date"].dt.quarter == q_num]

    df["business_unit"] = df["business_unit"].apply(normalize_unit)
    df["amount"] = df["amount"].astype(float).abs()
    df["irs_category"] = df.apply(
        lambda r: "Gross Receipts (Line 1)" if r["tx_type"] == "income" else categorize_expense(r["description"], r["payee"]),
        axis=1
    )

    pnl = df.pivot_table(
        index=["tx_type", "irs_category"],
        columns="business_unit",
        values="amount",
        aggfunc="sum",
        fill_value=0.0
    )
    pnl[f"{LLC_LEGAL_NAME} Total"] = pnl.sum(axis=1)

    if not mile_df.empty:
        mile_df["business_unit"] = mile_df["business_unit"].apply(normalize_unit)
        mile_df["deduction_usd"] = mile_df["miles"].astype(float) * IRS_MILEAGE_RATE
        mile_summary = mile_df.groupby("business_unit", as_index=False).agg(
            total_miles=("miles", "sum"),
            standard_deduction_usd=("deduction_usd", "sum")
        )
    else:
        mile_summary = pd.DataFrame([{"business_unit": "No mileage_logs rows found", "total_miles": 0.0, "standard_deduction_usd": 0.0}])

    non_card_methods = {"ach", "check", "cash", "zelle", "venmo_personal", "direct_deposit"}
    subs = df[
        (df["irs_category"] == "Contract Labor (Line 11)") &
        (df["payment_method"].str.lower().isin(non_card_methods))
    ].copy()

    report_1099 = (
        subs.groupby("payee", as_index=False)
        .agg(
            total_paid=("amount", "sum"),
            payout_count=("id", "count"),
            w9_on_file=("w9_on_file", "max")
        )
        .sort_values(by="total_paid", ascending=False)
    )
    report_1099["requires_1099_nec"] = report_1099["total_paid"] >= 600.0
    report_1099["action_needed"] = report_1099.apply(
        lambda r: "MISSING W-9 - COLLECT NOW" if (r["requires_1099_nec"] and not r["w9_on_file"])
        else ("ISSUE 1099-NEC BY JAN 31" if r["requires_1099_nec"] else "Under $600 Threshold"),
        axis=1
    )

    gross_income = df[df["tx_type"] == "income"]["amount"].sum()
    total_expenses = df[df["tx_type"] == "expense"]["amount"].sum()
    net_profit = max(0.0, gross_income - total_expenses)
    est_tax_reserve = round(net_profit * 0.25, 2)

    summary_meta = pd.DataFrame([
        {"Metric": "Legal Taxpayer Entity", "Value": LLC_LEGAL_NAME},
        {"Metric": "Registered DBA / Trade Name", "Value": DBA_NAME},
        {"Metric": "Period", "Value": f"{TAX_YEAR} ({quarter.upper() if quarter else 'Full Year'})"},
        {"Metric": "Gross Income", "Value": f"${gross_income:,.2f}"},
        {"Metric": "Tracked Expenses (Excl. Mileage)", "Value": f"${total_expenses:,.2f}"},
        {"Metric": "Net Profit Before Mileage", "Value": f"${net_profit:,.2f}"},
        {"Metric": "Recommended 25% Tax Reserve", "Value": f"${est_tax_reserve:,.2f}"},
    ])

    tag = f"_{quarter.upper()}" if quarter else ""
    out_file = f"BuildStack_Construction_LLC_Tax_Packet_{TAX_YEAR}{tag}.xlsx"
    with pd.ExcelWriter(out_file, engine="openpyxl") as writer:
        summary_meta.to_excel(writer, sheet_name="Executive_Summary", index=False)
        pnl.to_excel(writer, sheet_name="Schedule_C_PnL")
        report_1099.to_excel(writer, sheet_name="1099_NEC_Tracker", index=False)
        mile_summary.to_excel(writer, sheet_name="Mileage_Deduction", index=False)
        df.to_excel(writer, sheet_name="Full_Audit_Ledger", index=False)

    print(f"Generated {out_file} for {LLC_LEGAL_NAME} (DBA {DBA_NAME}).")
    print(f"  {len(df)} transactions, {len(mile_df) if not mile_df.empty else 0} mileage logs.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quarter", type=str, default=None, help="Optional quarter filter: Q1, Q2, Q3, or Q4")
    args = parser.parse_args()
    run_tax_report(quarter=args.quarter)
