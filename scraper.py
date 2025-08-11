import requests
import json
import os
from datetime import datetime
from bs4 import BeautifulSoup

# KKIOSK category URLs (can add more if needed)
KKIOSK_URLS = [
    "https://www.kkiosk.ch/collections/tabak",
    "https://www.kkiosk.ch/collections/e-zigaretten",
    "https://www.kkiosk.ch/collections/snacks"
]

DATA_FILE = "kkiosk_products.json"

# ---- HTTP helper ----
def get(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/115.0 Safari/537.36"
    }
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r

# ---- Scraper for all pages in category ----
def kkiosk_shopify_items_all_variants(base_url):
    products = []
    page = 1
    while True:
        url = f"{base_url}?page={page}"
        html = get(url).text
        soup = BeautifulSoup(html, "html.parser")
        product_cards = soup.select("div.grid-product")
        if not product_cards:
            break

        for card in product_cards:
            title_elem = card.select_one(".grid-product__title")
            price_elem = card.select_one(".grid-product__price")
            link_elem = card.select_one("a")
            if not title_elem or not price_elem or not link_elem:
                continue

            title = title_elem.get_text(strip=True)
            price = price_elem.get_text(strip=True)
            url = "https://www.kkiosk.ch" + link_elem.get("href")
            sku = link_elem.get("data-product-id") or url

            products.append({
                "sku": sku,
                "title": title,
                "price": price,
                "url": url
            })
        page += 1
    return products

# ---- Main comparison + notification ----
def run():
    all_products = []
    for url in KKIOSK_URLS:
        all_products.extend(kkiosk_shopify_items_all_variants(url))

    old_products = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            old_products = json.load(f)

    old_skus = {p["sku"]: p for p in old_products}
    new_skus = {p["sku"]: p for p in all_products}

    changes = []

    # Find new products
    for sku, prod in new_skus.items():
        if sku not in old_skus:
            changes.append(f"[NEW] kkiosk · {sku} · {prod['title']} · {prod['price']}")

    # Find removed products
    for sku, prod in old_skus.items():
        if sku not in new_skus:
            changes.append(f"[REMOVED] kkiosk · {sku} · {prod['title']} · {prod['price']}")

    if changes:
        print("\n".join(changes))
    else:
        print("No changes found.")

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(all_products, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    run()
