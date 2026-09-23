"""Synthetic Healing-Fist-like store data for dev and eval. Deterministic (seeded).

Real data path: `sync_shopify.py` (fills the same tables from Shopify Admin API) — see README.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta

from sqlalchemy import insert

from . import db

SEED = 20260901
START = date(2026, 3, 1)
END = date(2026, 8, 31)

PRODUCTS = [
    # sku, name, category, price, cost
    ("HF-TP-MINT", "Remineralizing Tooth Powder, Mint", "oral_care", 24.0, 6.5),
    ("HF-TP-CHAR", "Remineralizing Tooth Powder, Charcoal", "oral_care", 24.0, 6.8),
    ("HF-MISWAK-3", "Miswak Sticks, 3-pack", "oral_care", 14.0, 3.2),
    ("HF-SPRAY", "Herbal Mouth Spray", "oral_care", 18.0, 4.9),
    ("HF-BRUSH-4", "Bamboo Toothbrush, 4-pack", "oral_care", 12.0, 2.6),
    ("HF-SCRAPER", "Copper Tongue Scraper", "oral_care", 9.0, 1.9),
    ("HF-BUNDLE", "Fluoride-Free Starter Bundle", "bundle", 49.0, 14.0),
    ("HF-TISSUE", "Bamboo Facial Tissue, 6-box", "home", 22.0, 7.5),
]

DOCS = [
    ("Remineralizing Tooth Powder, Mint", "product",
     "Ingredients: calcium carbonate, baking soda, bentonite clay, xylitol, peppermint essential oil. Fluoride-free, "
     "no SLS, no glycerin. Use: wet brush, dip into powder, brush two minutes twice daily. 60g jar lasts about 3 months."),
    ("Remineralizing Tooth Powder, Charcoal", "product",
     "Ingredients: calcium carbonate, activated coconut charcoal, bentonite clay, xylitol, spearmint essential oil. "
     "Fluoride-free. Charcoal may stain grout; rinse sink after use. Not recommended with braces."),
    ("Miswak Sticks, 3-pack", "product",
     "Natural Salvadora persica chewing stick. Peel 1cm of bark, chew the tip until bristles form, brush without paste. "
     "Trim the tip every 2 to 3 days. Store dry."),
    ("Herbal Mouth Spray", "product",
     "Ingredients: purified water, xylitol, aloe vera, clove oil, peppermint oil. Alcohol-free. 30ml bottle, about 200 sprays. "
     "Shake before use. Not for children under 6."),
    ("Bamboo Toothbrush, 4-pack", "product",
     "Moso bamboo handle, BPA-free nylon bristles, soft. Replace every 3 months. Handle is compostable after removing bristles."),
    ("Copper Tongue Scraper", "product",
     "100% copper, naturally antimicrobial surface. Scrape from back to front 3 to 5 times each morning before brushing. Rinse and dry."),
    ("Fluoride-Free Starter Bundle", "product",
     "Includes one mint tooth powder, one bamboo toothbrush 4-pack, one copper tongue scraper and one miswak 3-pack. Saves 12 dollars versus buying separately."),
    ("Bamboo Facial Tissue, 6-box", "product",
     "3-ply tissue made from FSC-certified bamboo pulp, unbleached, no fragrance. 120 sheets per box."),
    ("Shipping policy", "policy",
     "Free standard shipping on US orders over 35 dollars; otherwise 4.95 dollars flat. Orders ship within 2 business days from Los Angeles. "
     "US delivery 3 to 6 business days. We ship to Canada (9.95 dollars) but not to other countries yet."),
    ("Returns and refunds", "policy",
     "Unopened products can be returned within 30 days for a full refund. Opened oral care products cannot be returned for hygiene reasons, "
     "but if a product arrives damaged we replace it free. Refunds are issued to the original payment method within 5 business days."),
    ("Subscription and discounts", "policy",
     "Subscribe and save 15 percent on any product, delivered every 1, 2 or 3 months. Cancel anytime from the account page. "
     "Discount codes cannot be combined with subscription pricing."),
    ("FAQ: Is tooth powder safe without fluoride?", "faq",
     "Our powders rely on calcium carbonate and xylitol to support enamel and reduce cavity-causing bacteria. They are fluoride-free by design. "
     "We do not make medical claims; consult a dentist for cavity treatment."),
    ("FAQ: Why does the charcoal powder look grey?", "faq",
     "Activated charcoal is naturally dark grey to black. Color variation between batches is normal because charcoal is a natural material."),
    ("FAQ: Third-party testing", "faq",
     "Batch test results are published on the product page as they become available. Contact support for the certificate of a specific lot."),
    ("FAQ: Wholesale", "faq",
     "We offer wholesale pricing for orders of 50 units or more. Email wholesale@ with your business details."),
]


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def seed(drop: bool = True) -> dict[str, int]:
    rng = random.Random(SEED)
    db.init_db(drop=drop)
    eng = db.get_engine()
    counts: dict[str, int] = {}
    with eng.begin() as conn:
        conn.execute(insert(db.products), [
            {"id": i + 1, "sku": s, "name": n, "category": c, "price": p, "cost": cost, "active": True}
            for i, (s, n, c, p, cost) in enumerate(PRODUCTS)
        ])
        conn.execute(insert(db.docs), [{"id": i + 1, "title": t, "kind": k, "content": c, "embedding": None} for i, (t, k, c) in enumerate(DOCS)])

        # customers
        states = ["CA", "TX", "NY", "FL", "WA", "IL", "CO", "OR", "AZ", "GA"]
        n_customers = 700
        cust_rows = []
        for cid in range(1, n_customers + 1):
            country = "US" if rng.random() < 0.92 else "CA"
            cust_rows.append({
                "id": cid, "email_hash": _hash(f"c{cid}"), "country": country,
                "state": rng.choice(states) if country == "US" else None, "first_order_date": None,
            })
        conn.execute(insert(db.customers), cust_rows)

        # orders + items, ~8/day with a summer bump and a July promo
        oid, iid = 0, 0
        order_rows, item_rows = [], []
        first_seen: dict[int, date] = {}
        day = START
        while day <= END:
            base = 8 + (3 if day.month in (6, 7, 8) else 0) + (5 if day.month == 7 and 10 <= day.day <= 17 else 0)
            for _ in range(max(1, int(rng.gauss(base, 2)))):
                oid += 1
                cid = rng.randint(1, n_customers)
                first_seen.setdefault(cid, day)
                channel = rng.choices(["online_store", "amazon", "tiktok_shop"], [0.62, 0.23, 0.15])[0]
                utm = rng.choices(["meta", "tiktok", "google", "organic", "email"], [0.28, 0.22, 0.15, 0.25, 0.10])[0] if channel == "online_store" else None
                n_items = rng.choices([1, 2, 3], [0.62, 0.28, 0.10])[0]
                subtotal = 0.0
                for pid in rng.sample(range(1, len(PRODUCTS) + 1), n_items):
                    qty = rng.choices([1, 2, 3], [0.8, 0.15, 0.05])[0]
                    price = PRODUCTS[pid - 1][3]
                    iid += 1
                    item_rows.append({"id": iid, "order_id": oid, "product_id": pid, "quantity": qty, "unit_price": price})
                    subtotal += qty * price
                discount = round(subtotal * 0.15, 2) if (day.month == 7 and 10 <= day.day <= 17 and rng.random() < 0.7) else 0.0
                shipping = 0.0 if subtotal - discount >= 35 else 4.95
                status = rng.choices(["fulfilled", "paid", "refunded", "cancelled"], [0.86, 0.06, 0.05, 0.03])[0]
                order_rows.append({
                    "id": oid, "customer_id": cid, "order_date": day, "status": status, "subtotal": round(subtotal, 2),
                    "discount": discount, "shipping": shipping, "total": round(subtotal - discount + shipping, 2),
                    "channel": channel, "utm_source": utm,
                })
            day += timedelta(days=1)
        conn.execute(insert(db.orders), order_rows)
        conn.execute(insert(db.order_items), item_rows)
        for cid, d in first_seen.items():
            conn.execute(db.customers.update().where(db.customers.c.id == cid).values(first_order_date=d))

        # ad spend, daily per platform
        ad_rows, aid = [], 0
        campaigns = {"meta": ["HF_Prospecting_US", "HF_Retargeting"], "tiktok": ["HF_Spark_Crunchy", "HF_TopView_Test"], "google": ["HF_Brand_Search", "HF_Shopping"]}
        day = START
        while day <= END:
            for platform, camps in campaigns.items():
                for camp in camps:
                    aid += 1
                    spend = round(max(5.0, rng.gauss({"meta": 70, "tiktok": 45, "google": 30}[platform], 15)), 2)
                    impressions = int(spend * rng.uniform(90, 160))
                    clicks = int(impressions * rng.uniform(0.008, 0.02))
                    attributed = int(clicks * rng.uniform(0.02, 0.06))
                    ad_rows.append({"id": aid, "date": day, "platform": platform, "campaign": camp, "spend": spend,
                                    "impressions": impressions, "clicks": clicks, "attributed_orders": attributed})
            day += timedelta(days=1)
        conn.execute(insert(db.ad_spend), ad_rows)
        counts = {"products": len(PRODUCTS), "docs": len(DOCS), "customers": n_customers, "orders": len(order_rows), "order_items": len(item_rows), "ad_spend": len(ad_rows)}
    return counts


if __name__ == "__main__":
    print(seed())
