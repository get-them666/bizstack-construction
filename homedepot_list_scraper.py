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

    # Try to find JSON data in script tags
    # Look for list items
    patterns = [
        r'"itemId"\s*:\s*"([^"]+)"',
        r'"productId"\s*:\s*"([^"]+)"',
    ]
    
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    
    # Try different approaches
    for script in soup.find_all('script'):
        content = script.string or ''
        if 'list' in content.lower() and ('price' in content.lower() or 'item' in content.lower()):
            # Try to extract JSON objects
            # Look for arrays of products
            for m in re.finditer(r'\{[^{}]*"price"[^{}]*\}', content):
                try:
                    obj = json.loads(m.group())
                    if obj.get('price') or obj.get('currentPrice'):
                        items.append(obj)
                except:
                    pass
    
    # Also look for product cards
    for card in soup.find_all(['div', 'li'], class_=re.compile(r'product|item|card', re.I)):
        text = card.get_text()
        price_m = re.search(r'\$\s*([0-9,]+\.?[0-9]*)', text)
        if price_m:
            # Try to find sku/id
            sku = card.get('data-itemid') or card.get('data-productid') or 'UNKNOWN'
            if sku == 'UNKNOWN':
                sku_m = re.search(r'([0-9]{6,})', str(card))
                if sku_m:
                    sku = sku_m.group(1)
            # Get title/description
            title = card.find(['h3', 'h4', 'a'], class_=re.compile(r'title|name', re.I))
            desc = title.get_text(strip=True) if title else text[:100].strip()
            writer.writerow([f"HD-{sku}", desc, price_m.group(1).replace(',', ''), "Each"])
    
    result = output.getvalue()
    if result.count('\n') <= 1:
        return "Error: Could not extract items from this list. The page structure may require JavaScript rendering or authentication."
    return result


if __name__ == '__main__':
    print(scrape_homedepot_list('https://www.homedepot.com/list/view/details/shared/4c5f3bf0-d483-11f1-9806-f59836140ecb'))
