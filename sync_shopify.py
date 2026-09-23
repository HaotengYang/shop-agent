"""Pull real orders and products from a Shopify store into the same tables the agent queries.

Run by the store owner with their own credentials in .env:
  SHOPIFY_STORE=your-store.myshopify.com
  SHOPIFY_TOKEN=shpat_...        (Admin API access token, read_orders + read_products scopes)

  uv run python sync_shopify.py            # incremental: orders updated since last sync
  uv run python sync_shopify.py --full     # drop and reload

Ad spend is not in Shopify; drop a CSV at data/ad_spend.csv with columns
date,platform,campaign,spend,impressions,clicks,attributed_orders and it is loaded too.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path

import httpx
from sqlalchemy import delete, insert

from shop_agent import config, db

API_VERSION = "2026-07"
STATE_FILE = config.DATA_DIR / "shopify_sync_state.json"


def _client() -> tuple[httpx.Client, str]:
    store, token = os.getenv("SHOPIFY_STORE"), os.getenv("SHOPIFY_TOKEN")
    if not store or not token:
        raise SystemExit("set SHOPIFY_STORE and SHOPIFY_TOKEN in .env")
    return httpx.Client(base_url=f"https://{store}/admin/api/{API_VERSION}", headers={"X-Shopify-Access-Token": token}, timeout=60), store


def _paged(client: httpx.Client, path: str, params: dict) -> list[dict]:
    """Follow Shopify's Link-header cursor pagination."""
    out, url = [], path
    while url:
        r = client.get(url, params=params if url == path else None)
        r.raise_for_status()
        key = next(iter(r.json()))
        out.extend(r.json()[key])
        link = r.headers.get("link", "")
        nxt = [p for p in link.split(",") if 'rel="next"' in p]
        url = nxt[0].split(";")[0].strip("<> ") if nxt else None
    return out


def _status(o: dict) -> str:
    if o.get("cancelled_at"):
        return "cancelled"
    fs = o.get("financial_status")
    if fs in ("refunded", "partially_refunded"):
        return "refunded"
    if o.get("fulfillment_status") == "fulfilled":
        return "fulfilled"
    return "paid"


def _channel(o: dict) -> str:
    name = (o.get("source_name") or "").lower()
    if "amazon" in name:
        return "amazon"
    if "tiktok" in name:
        return "tiktok_shop"
    return "online_store"


def _utm(o: dict) -> str | None:
    lp = (o.get("landing_site") or "").lower()
    for k in ("meta", "facebook", "tiktok", "google", "email"):
        if f"utm_source={k}" in lp:
            return "meta" if k == "facebook" else k
    return "organic" if lp else None


def sync(full: bool) -> dict[str, int]:
    client, _ = _client()
    db.init_db(drop=full)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() and not full else {}
    params = {"limit": 250, "status": "any"}
    if state.get("updated_at_min"):
        params["updated_at_min"] = state["updated_at_min"]

    products = _paged(client, "/products.json", {"limit": 250})
    orders = _paged(client, "/orders.json", params)
    eng = db.get_engine()
    counts = {"products": 0, "orders": 0, "order_items": 0, "customers": 0, "ad_spend": 0}
    with eng.begin() as conn:
        variant_to_product: dict[int, int] = {}
        for p in products:
            v = p["variants"][0]
            conn.execute(delete(db.products).where(db.products.c.id == p["id"]))
            conn.execute(insert(db.products).values(
                id=p["id"], sku=v.get("sku") or str(p["id"]), name=p["title"], category=(p.get("product_type") or "uncategorized").lower(),
                price=float(v["price"]), cost=float(v.get("cost") or 0), active=p.get("status") == "active"))
            for var in p["variants"]:
                variant_to_product[var["id"]] = p["id"]
            counts["products"] += 1
        seen_customers: set[int] = set()
        for o in orders:
            cust = o.get("customer") or {}
            cid = cust.get("id") or 0
            if cid and cid not in seen_customers:
                addr = o.get("shipping_address") or {}
                conn.execute(delete(db.customers).where(db.customers.c.id == cid))
                conn.execute(insert(db.customers).values(
                    id=cid, email_hash=hashlib.sha256((cust.get("email") or str(cid)).encode()).hexdigest()[:16],
                    country=addr.get("country_code") or "US", state=addr.get("province_code"),
                    first_order_date=datetime.fromisoformat(cust["created_at"]).date() if cust.get("created_at") else None))
                seen_customers.add(cid)
                counts["customers"] += 1
            conn.execute(delete(db.order_items).where(db.order_items.c.order_id == o["id"]))
            conn.execute(delete(db.orders).where(db.orders.c.id == o["id"]))
            conn.execute(insert(db.orders).values(
                id=o["id"], customer_id=cid, order_date=datetime.fromisoformat(o["created_at"]).date(), status=_status(o),
                subtotal=float(o["subtotal_price"]), discount=float(o.get("total_discounts") or 0),
                shipping=float((o.get("total_shipping_price_set") or {}).get("shop_money", {}).get("amount") or 0),
                total=float(o["total_price"]), channel=_channel(o), utm_source=_utm(o)))
            counts["orders"] += 1
            for li in o.get("line_items", []):
                pid = variant_to_product.get(li.get("variant_id")) or li.get("product_id") or 0
                conn.execute(insert(db.order_items).values(
                    id=li["id"], order_id=o["id"], product_id=pid, quantity=int(li["quantity"]), unit_price=float(li["price"])))
                counts["order_items"] += 1
        csv_path = config.DATA_DIR / "ad_spend.csv"
        if csv_path.exists():
            conn.execute(delete(db.ad_spend))
            with csv_path.open() as f:
                for i, row in enumerate(csv.DictReader(f), start=1):
                    conn.execute(insert(db.ad_spend).values(
                        id=i, date=date.fromisoformat(row["date"]), platform=row["platform"], campaign=row["campaign"], spend=float(row["spend"]),
                        impressions=int(row["impressions"]), clicks=int(row["clicks"]), attributed_orders=int(row["attributed_orders"])))
                    counts["ad_spend"] += 1
    STATE_FILE.write_text(json.dumps({"updated_at_min": datetime.utcnow().isoformat() + "Z"}))
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    print(sync(full=a.full))
