# scraper.py — kkiosk-only monitor (price change, new SKU, image change)
# Uses Shopify JSON (all pages), emails via SMTP.

import os, re, json, time, hashlib, smtplib
from email.mime.text import MIMEText
from urllib.parse import urlparse
from io import BytesIO
import requests
from PIL import Image

# ===== SMTP (use your GitHub Secrets) =====
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "you@example.com")
SMTP_PASS = os.getenv("SMTP_PASS", "app-password")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO   = os.getenv("EMAIL_TO", "you@example.com")  # comma-separated OK
# =========================================

STATE_FILE = "state.json"

# kkiosk categories (add/remove as you like)
KKIOSK_URLS = [
    "https://tabak.kkiosk.ch/collections/cigarettes-1",
    "https://tabak.kkiosk.ch/collections/e-cigarettes",
    "https://tabak.kkiosk.ch/collections/einweg-e-zigaretten",
    "https://tabak.kkiosk.ch/collections/snus",
    "https://tabak.kkiosk.ch/collections/tabak",
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

def load_state():
    return json.load(open(STATE_FILE,"r",encoding="utf-8")) if os.path.exists(STATE_FILE) else {}

def save_state(state):
    json.dump(state, open(STATE_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=2, sort_keys=True)

def price_to_cents_str(s):
    return f"CHF {s/100:.2f}" if s is not None else "CHF —"

def hash_image_bytes(b: bytes):
    try:
        from PIL import Image
        img = Image.open(BytesIO(b)).convert("RGB").resize((128,128))
        return hashlib.sha1(img.tobytes()).hexdigest()
    except Exception:
        return hashlib.sha1(b).hexdigest()

def get(url: str, headers: dict = None, timeout: int = 40):
    h = dict(DEFAULT_HEADERS)
    if headers: h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r

def fetch_img_hash(url: str):
    if not url: return None
    try:
        return hash_image_bytes(get(url).content)
    except Exception:
        return None

def kkiosk_shopify_items(collection_url: str):
    """Scrape ALL products from a kkiosk collection via Shopify JSON pagination."""
    items = []
    p = urlparse(collection_url)
    parts = [q for q in p.path.split("/") if q]
    handle = parts[parts.index("collections")+1] if "collections" in parts else parts[-1]
    base = f"{p.scheme}://{p.netloc}/collections/{handle}/products.json"
    page = 1
    while True:
        data = get(f"{base}?limit=250&page={page}").json().get("products", [])
        if not data: break
        for prod in data:
            variants = prod.get("variants", [])
            price = None
            if variants:
                try:
                    price = min(int(float(v.get("price","0"))*100) for v in variants)
                except Exception:
                    price = None
            img = None
            if prod.get("image"):
                img = prod["image"].get("src")
                if img and img.startswith("//"): img = "https:"+img
            sku = (variants[0].get("sku") if variants else prod.get("handle")) or str(prod.get("id"))
            items.append({
                "site": "kkiosk",
                "sku": sku,
                "name": prod.get("title"),
                "price_cents": price,
                "image_url": img,
                "url": f"{p.scheme}://{p.netloc}/products/{prod.get('handle')}",
            })
        page += 1
    return items

def send_email(subj: str, body: str):
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

def run():
    state = load_state()
    changes = []

    for url in KKIOSK_URLS:
        for p in kkiosk_shopify_items(url):
            key = f"{p['site']}:{p['sku']}"
            old = state.get(key)
            img_hash = fetch_img_hash(p["image_url"])
            now = int(time.time())
            if not old:
                changes.append(f"[NEW] {p['site']} · {p['sku']} · {p['name']} · {price_to_cents_str(p['price_cents'])}")
                state[key] = {
                    **p, "image_hash": img_hash, "last_seen": now
                }
            else:
                # price change
                if old.get("price_cents") != p["price_cents"] and p["price_cents"] is not None:
                    changes.append(f"[PRICE] {p['site']} · {p['sku']} · {p['name']} · {price_to_cents_str(old.get('price_cents'))} → {price_to_cents_str(p['price_cents'])}")
                    old["price_cents"] = p["price_cents"]
                # image change
                if img_hash and img_hash != old.get("image_hash"):
                    changes.append(f"[IMAGE] {p['site']} · {p['sku']} · {p['name']} · image changed")
                    old["image_url"] = p["image_url"]
                    old["image_hash"] = img_hash
                old["last_seen"] = now
                state[key] = old

    if changes:
        body = "\n".join(changes)
        print(body)
        send_email("kkiosk watch: changes detected", body)
    else:
        print("No changes detected.")
    save_state(state)

if __name__ == "__main__":
    run()
