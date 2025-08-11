# scraper.py — kkiosk ALL SKUs (per-variant) monitor
# Emails on [NEW], [PRICE], [IMAGE]. Uses Shopify JSON (all pages).

import os, re, json, time, hashlib, smtplib
from email.mime.text import MIMEText
from urllib.parse import urlparse
from io import BytesIO
import requests
from PIL import Image

# ===== SMTP (from GitHub Secrets; defaults for local test) =====
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "you@example.com")
SMTP_PASS = os.getenv("SMTP_PASS", "app-password")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO   = os.getenv("EMAIL_TO", "you@example.com")  # comma-separated OK
# ==============================================================

STATE_FILE = "state.json"

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

def price_str(cents):
    return f"CHF {cents/100:.2f}" if cents is not None else "CHF —"

def hash_image_bytes(b):
    try:
        img=Image.open(BytesIO(b)).convert("RGB").resize((128,128))
        return hashlib.sha1(img.tobytes()).hexdigest()
    except Exception:
        return hashlib.sha1(b).hexdigest()

def get(url, headers=None, timeout=40):
    h=dict(DEFAULT_HEADERS)
    if headers: h.update(headers)
    r=requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r

def fetch_img_hash(url):
    if not url: return None
    try: return hash_image_bytes(get(url).content)
    except Exception: return None

def kkiosk_shopify_items_all_variants(collection_url: str):
    """Return one record per VARIANT (SKU). Paginates through collection JSON."""
    items=[]
    p=urlparse(collection_url)
    parts=[q for q in p.path.split("/") if q]
    handle = parts[parts.index("collections")+1] if "collections" in parts else parts[-1]
    base=f"{p.scheme}://{p.netloc}/collections/{handle}/products.json"
    page=1
    while True:
        data=get(f"{base}?limit=250&page={page}").json().get("products",[])
        if not data: break
        for prod in data:
            # Build image lookup by image_id for variant-specific images
            img_by_id={}
            for img in prod.get("images", []):
                src = img.get("src")
                if not src: continue
                if src.startswith("//"): src="https:"+src
                img_by_id[img.get("id")] = src
            product_img=None
            if prod.get("image") and prod["image"].get("src"):
                product_img=prod["image"]["src"]
                if product_img and product_img.startswith("//"):
                    product_img="https:"+product_img

            title=prod.get("title") or ""
            product_url=f"{p.scheme}://{p.netloc}/products/{prod.get('handle')}"
            for v in (prod.get("variants") or []):
                sku = (v.get("sku") or str(v.get("id"))).strip()
                try:
                    price_cents = int(float(v.get("price","0"))*100)
                except Exception:
                    price_cents = None
                image_url = img_by_id.get(v.get("image_id")) or product_img
                name = f"{title} {v.get('title') or ''}".strip()
                items.append({
                    "site":"kkiosk",
                    "sku": sku or f"variant_{v.get('id')}",
                    "name": name,
                    "price_cents": price_cents,
                    "image_url": image_url,
                    "url": product_url,
                })
        page+=1
    return items

def send_email(subj, body):
    to_list=[e.strip() for e in EMAIL_TO.split(",") if e.strip()]
    msg=MIMEText(body,"plain","utf-8"); msg["Subject"]=subj; msg["From"]=EMAIL_FROM; msg["To"]=", ".join(to_list)
    s=smtplib.SMTP(SMTP_HOST, SMTP_PORT); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
    s.sendmail(EMAIL_FROM, to_list, msg.as_string()); s.quit()
    print("Email sent to:", ", ".join(to_list))

def run():
    state=load_state(); changes=[]; now=int(time.time())

    for url in KKIOSK_URLS:
        for p in kkiosk_shopify_items_all_variants(url):
            key=f"{p['site']}:{p['sku']}"
            old=state.get(key); img_hash=fetch_img_hash(p["image_url"])

            if not old:
                changes.append(f"[NEW] {p['site']} · {p['sku']} · {p['name']} · {price_str(p['price_cents'])} · {p['url']}")
                state[key]={**p,"image_hash":img_hash,"last_seen":now}
            else:
                # price change
                if old.get("price_cents")!=p["price_cents"] and p["price_cents"] is not None:
                    changes.append(f"[PRICE] {p['site']} · {p['sku']} · {p['name']} · "
                                   f"{price_str(old.get('price_cents'))} → {price_str(p['price_cents'])} · {p['url']}")
                    old["price_cents"]=p["price_cents"]
                # image change
                if img_hash and img_hash!=old.get("image_hash"):
                    changes.append(f"[IMAGE] {p['site']} · {p['sku']} · {p['name']} · image changed · {p['url']}")
