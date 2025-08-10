# scraper.py — kkiosk + Coop monitor (price change, new SKU, image change)
# Sends email via SMTP (Gmail or any SMTP). Designed for GitHub Actions.

import os, re, json, time, hashlib, smtplib
from email.mime.text import MIMEText
from urllib.parse import urlparse, urljoin
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from PIL import Image

# ====== SMTP SETTINGS ======
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "you@example.com")
SMTP_PASS = os.getenv("SMTP_PASS", "app-password-or-password")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO   = os.getenv("EMAIL_TO", "you@example.com")  # comma-separated list
# ===========================

STATE_FILE = "state.json"

# Real browser headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Connection": "close",
}

# Category targets
TARGETS = [
    # kkiosk (Shopify JSON)
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/cigarettes-1"),
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/e-cigarettes"),
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/einweg-e-zigaretten"),
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/snus"),
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/tabak"),
    # Coop (HTML multi-page)
    ("coop_html", "https://www.coop.ch/en/kiosk/tobacco-products/c/m_5586"),
    ("coop_html", "https://www.coop.ch/en/kiosk/tobacco-products/cigarettes/c/m_4209"),
    ("coop_html", "https://www.coop.ch/en/kiosk/tobacco-products/e-cigarettes-vapes/c/m_5898"),
    ("coop_html", "https://www.coop.ch/en/kiosk/tobacco-products/snus/c/m_5896"),
]

# ------------------------- helpers -------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)

def price_to_cents(text: str):
    if not text:
        return None
    t = text.replace("CHF", "").replace("Fr.", "").strip()
    m = re.search(r"(\d+)[\.,](\d{2})", t)
    if m:
        return int(m.group(1)) * 100 + int(m.group(2))
    m = re.search(r"\d+", t)
    return int(m.group(0)) * 100 if m else None

def hash_image_bytes(b: bytes):
    try:
        img = Image.open(BytesIO(b)).convert("RGB").resize((128, 128))
        return hashlib.sha1(img.tobytes()).hexdigest()
    except Exception:
        return hashlib.sha1(b).hexdigest()

def get(url: str, headers: dict = None, timeout: int = 40):
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r

def fetch_img_hash(url: str):
    if not url:
        return None
    try:
        return hash_image_bytes(get(url).content)
    except Exception:
        return None

# ------------------------- site scrapers -------------------------
def kkiosk_shopify_items(collection_url: str):
    """Use Shopify JSON API: /collections/<handle>/products.json with pagination."""
    items = []
    p = urlparse(collection_url)
    parts = [q for q in p.path.split("/") if q]
    handle = parts[parts.index("collections") + 1] if "collections" in parts else parts[-1]
    base = f"{p.scheme}://{p.netloc}/collections/{handle}/products.json"
    page = 1
    while True:
        resp = get(f"{base}?limit=250&page={page}")
        data = resp.json().get("products", [])
        if not data:
            break
        for prod in data:
            variants = prod.get("variants", [])
            price = None
            if variants:
                try:
                    price = min(int(float(v.get("price", "0")) * 100) for v in variants)
                except Exception:
                    price = None
            img = None
            if prod.get("image"):
                img = prod["image"].get("src")
                if img and img.startswith("//"):
                    img = "https:" + img
            sku = (variants[0].get("sku") if variants else prod.get("handle")) or str(prod.get("id"))
            items.append({
                "sku": sku,
                "name": prod.get("title"),
                "price_cents": price,
                "image_url": img,
                "url": f"{p.scheme}://{p.netloc}/products/{prod.get('handle')}",
            })
        page += 1
    return items

def coop_items_all_pages(start_url: str):
    """Scrape all pages in a Coop category."""
    items = []
    page_num = 1
    base_url = start_url.split("?")[0]
    while True:
        url = f"{base_url}?page={page_num}"
        html = get(url).text
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(".product-tile, .product, li[data-sku], .product-card")
        if not cards:
            break
        for c in cards:
            name_el = c.select_one(".product-name, .name, .title, [data-name]")
            price_el = c.select_one(".price, .product-price, [data-price]")
            img_el = c.select_one("img")
            name = name_el.get_text(strip=True) if name_el else None
            price_cents = price_to_cents(price_el.get_text(strip=True) if price_el else "")
            sku = c.get("data-sku") or name or ""
            image_url = None
            if img_el and img_el.has_attr("src"):
                image_url = img_el["src"]
                if image_url.startswith("//"):
                    image_url = "https:" + image_url
            items.append({
                "sku": sku,
                "name": name,
                "price_cents": price_cents,
                "image_url": image_url,
                "url": url,
            })
        page_num += 1
    return items

# ------------------------- email -------------------------
def send_email(subj: str, body: str):
    try:
        to_list = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subj
        msg["From"] = EMAIL_FROM
        msg["To"] = ", ".join(to_list)
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(EMAIL_FROM, to_list, msg.as_string())
        s.quit()
        print(f"Email sent to: {', '.join(to_list)}")
    except Exception as e:
        print(f"Email send failed: {e}\n{body}")

# ------------------------- main -------------------------
def run():
    state = load_state()
    changes = []

    for site, url in TARGETS:
        if site == "kkiosk_shopify":
            products = kkiosk_shopify_items(url)
        elif site == "coop_html":
            products = coop_items_all_pages(url)
        else:
            continue

        for p in products:
            key = f"{site}:{p['sku']}"
            old = state.get(key)
            img_hash = fetch_img_hash(p["image_url"])
            now = int(time.time())
            new_entry = {
                "site": site,
                "sku": p["sku"],
                "name": p["name"],
                "price_cents": p["price_cents"],
                "image_url": p["image_url"],
                "image_hash": img_hash,
                "url": p["url"],
                "last_seen": now
            }
            if not old:
                changes.append(f"[NEW] {site} · {p['sku']} · {p['name']} · CHF {p['price_cents']/100 if p['price_cents'] else '—'}")
                state[key] = new_entry
            else:
                if old.get("price_cents") != p["price_cents"] and p["price_cents"] is not None:
                    changes.append(f"[PRICE] {site} · {p['sku']} · {p['name']} · CHF {old.get('price_cents',0)/100} → CHF {p['price_cents']/100}")
                    old["price_cents"] = p["price_cents"]
                if img_hash and img_hash != old.get("image_hash"):
                    changes.append(f"[IMAGE] {site} · {p['sku']} · {p['name']} · image changed")
                    old["image_url"] = p["image_url"]
                    old["image_hash"] = img_hash
                old["last_seen"] = now
                state[key] = old

    if changes:
        body = "\n".join(changes)
        print(body)
        send_email("Tabak watch: changes detected", body)
    else:
        print("No changes detected.")

    save_state(state)

if __name__ == "__main__":
    run()
