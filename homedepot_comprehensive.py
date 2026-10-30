"""Generate comprehensive Home Depot price sheet."""
import csv
import io

# Comprehensive list covering requested categories
ITEMS = [
    # BATHROOM
    ("HD-1001", "Delta Foundations Tub/Shower Faucet Chrome", "Each", 89.00),
    ("HD-1002", "Moen Genta Bathroom Faucet Matte Black", "Each", 129.00),
    ("HD-1003", "American Standard Toilet 1.28 GPF White", "Each", 199.00),
    ("HD-1004", "KOHLER Archer Toilet Comfort Height", "Each", 279.00),
    ("HD-1005", "Shower Pan 60x32 Acrylic", "Each", 159.00),
    ("HD-1006", "Bathroom Vanity 36in White Shaker", "Each", 399.00),
    ("HD-1007", "Bathroom Exhaust Fan 50 CFM", "Each", 49.00),
    ("HD-1008", "Tile Shower Niche Recessed", "Each", 39.00),
    
    # KITCHEN
    ("HD-2001", "Hampton Bay Shaker Cabinets 36in Base", "Each", 189.00),
    ("HD-2002", "Hampton Bay Wall Cabinet 30in", "Each", 129.00),
    ("HD-2003", "Granite Countertop Slab (per sq ft)", "Sq Ft", 45.00),
    ("HD-2004", "Quartz Countertop (per sq ft)", "Sq Ft", 55.00),
    ("HD-2005", "Moen Kitchen Faucet Single Handle Chrome", "Each", 149.00),
    ("HD-2006", "Stainless Steel Kitchen Sink 33in", "Each", 199.00),
    ("HD-2007", "Garbage Disposal 3/4 HP", "Each", 129.00),
    ("HD-2008", "Dishwasher Standard 24in", "Each", 449.00),
    ("HD-2009", "Range Hood 30in Under Cabinet", "Each", 149.00),
    
    # DRYWALL
    ("HD-3001", "1/2 in. 4x8 Drywall Sheet", "Sheet", 15.48),
    ("HD-3002", "5/8 in. 4x8 Type X Fire-Rated Drywall", "Sheet", 19.28),
    ("HD-3003", "1/2 in. 4x12 Drywall Sheet", "Sheet", 23.22),
    ("HD-3004", "Ready-Mix Joint Compound 5 Gal", "Bucket", 19.98),
    ("HD-3005", "Drywall Tape 250 ft", "Roll", 4.98),
    ("HD-3006", "Drywall Screws 1-1/4 in 5 lb", "Box", 6.98),
    ("HD-3007", "Corner Bead 8 ft", "Each", 2.98),
    
    # PAINT
    ("HD-4001", "Interior Paint Flat 1 Gal Eggshell", "Gal", 39.98),
    ("HD-4002", "Interior Paint Satin 1 Gal", "Gal", 42.98),
    ("HD-4003", "Exterior Paint Satin 1 Gal", "Gal", 54.98),
    ("HD-4004", "Primer Interior/Exterior 1 Gal", "Gal", 29.98),
    ("HD-4005", "Painter's Tape 1.88 in x 60 yd", "Roll", 5.98),
    ("HD-4006", "Paint Roller Kit 9 in", "Kit", 12.98),
    ("HD-4007", "Caulk Paintable Silicone 10.1 oz", "Tube", 4.98),
    
    # FLOORING
    ("HD-5001", "Luxury Vinyl Plank 7x48 Click Lock", "Sq Ft", 2.69),
    ("HD-5002", "Luxury Vinyl Plank Waterproof 6x48", "Sq Ft", 3.49),
    ("HD-5003", "Laminate Flooring 8mm AC4", "Sq Ft", 1.99),
    ("HD-5004", "Ceramic Tile 12x12", "Sq Ft", 1.49),
    ("HD-5005", "Porcelain Tile 12x24 Matte", "Sq Ft", 3.29),
    ("HD-5006", "Vinyl Sheet Flooring", "Sq Ft", 1.79),
    ("HD-5007", "Tile Spacers 1/8 in 250 ct", "Bag", 2.98),
    ("HD-5008", "Thinset Mortar 50 lb", "Bag", 16.98),
    ("HD-5009", "Grout Sanded 25 lb", "Bag", 14.98),
    
    # LUMBER
    ("HD-6001", "2x4x8 SPF Stud", "Each", 5.48),
    ("HD-6002", "2x4x10 SPF Stud", "Each", 6.98),
    ("HD-6003", "2x6x8 SPF", "Each", 8.28),
    ("HD-6004", "2x6x10 SPF", "Each", 10.38),
    ("HD-6005", "2x8x8 SPF", "Each", 11.28),
    ("HD-6006", "2x10x10 SPF", "Each", 16.58),
    ("HD-6007", "2x12x12 SPF", "Each", 26.38),
    ("HD-6008", "Pressure Treated 2x4x8", "Each", 7.48),
    ("HD-6009", "Pressure Treated 2x6x8", "Each", 12.28),
    ("HD-6010", "LVL Beam 1.75x11.25x20 ft", "Each", 189.00),
    ("HD-6011", "OSB Sheathing 7/16 in 4x8", "Sheet", 16.98),
    ("HD-6012", "CDX Plywood 3/4 in 4x8", "Sheet", 42.98),
    ("HD-6013", "CDX Plywood 1/2 in 4x8", "Sheet", 34.98),
    
    # COUNTERTOPS
    ("HD-7001", "Laminate Countertop 8 ft", "Each", 189.00),
    ("HD-7002", "Laminate Countertop Custom per LF", "LF", 39.00),
    ("HD-7003", "Quartz Countertop Slab per Sq Ft", "Sq Ft", 59.00),
    ("HD-7004", "Granite Countertop Slab per Sq Ft", "Sq Ft", 49.00),
    ("HD-7005", "Butcher Block Countertop 8 ft", "Each", 299.00),
    ("HD-7006", "Solid Surface Countertop per Sq Ft", "Sq Ft", 65.00),
    
    # CABINETS
    ("HD-8001", "Base Cabinet 36in Shaker White", "Each", 249.00),
    ("HD-8002", "Wall Cabinet 30in Shaker White", "Each", 169.00),
    ("HD-8003", "Tall Cabinet Pantry 84in", "Each", 449.00),
    ("HD-8004", "Corner Base Cabinet 36in", "Each", 299.00),
    ("HD-8005", "Sink Base Cabinet 36in", "Each", 229.00),
    ("HD-8006", "Drawer Base Cabinet 36in", "Each", 279.00),
    ("HD-8007", "Wall End Cabinet 12in", "Each", 129.00),
    ("HD-8008", "Cabinet Hardware Pull Satin Nickel", "Each", 4.98),
    
    # INSULATION
    ("HD-9001", "R-13 Kraft Faced Batt 15x93", "Sq Ft", 0.59),
    ("HD-9002", "R-19 Kraft Faced Batt 23x93", "Sq Ft", 0.79),
    ("HD-9003", "R-30 Blown-In Cellulose", "Sq Ft", 1.29),
    ("HD-9004", "R-38 Blown-In Fiberglass", "Sq Ft", 1.49),
    ("HD-9005", "Foam Board Insulation 2 in R-10", "Sheet", 29.98),
    ("HD-9006", "Spray Foam Kit 200 sq ft", "Kit", 449.00),
    ("HD-9007", "Pipe Insulation Foam 3/4 in", "Each", 2.98),
    
    # SHINGLES
    ("HD-10001", "3-Tab Asphalt Shingles 33.33 sq ft", "Bundle", 29.98),
    ("HD-10002", "Architectural Shingles 33.33 sq ft", "Bundle", 39.98),
    ("HD-10003", "Premium Architectural Shingles 33.33 sq ft", "Bundle", 49.98),
    ("HD-10004", "Designer Shingles 33.33 sq ft", "Bundle", 59.98),
    ("HD-10005", "Roofing Felt #15 144 sq ft", "Roll", 36.98),
    ("HD-10006", "Roofing Felt #30 288 sq ft", "Roll", 59.98),
    ("HD-10007", "Ice & Water Shield 36 in x 75 ft", "Roll", 149.00),
    ("HD-10008", "Drip Edge Aluminum 10 ft", "Each", 8.98),
    ("HD-10009", "Ridge Cap Shingles 33.33 sq ft", "Bundle", 39.98),
    ("HD-10010", "Roofing Nails 1-1/4 in 5 lb", "Box", 7.98),
    
    # TILE
    ("HD-11001", "Ceramic Wall Tile 3x6 Subway White", "Sq Ft", 0.99),
    ("HD-11002", "Ceramic Floor Tile 12x12 Matte", "Sq Ft", 1.49),
    ("HD-11003", "Porcelain Floor Tile 12x24", "Sq Ft", 3.49),
    ("HD-11004", "Porcelain Large Format 24x48", "Sq Ft", 4.99),
    ("HD-11005", "Glass Mosaic Tile Sheet", "Sheet", 12.98),
    ("HD-11006", "Natural Stone Travertine 12x12", "Sq Ft", 5.99),
    ("HD-11007", "Tile Backer Board 1/2 in 3x5", "Sheet", 12.98),
    ("HD-11008", "Tile Edge Trim L-Shaped", "LF", 2.98),
]


def generate_comprehensive_csv() -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["SKU", "Description", "Price", "UOM"])
    for sku, desc, uom, price in ITEMS:
        writer.writerow([sku, desc, f"{price:.2f}", uom])
    return output.getvalue()


if __name__ == '__main__':
    print(generate_comprehensive_csv()[:200])
