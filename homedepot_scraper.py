"""Home Depot cart/list scraper for price sheets."""
import os
import csv
import io
from datetime import datetime

import requests
from bs4 import BeautifulSoup


HD_API_KEY = os.getenv("HOMEDEPOT_API_KEY")
HD_AFFILIATE_ID = os.getenv("HOMEDEPOT_AFFILIATE_ID")
STORE_ID = os.getenv("HOMEDEPOT_STORE_ID", "4605")
MARKUP_FACTOR = float(os.getenv("HOMEDEPOT_MARKUP_FACTOR", "1.2321"))


def scrape_homedepot_cart_to_csv(cart_url: str) -> str:
    """
    Uses BeautifulSoup to extract active items, descriptions, and raw base costs 
    from a saved or exported Home Depot cart/list HTML layout.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        response = requests.get(cart_url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
    except Exception as e:
        return f"Error retrieving webpage: {e}"

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["SKU", "Description", "Price", "UOM"])

    cart_items = soup.find_all(class_=["cart-item-row", "pip-price-package", "product-item", "item"])
    if not cart_items:
        cart_items = soup.find_all(attrs={"data-testid": True}) or []

    count = 0
    for item in cart_items:
        try:
            sku_element = item.find(attrs={"data-automation-id": "productSku"}) or item.find(attrs={"data-productid": True}) or item.find(attrs={"id": lambda x: x and "sku" in str(x).lower()})
            sku = "UNKNOWN"
            if sku_element:
                sku_text = sku_element.get_text(strip=True)
                sku = sku_text.replace("SKU #", "").replace("SKU:", "").strip()
            if sku == "UNKNOWN" or not sku:
                # try to find sku in data attributes
                for attr in ['data-itemid', 'data-product-id', 'data-sku']:
                    if item.get(attr):
                        sku = str(item.get(attr)).strip()
                        break

            desc_element = item.find(class_=["product-title", "item-description", "product-name"]) or item.find('a', class_=lambda x: x and 'title' in str(x).lower())
            desc = "Generic Building Material"
            if desc_element:
                desc = desc_element.get_text(strip=True)

            price_raw = None
            # Try various price selectors
            price_element = item.find(class_=["price__format", "item-price", "price", "product-price"]) or item.find(attrs={"data-automation-id": lambda x: x and "price" in str(x).lower()})
            if price_element:
                price_raw = price_element.get_text(strip=True)
            if not price_raw:
                # look for any $ sign
                import re
                m = re.search(r'\$\s*([0-9,]+\.?[0-9]*)', item.get_text())
                if m:
                    price_raw = m.group(0)
            if not price_raw:
                continue

            # Clean price
            import re
            price_clean = re.sub(r'[^\d.,]', '', price_raw)
            if not price_clean or price_clean == '0' or price_clean == '00':
                continue

            writer.writerow([f"HD-{sku}" if sku != "UNKNOWN" and not str(sku).startswith("HD-") else sku, desc, price_clean, "Each"])
            count += 1
        except Exception:
            continue

    result = output.getvalue()
    if count == 0 and result.count('\n') <= 1:
        return f"Error: No items found. Home Depot may have changed their page structure or requires login. Try exporting a CSV from your cart or use the price sheet upload instead."
    return result
