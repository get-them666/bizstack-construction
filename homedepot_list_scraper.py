"""Scrape Home Depot shared list pages."""
import requests
from bs4 import BeautifulSoup
import csv
import io
import json
import re


def scrape_homedepot_list(list_url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(list_url, headers=headers, timeout=30)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return f"Error: {e}"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["SKU", "Description", "Price", "UOM"])

    soup = BeautifulSoup(html, 'html.parser')
    
    # Look for all script tags with JSON
    for script in soup.find_all('script', type='application/json'):
        try:
            data = json.loads(script.string)
            # Traverse to find products/items
            def find_items(obj, path=''):
                if isinstance(obj, dict):
                    # Check if this looks like a product
                    if ('price' in obj or 'currentPrice' in obj or 'unitPrice' in obj) and ('itemId' in obj or 'productId' in obj or 'sku' in obj or 'id' in obj):
                        items_found.append(obj)
                    for k, v in obj.items():
                        find_items(v, path + '.' + k)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        find_items(item, path + f'[{i}]')
            items_found = []
            find_items(data)
            if items_found:
                for prod in items_found:
                    sku = prod.get('itemId') or prod.get('productId') or prod.get('sku') or prod.get('id') or prod.get('modelId') or 'UNKNOWN'
                    desc = prod.get('productName') or prod.get('name') or prod.get('description') or prod.get('title') or 'Item'
                    price = None
                    if isinstance(prod.get('price'), dict):
                        price = prod['price'].get('value') or prod['price'].get('currentPrice') or prod['price'].get('amount')
                    elif prod.get('price'):
                        price = prod.get('price')
                    elif isinstance(prod.get('currentPrice'), dict):
                        price = prod['currentPrice'].get('value')
                    elif prod.get('currentPrice'):
                        price = prod.get('currentPrice')
                    elif prod.get('unitPrice'):
                        price = prod.get('unitPrice')
                    if price:
                        try:
                            # Clean price
                            price_str = str(price).replace('$', '').replace(',', '')
                            writer.writerow([f"HD-{sku}" if not str(sku).startswith('HD-') else str(sku), str(desc)[:200], price_str, "Each"])
                        except:
                            pass
                result = output.getvalue()
                if result.count('\n') > 1:
                    return result
        except Exception as e:
            continue
    
    # Also look for embedded JSON in script tags
    for script in soup.find_all('script'):
        content = script.string or ''
        if '"items"' in content or '"listItems"' in content:
            # Try to extract with regex
            for m in re.finditer(r'"(?:itemId|productId|sku)"\s*:\s*"([^"]+)".*?"(?:productName|name)"\s*:\s*"([^"]+)".*?"(?:price|currentPrice)"[^0-9]*([0-9,.]+)', content, re.S):
                writer.writerow([f"HD-{m.group(1)}", m.group(2), m.group(3).replace(',', ''), "Each"])
            for m in re.finditer(r'\$([0-9]+\.[0-9]{2})', content):
                pass  # fallback
    
    result = output.getvalue()
    if result.count('\n') <= 1:
        return "Error: Could not extract items from this list. Try exporting from Home Depot or use the comprehensive price list instead."
    return result


if __name__ == '__main__':
    print(scrape_homedepot_list('https://www.homedepot.com/list/view/details/shared/4c5f3bf0-d483-11f1-9806-f59836140ecb'))
