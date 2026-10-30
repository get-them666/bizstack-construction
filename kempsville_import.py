"""Kempsville Building Materials stock catalog import."""
import csv
import io
from datetime import datetime

KBM_STOCK_CATALOG = [
    # --- DIMENSIONAL LUMBER & FRAMING ---
    {"SKU": "KBM-DF-2408", "Desc": "2x4x8 Stud Douglas Fir Premium", "Price": 4.15, "UOM": "Each"},
    {"SKU": "KBM-DF-2412", "Desc": "2x4x12 Structural Lumber Douglas Fir", "Price": 6.45, "UOM": "Each"},
    {"SKU": "KBM-DF-2416", "Desc": "2x4x16 Stud Douglas Fir", "Price": 8.95, "UOM": "Each"},
    {"SKU": "KBM-DF-2612", "Desc": "2x6x12 Structural Lumber Premium", "Price": 11.20, "UOM": "Each"},
    {"SKU": "KBM-DF-2616", "Desc": "2x6x16 Structural Lumber Premium", "Price": 14.85, "UOM": "Each"},
    {"SKU": "KBM-DF-2812", "Desc": "2x8x12 Structural Lumber Premium", "Price": 16.20, "UOM": "Each"},
    {"SKU": "KBM-DF-2816", "Desc": "2x8x16 Structural Lumber Premium", "Price": 21.50, "UOM": "Each"},
    {"SKU": "KBM-DF-21012", "Desc": "2x10x12 Structural Lumber Premium", "Price": 22.40, "UOM": "Each"},
    {"SKU": "KBM-DF-21016", "Desc": "2x10x16 Structural Lumber Premium", "Price": 29.95, "UOM": "Each"},
    
    # --- ENGINEERED SHEATHING & PLYWOOD ---
    {"SKU": "KBM-OSB-4871", "Desc": "7/16 in. 4x8 OSB Rated Sheathing", "Price": 15.25, "UOM": "Sheet"},
    {"SKU": "KBM-PLY-3471", "Desc": "3/4 in. 4x8 CDX Structural Plywood", "Price": 31.95, "UOM": "Sheet"},
    {"SKU": "KBM-PLY-1271", "Desc": "1/2 in. 4x8 CDX Structural Plywood", "Price": 24.50, "UOM": "Sheet"},
    {"SKU": "KBM-OSB-34TG", "Desc": "3/4 in. 4x8 OSB Tongue & Groove Subfloor", "Price": 28.75, "UOM": "Sheet"},

    # --- DRYWALL SUITE & COMPOUNDS ---
    {"SKU": "KBM-DW-1248", "Desc": "1/2 in. 4x8 Standard Gypsum Drywall Board", "Price": 13.95, "UOM": "Sheet"},
    {"SKU": "KBM-DW-5848", "Desc": "5/8 in. 4x12 Type X Fire-Rated Drywall", "Price": 21.50, "UOM": "Sheet"},
    {"SKU": "KBM-DW-1248MR", "Desc": "1/2 in. 4x8 Mold & Moisture Resistant Drywall", "Price": 17.10, "UOM": "Sheet"},
    {"SKU": "KBM-JC-05GL", "Desc": "Ready-Mix All-Purpose Joint Compound Premium", "Price": 17.95, "UOM": "5 Gal Pail"},
    {"SKU": "KBM-DT-250FT", "Desc": "Standard Fibermesh Drywall Joint Tape 250ft", "Price": 6.25, "UOM": "Roll"},

    # --- INSULATION PRODUCTS ---
    {"SKU": "KBM-INS-R13", "Desc": "R-13 Kraft Faced Fiberglass Insulation Roll 15\"x40'", "Price": 24.25, "UOM": "Roll"},
    {"SKU": "KBM-INS-R19", "Desc": "R-19 Kraft Faced Fiberglass Insulation Roll 23\"x30'", "Price": 34.60, "UOM": "Roll"},
    {"SKU": "KBM-INS-R30", "Desc": "R-30 Unfaced Fiberglass Insulation Roll 24\"x25'", "Price": 42.10, "UOM": "Roll"},

    # --- ROOFING COMPONENTS ---
    {"SKU": "KBM-RF-ARCH", "Desc": "Architectural Asphalt Shingles (33.3 sq ft cover)", "Price": 34.50, "UOM": "Bundle"},
    {"SKU": "KBM-RF-F300", "Desc": "#30 Heavy-Duty Asphalt Roofing Felt 200 sq ft", "Price": 26.80, "UOM": "Roll"},
    {"SKU": "KBM-RF-AL10", "Desc": "10 ft. Aluminum Drip Edge Flashing White", "Price": 7.15, "UOM": "Each"},
    {"SKU": "KBM-NL-C125", "Desc": "1-1/4 in. Coil Roofing Nails 15-Degree Wire", "Price": 38.00, "UOM": "Box 7200ct"},

    # --- MILLWORK, CASING & TRIM ---
    {"SKU": "KBM-MW-WM356", "Desc": "WM356 2-1/4 in. Primed MDF Casing Trim", "Price": 1.15, "UOM": "LF"},
    {"SKU": "KBM-MW-WM623", "Desc": "WM623 3-1/4 in. Primed MDF Baseboard Trim", "Price": 1.45, "UOM": "LF"},
    {"SKU": "KBM-MW-WM47", "Desc": "WM47 4-5/8 in. Primed MDF Crown Moulding", "Price": 2.10, "UOM": "LF"},

    # --- FASTENERS & HARDWARE ---
    {"SKU": "KBM-FS-16D", "Desc": "16D Common Bright Framing Nails 50lb Box", "Price": 64.50, "UOM": "Box"},
    {"SKU": "KBM-FS-8D", "Desc": "8D Coated Sinker Framing Nails 50lb Box", "Price": 68.20, "UOM": "Box"}
]


def generate_kempsville_csv() -> str:
    """Generate CSV format compatible with supplier_prices parser."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Item Number", "Description", "UOM", "Price"])
    for row in KBM_STOCK_CATALOG:
        writer.writerow([row["SKU"], row["Desc"], row["UOM"], f"{row['Price']:.2f}"])
    return output.getvalue()


def load_kempsville_to_supplier(conn, public_ok: bool = True):
    """Load Kempsville catalog into the supplier pricing system."""
    import supplier_adapters
    csv_data = generate_kempsville_csv()
    return supplier_adapters.register_sheet_supplier(
        conn,
        name="Kempsville Building Materials",
        sheet_text=csv_data,
        category="lumber",
        default_uom="each",
        public_ok=public_ok
    )
