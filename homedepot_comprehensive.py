"""Generate comprehensive Home Depot price sheet."""
import csv
import io

# Customer-facing materials for picker
ITEMS = [
    # BATHROOM
    ("HD-1001", "Delta Foundations Tub/Shower Faucet Chrome", "Each", 89.00),
    ("HD-1002", "Moen Genta Bathroom Faucet Matte Black", "Each", 129.00),
    ("HD-1003", "American Standard Toilet 1.28 GPF White", "Each", 199.00),
    ("HD-1004", "KOHLER Archer Toilet Comfort Height", "Each", 279.00),
    ("HD-1005", "Shower Pan 60x32 Acrylic", "Each", 159.00),
    ("HD-1006", "Bathroom Vanity 36in White Shaker", "Each", 399.00),
    ("HD-1007", "Bathroom Vanity 48in Gray Shaker", "Each", 549.00),
    ("HD-1008", "American Standard Toilet 1.28 GPF White", "Each", 199.00),
    ("HD-1009", "KOHLER Archer Toilet Comfort Height", "Each", 279.00),
    ("HD-1010", "Toto Drake Toilet Elongated", "Each", 329.00),
    ("HD-1011", "Pedestal Sink White", "Each", 149.00),
    ("HD-1012", "Bathroom Mirror 36x30 Framed", "Each", 89.00),
    ("HD-1013", "Towel Bar Set Chrome", "Each", 39.00),
    
    # KITCHEN
    ("HD-2001", "Hampton Bay Shaker Base Cabinet 36in White", "Each", 249.00),
    ("HD-2002", "Hampton Bay Wall Cabinet 30in White", "Each", 169.00),
    ("HD-2003", "Hampton Bay Pantry Cabinet 84in White", "Each", 449.00),
    ("HD-2004", "Hampton Bay Corner Base Cabinet 36in", "Each", 299.00),
    ("HD-2005", "Hampton Bay Sink Base Cabinet 36in", "Each", 229.00),
    ("HD-2006", "Granite Countertop (per sq ft)", "Sq Ft", 45.00),
    ("HD-2007", "Quartz Countertop (per sq ft)", "Sq Ft", 55.00),
    ("HD-2008", "Moen Kitchen Faucet Single Handle Chrome", "Each", 149.00),
    ("HD-2009", "Stainless Steel Kitchen Sink 33in", "Each", 199.00),
    ("HD-2010", "Dishwasher 24in Stainless Steel", "Each", 449.00),
    ("HD-2011", "Dishwasher 24in Built-In Black", "Each", 399.00),
    ("HD-2012", "Garbage Disposal 3/4 HP", "Each", 129.00),
    ("HD-2013", "Kitchen Cabinet Hardware Pull Satin Nickel", "Each", 4.98),
    
    # CABINETS (standalone)
    ("HD-8001", "Base Cabinet 36in Shaker White", "Each", 249.00),
    ("HD-8002", "Wall Cabinet 30in Shaker White", "Each", 169.00),
    ("HD-8003", "Tall Cabinet Pantry 84in", "Each", 449.00),
    ("HD-8004", "Corner Base Cabinet 36in", "Each", 299.00),
    ("HD-8005", "Sink Base Cabinet 36in", "Each", 229.00),
    ("HD-8006", "Drawer Base Cabinet 36in", "Each", 279.00),
    
    # DOORS
    ("HD-2101", "Pre-Hung Interior Door 36x80 6-Panel", "Each", 149.00),
    ("HD-2102", "Pre-Hung Interior Door 30x80 Shaker", "Each", 169.00),
    ("HD-2103", "Pre-Hung Exterior Door 36x80 Fiberglass", "Each", 699.00),
    ("HD-2104", "Barn Door Slab 36x80", "Each", 199.00),
    ("HD-2105", "Pocket Door Kit 30x80", "Each", 149.00),
    ("HD-2106", "Door Hardware Entry Set Satin Nickel", "Each", 89.00),
    ("HD-2107", "Interior Door Lever Set", "Each", 29.00),
    
    # MOLDING/TRIM
    ("HD-2201", "Primed MDF Baseboard 3-1/4 in x 8 ft", "Each", 12.98),
    ("HD-2202", "Primed MDF Casing 2-1/4 in x 8 ft", "Each", 9.98),
    ("HD-2203", "Primed MDF Crown Moulding 4-5/8 in x 8 ft", "Each", 16.98),
    ("HD-2204", "Chair Rail Moulding 3-1/4 in x 8 ft", "Each", 12.98),
    ("HD-2205", "Quarter Round Moulding 3/4 x 3/4 x 8 ft", "Each", 4.98),
    ("HD-2206", "Shoe Moulding 1/2 x 3/4 x 8 ft", "Each", 3.98),
    
    # FLOORING
    ("HD-5001", "Luxury Vinyl Plank 7x48 Click Lock", "Sq Ft", 2.69),
    ("HD-5002", "Luxury Vinyl Plank Waterproof 6x48", "Sq Ft", 3.49),
    ("HD-5003", "Laminate Flooring 8mm AC4", "Sq Ft", 1.99),
    ("HD-5004", "Engineered Hardwood 3/4 in", "Sq Ft", 4.99),
    ("HD-5005", "Vinyl Sheet Flooring", "Sq Ft", 1.79),
    
    # TILE
    ("HD-11001", "Ceramic Wall Tile 3x6 Subway White", "Sq Ft", 0.99),
    ("HD-11002", "Ceramic Floor Tile 12x12 Matte", "Sq Ft", 1.49),
    ("HD-11003", "Porcelain Floor Tile 12x24", "Sq Ft", 3.49),
    ("HD-11004", "Porcelain Large Format 24x48", "Sq Ft", 4.99),
    ("HD-11005", "Glass Mosaic Tile Sheet", "Sheet", 12.98),
    ("HD-11006", "Natural Stone Travertine 12x12", "Sq Ft", 5.99),
    ("HD-11007", "Pebble Tile Mesh Sheet", "Sheet", 14.98),
    
    # FAUCETS & FIXTURES
    ("HD-3001", "Delta Kitchen Faucet Pull Down Chrome", "Each", 169.00),
    ("HD-3002", "Moen Bathroom Faucet Centerset Chrome", "Each", 79.00),
    ("HD-3003", "Showerhead Rainfall Chrome", "Each", 59.00),
    ("HD-3004", "Shower Valve Trim Kit", "Each", 149.00),
    ("HD-3005", "Kitchen Soap Dispenser", "Each", 39.00),
    
    # LIGHTING
    ("HD-4001", "Vanity Light 3-Light Chrome", "Each", 89.00),
    ("HD-4002", "Recessed LED Downlight 6in", "Each", 24.98),
    ("HD-4003", "Pendant Light Kitchen Island", "Each", 129.00),
    ("HD-4004", "Chandelier Dining Room", "Each", 199.00),
    
    # HARDWARE
    ("HD-5001h", "Cabinet Pull 3in Satin Nickel", "Each", 3.98),
    ("HD-5002h", "Cabinet Knob Round Satin Nickel", "Each", 2.98),
    ("HD-5003h", "Door Stop Wall Mount", "Each", 2.98),
    ("HD-5004h", "Hinge Concealed 105deg", "Each", 4.98),
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
