# scraper.py — kkiosk + (Ploom/Vuse/VELO/glo with TEXT changes)
# Emails on [NEW], [PRICE], [IMAGE], [TEXT]. Designed for GitHub Actions.

import os, re, json, time, hashlib, smtplib
from email.mime.text import MIMEText
from urllib.parse import urlparse
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from PIL import Image

# ===== SMTP (read from Secrets) =====
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "you@example.com")
SMTP_PASS = os.getenv("SMTP_PASS", "app-password")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO   = os.getenv("EMAIL_TO", "you@example.com")   # comma-separated OK
# ====================================

STATE_FILE = "state.json"

# ------- kkiosk (unchanged, per-variant full coverage) -------
KKIOSK_URLS = [
    "https://tabak.kkiosk.ch/collections/cigarettes-1",
    "https://tabak.kkiosk.ch/collections/e-cigarettes",
    "https://tabak.kkiosk.ch/collections/einweg-e-zigaretten",
    "https://tabak.kkiosk.ch/collections/snus",
    "https://tabak.kkiosk.ch/collections/tabak",
]

# ------- Other CH sites (category pages) -------
# You can tweak selectors if needed later; defaults work for many storefronts.
OTHER_TARGETS = [
    # (site, category_url)
    ("ploom", "https://www.ploom.ch/en/shop/sticks"),
    ("ploom", "https://www.ploom.ch/en/shop/devices"),
    ("vuse",  "https://www.vuse.ch/en/ezigaretten/einweg-e-zigaretten"),
    ("vuse",  "https://www.vuse.ch/en/ezigaretten/pods"),
    ("velo",  "https://www.velo.com/ch/en/velo"),
    ("glo",   "https://www.discoverglo.ch/en/shop"),
    ("glo",   "https://www.discoverglo.ch/en/shop/neo-sticks"),
]

# Product card selectors to try (broad and resilient)
CARD_SEL = ".product-card, .product-tile, .product, li[data-sku], article.product, div.product-item, div.product-card"
NAME_SEL = ".product-name, .name, .title, h3, h2, [data-name]"
PRICE_SEL = ".price, .product-price, .currency, [data-price], .price__current, .product__price"
IMG_SEL   = "img"

# Real browser headers (helps avoid 403s)
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

# --------------------- Utils ---------------------
def load_state():
    return json.load(open(STATE_FILE,"r",encoding="utf-8")) if os.path.exists(STATE_FILE) else {}

def save_state(state):
    json.dump(state, open(STATE_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=2, sort_keys=True)

def price_str(cents):
    return f"CHF {cents/100:.2f}" if cents is not None else "CHF —"

def price_to_cents(text: str):
    if not text: return None
    t = text.replace("CHF","").replace("Fr.","").replace("CHF.","").strip()
    m = re.search(r"(\d+)[\.,](\d{2})", t)
    if m: return int(m.group(1))*100 + int(m.group(2))
    m = re.search(r"\d+", t)
    return int(m.group(0))*100 if m else None

def hash_bytes_as_image(b: bytes):
    try:
        img = Image.open(BytesIO(b)).convert("RGB").resize((128,128))
        return hashlib.sha1(img.tobytes()).hexdigest()
    except Exception:
        return hashlib.sha1(b).hexdigest()

def get(url: str, headers: dict = None, timeout: int = 45):
    h = dict(DEFAULT_HEADERS)
    if headers: h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r

def fetch_img_hash(url: str):
    if not url: return None
    try: return hash_bytes_as_image(get(url).content)
    except Exception: return None

def card_text_hash(el):
    # visible text of the product card -> normalized and hashed
    text = el.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

# --------------------- kkiosk (Shopify JSON) ---------------------
def kkiosk_shopify_items_all_variants(collection_url: str):
    items=[]
    p = urlparse(collection_url)
    parts=[q for q in p.path.split("/") if q]
    handle = parts[parts.index("collections")+1] if "collections" in parts else parts[-1]
    base = f"{p.scheme}://{p.netloc}/collections/{handle}/products.json"
    page=1
    while True:
        data = get(f"{base}?limit=250&page={page}").json().get("products",[])
        if not data: break
        for prod in data:
            # map image_id -> src
            img_by_id = {}
            for img in prod.get("images", []):
                src = img.get("src")
                if not src: continue
                if src.startswith("//"): src = "https:"+src
                img_by_id[img.get("id")] = src
            product_img = None
            if prod.get("image") and prod["image"].get("src"):
                product_img = prod["image"]["src"]
                if product_img.startswith("//"): product_img = "https:"+product_img

            title = prod.get("title")
            url   = f"{p.scheme}://{p.netloc}/products/{prod.get('handle')}"
            for v in (prod.get("variants") or []):
                sku = (v.get("sku") or str(v.get("id"))).strip()
                try:
                    cents = int(float(v.get("price","0"))*100)
                except Exception:
                    cents = None
                image_url = img_by_id.get(v.get("image_id")) or product_img
                items.append({
                    "site":"kkiosk",
                    "sku": sku,
                    "name": f"{title} {v.get('title') or ''}".strip(),
                    "price_cents": cents,
                    "image_url": image_url,
                    "url": url,
                    "text_hash": None,     # not used for kkiosk
                })
        page += 1
    return items

# --------------------- Other sites (HTML + TEXT) ---------------------
def scrape_category_with_text(url: str, site: str):
    """Return product rows with name/price/image + a TEXT hash per card."""
    html = get(url).text
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(CARD_SEL)
    out=[]
    for c in cards:
        # name
        name_el = None
        for q in [s.strip() for s in NAME_SEL.split(",") if s.strip()]:
            name_el = c.select_one(q)
            if name_el: break
        name = name_el.get_text(strip=True) if name_el else None

        # price
        price_el = None
        for q in [s.strip() for s in PRICE_SEL.split(",") if s.strip()]:
            price_el = c.select_one(q)
            if price_el: break
        price_cents = price_to_cents(price_el.get_text(strip=True)) if price_el else None

        # image
        img_el = c.select_one(IMG_SEL)
        image_url = None
        if img_el and img_el.has_attr("src"):
            image_url = img_el["src"]
            if image_url.startswith("//"): image_url = "https:"+image_url

        # key (SKU if present, else normalized name)
        key = c.get("data-sku") or c.get("data-id") or (name or "")
        if not key:
            # fall back to a hash of the card to keep it stable
            key = "card_" + card_text_hash(c)[:12]

        out.append({
            "site": site,
            "sku": key.strip(),
            "name": name,
            "price_cents": price_cents,
            "image_url": image_url,
            "url": url,                      # report the category URL
            "text_hash": card_text_hash(c),  # detect any textual change
        })
    return out

# --------------------- Email ---------------------
def send_email(subj, body):
    to_list = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subj
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_list)
    try:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(EMAIL_FROM, to_list, msg.as_string())
        s.quit()
        print("Email sent to:", ", ".join(to_list))
    except Exception as e:
        print("Email send failed:", e, "\n", body)

# --------------------- Main ---------------------
def run():
    state = load_state()
    changes = []
    now = int(time.time())

    # kkiosk (full per-variant)
    for url in KKIOSK_URLS:
        for p in kkiosk_shopify_items_all_variants(url):
            key = f"{p['site']}:{p['sku']}"
            old = state.get(key)
            img_hash = fetch_img_hash(p["image_url"])
            if not old:
                changes.append(f"[NEW] {p['site']} · {p['sku']} · {p['name']} · {price_str(p['price_cents'])} · {p['url']}")
                state[key] = {**p, "image_hash": img_hash, "last_seen": now}
            else:
                if old.get("price_cents") != p["price_cents"] and p["price_cents"] is not None:
                    changes.append(f"[PRICE] {p['site']} · {p['sku']} · {p['name']} · {price_str(old.get('price_cents'))} → {price_str(p['price_cents'])} · {p['url']}")
                    old["price_cents"] = p["price_cents"]
                if img_hash and img_hash != old.get("image_hash"):
                    changes.append(f"[IMAGE] {p['site']} · {p['sku']} · {p['name']} · image changed · {p['url']}")
                    old["image_url"] = p["image_url"]
                    old["image_hash"] = img_hash
                old["last_seen"] = now
                state[key] = old

    # Other sites (category URLs, detect TEXT too)
    for site, url in OTHER_TARGETS:
        products = scrape_category_with_text(url, site)
        for p in products:
            key = f"{site}:{p['sku']}"
            old = state.get(key)
            img_hash = fetch_img_hash(p["image_url"])
            if not old:
                changes.append(f"[NEW] {site} · {p['sku']} · {p['name']} · {price_str(p['price_cents'])} · {url}")
                state[key] = {**p, "image_hash": img_hash, "last_seen": now}
            else:
                # price
                if old.get("price_cents") != p["price_cents"] and p["price_cents"] is not None:
                    changes.append(f"[PRICE] {site} · {p['sku']} · {p['name']} · {price_str(old.get('price_cents'))} → {price_str(p['price_cents'])} · {url}")
                    old["price_cents"] = p["price_cents"]
                # image
                if img_hash and img_hash != old.get("image_hash"):
                    changes.append(f"[IMAGE] {site} · {p['sku']} · {p['name']} · image changed · {url}")
                    old["image_url"] = p["image_url"]
                    old["image_hash"] = img_hash
                # TEXT (visible text within product card)
                if p.get("text_hash") and p["text_hash"] != old.get("text_hash"):
                    changes.append(f"[TEXT] {site} · {p['sku']} · {p['name']} · text changed · {url}")
                    old["text_hash"] = p["text_hash"]
                old["last_seen"] = now
                state[key] = old

    if changes:
        send_email("Tabak watch: changes detected", "\n".join(changes))
        print("\n".join(changes))
    else:
        print("No changes detected.")

    save_state(state)

if __name__ == "__main__":
    run()
