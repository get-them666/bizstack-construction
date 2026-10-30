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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        response = requests.get(cart_url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
    except Exception as e:
        return f"Error retrieving webpage: {e}"

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["SKU", "Description", "Price", "UOM"])

    cart_items = soup.find_all(class_=["cart-item-row", "pip-price-package"])

    for item in cart_items:
        try:
            sku_element = item.find(attrs={"data-automation-id": "productSku"})
            sku = sku_element.text.replace("SKU #", "").strip() if sku_element else "UNKNOWN"

            desc_element = item.find(class_=["product-title", "item-description"])
            desc = desc_element.text.strip() if desc_element else "Generic Building Material"

            price_element = item.find(class_=["price__format", "item-price"])
            if not price_element:
                price_element = item.find(string=lambda t: t and '$' in str(t))
                if price_element:
                    price_raw = str(price_element).replace("$", "").strip()
                else:
                    continue
            else:
                price_raw = price_element.text.replace("$", "").strip()

            writer.writerow([f"HD-{sku}" if sku != "UNKNOWN" and not sku.startswith("HD-") else sku, desc, price_raw, "Each"])
        except Exception:
            continue

    return output.getvalue()
