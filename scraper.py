import os, re, json, time, hashlib, smtplib
from email.mime.text import MIMEText
from urllib.parse import urlparse
from io import BytesIO
import requests
from bs4 import BeautifulSoup
from PIL import Image

# ====== SMTP SETTINGS (edit in repo secrets or here for testing) ======
SMTP_HOST = os.getenv("SMTP_HOST") or "smtp.example.com"
SMTP_PORT = int(os.getenv("SMTP_PORT") or 587)
SMTP_USER = os.getenv("SMTP_USER") or "alerts@example.com"
SMTP_PASS = os.getenv("SMTP_PASS") or "password"
EMAIL_FROM = os.getenv("EMAIL_FROM") or "alerts@example.com"
EMAIL_TO   = os.getenv("EMAIL_TO")   or "boris.buschlebello@pmi.com"
# ======================================================================

STATE_FILE = "state.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; TabakMonitor/1.0)"}

# Category targets
TARGETS = [
    # kkiosk (Shopify)
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/cigarettes-1"),
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/e-cigarettes"),
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/einweg-e-zigaretten"),
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/snus"),
    ("kkiosk_shopify", "https://tabak.kkiosk.ch/collections/tabak"),
    # Coop (HTML)
    ("coop_html", "https://www.coop.ch/en/kiosk/tobacco-products/c/m_5586"),
    ("coop_html", "https://www.coop.ch/en/kiosk/tobacco-products/cigarettes/c/m_4209"),
    ("coop_html", "https://www.coop.ch/en/kiosk/tobacco-products/e-cigarettes-vapes/c/m_5898"),
    ("coop_html", "https://www.coop.ch/en/kiosk/tobacco-products/snus/c/m_5896"),
]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)

def price_to_cents(text):
    if not text: return None
    t = text.replace("CHF", "").replace("Fr.", "").strip()
    m = re.search(r"(\d+)[\.,](\d{2})", t)
    if m: return int(m.group(1))*100 + int(m.group(2))
    m = re.search(r"\d+", t)
    return int(m.group(0))*100 if m else None

def get(url, session=None):
    s = session or requests.Session()
    r = s.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    return r

def hash_image_bytes(b):
    try:
        img = Image.open(BytesIO(b)).convert("RGB").resize((128, 128))
        return hashlib.sha1(img.tobytes()).hexdigest()
    except Exception:
        return hashlib.sha1(b).hexdigest()

def fetch_img_hash(url):
    if not url: return None
    try:
        return hash_image_bytes(get(url).content)
    except: return None

def kkiosk_shopify_items(collection_url):
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
                    price = min(int(float(v.get("price", "0"))*100) for v in variants)
                except: price = None
            img = None
            if prod.get("image"):
                img = prod["image"].get("src")
                if img and img.startswith("//"):
                    img = "https:" + img
            sku = (variants[0].get("sku") if variants else prod.get("handle")) or str(prod.get("id"))
            items.append({
                "sku": sku, "name": prod.get("title"), "price_cents": price,
                "image_url": img, "url": f"{p.scheme}://{p.netloc}/products/{prod.get('handle')}"
            })
        page += 1
    return items

def coop_items(url):
    html = get(url).text
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(".product-tile, .product, li[data-sku], .product-card")
    out = []
    for c in cards:
        name_el = c.select_one(".product-name, .name, .title, [data-name]")
        price_el = c.select_one(".price, .product-price, [data-price]")
        img_el = c.select_one("img")
        name = name_el.get_text(strip=True) if name_el else None
        price_cents = price_to_cents(price_el.get_text(strip=True) if price_el else "")
        sku = c.get("data-sku") or name
        image_url = None
        if img_el and img_el.has_attr("src"):
            image_url = img_el["src"]
            if image_url.startswith("//"):
                image_url = "https:" + image_url
        out.append({
            "sku": sku, "name": name, "price_cents": price_cents,
            "image_url": image_url, "url": url
        })
    return out

def send_email(subj, body):
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM, EMAIL_TO]):
        print("Email not configured. Changes:\n" + body)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subj
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
    s.starttls()
    s.login(SMTP_USER, SMTP_PASS)
    s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    s.quit()

def run():
    state = load_state()
    changes = []

    for site, url in TARGETS:
        products = kkiosk_shopify_items(url) if site == "kkiosk_shopify" else coop_items(url)
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
