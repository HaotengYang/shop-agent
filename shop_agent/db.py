"""Database layer. SQLAlchemy Core so the same code runs on SQLite (dev/tests) and PostgreSQL (prod).

Tables are deliberately small and explicit; the text-to-SQL prompt is generated from this schema
so the model never sees a table it is not allowed to touch.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine

from . import config

metadata = MetaData()

products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sku", String(32), nullable=False, unique=True),
    Column("name", String(120), nullable=False),
    Column("category", String(60), nullable=False),
    Column("price", Float, nullable=False),
    Column("cost", Float, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
)

customers = Table(
    "customers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email_hash", String(64), nullable=False),
    Column("country", String(2), nullable=False),
    Column("state", String(2)),
    Column("first_order_date", Date),
)

orders = Table(
    "orders",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("customer_id", Integer, nullable=False),
    Column("order_date", Date, nullable=False),
    Column("status", String(16), nullable=False),  # paid | fulfilled | refunded | cancelled
    Column("subtotal", Float, nullable=False),
    Column("discount", Float, nullable=False, default=0),
    Column("shipping", Float, nullable=False, default=0),
    Column("total", Float, nullable=False),
    Column("channel", String(24), nullable=False),  # online_store | amazon | tiktok_shop
    Column("utm_source", String(24)),  # meta | tiktok | google | organic | email
)

order_items = Table(
    "order_items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("order_id", Integer, nullable=False),
    Column("product_id", Integer, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit_price", Float, nullable=False),
)

ad_spend = Table(
    "ad_spend",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("date", Date, nullable=False),
    Column("platform", String(16), nullable=False),  # meta | tiktok | google
    Column("campaign", String(80), nullable=False),
    Column("spend", Float, nullable=False),
    Column("impressions", Integer, nullable=False),
    Column("clicks", Integer, nullable=False),
    Column("attributed_orders", Integer, nullable=False),
)

docs = Table(
    "docs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(160), nullable=False),
    Column("kind", String(16), nullable=False),  # product | policy | faq
    Column("content", Text, nullable=False),
    Column("embedding", Text),  # JSON list[float]; pgvector column in prod
)

ALLOWED_TABLES = {"products", "customers", "orders", "order_items", "ad_spend"}

SCHEMA_DOC = """
products(id, sku, name, category, price, cost, active)
customers(id, email_hash, country, state, first_order_date)
orders(id, customer_id, order_date, status, subtotal, discount, shipping, total, channel, utm_source)
  status in ('paid','fulfilled','refunded','cancelled'); channel in ('online_store','amazon','tiktok_shop');
  utm_source in ('meta','tiktok','google','organic','email')
order_items(id, order_id, product_id, quantity, unit_price)
ad_spend(id, date, platform, campaign, spend, impressions, clicks, attributed_orders)
  platform in ('meta','tiktok','google')
Conventions (follow unless the question says otherwise):
- Money metrics (revenue, average order value, discounts, shipping fees, gross profit) use orders with status IN ('paid','fulfilled'). "revenue" = SUM(orders.total). Product-level revenue = SUM(order_items.quantity*order_items.unit_price) joined to paid/fulfilled orders.
- "how many orders" / "customers who ordered" / "average items per order" count ALL statuses; filter status only when the question mentions refunds, cancellations or paid orders.
- "units sold" / "fewest or most units" = SUM(order_items.quantity) over paid/fulfilled orders. "sales" in dollars = revenue.
- When computing ratios over a period, aggregate ALL rows first (SUM(spend)/NULLIF(SUM(attributed_orders),0)); never drop rows with a zero metric before summing.
- customers.country is an ISO-2 code: 'US', 'CA' (Canada).
- Product names include pack size (e.g. 'Bamboo Facial Tissue, 6-box'); match with LIKE '%name%' or by sku.
- "attributed to <platform> ads" = SUM(ad_spend.attributed_orders) for that platform, not orders.utm_source.
- Rates (CTR, refund rate, repeat rate) are percentages 0-100, rounded to 2 decimals.
- Return only the columns the question asks for; for "which X" questions return the label and its metric.
- Return raw values: never format numbers or relabel values inside SQL (no printf, no '$' concatenation, no CASE that renames channel/platform codes). Formatting belongs to the answer, not the query.
- Dates are ISO strings YYYY-MM-DD. Month grouping: strftime('%Y-%m', order_date) on SQLite; to_char(order_date,'YYYY-MM') on PostgreSQL.
""".strip()

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(config.DATABASE_URL, future=True)
    return _engine


def init_db(drop: bool = False) -> None:
    eng = get_engine()
    if drop:
        metadata.drop_all(eng)
    metadata.create_all(eng)


def dialect() -> str:
    return get_engine().dialect.name


def run_readonly_sql(sql: str) -> list[dict[str, Any]]:
    """Execute an already-validated SELECT and return rows as dicts."""
    eng = get_engine()
    with eng.connect() as conn:
        if dialect() == "sqlite":
            conn.execute(text("PRAGMA query_only = 1"))
        res = conn.execute(text(sql))
        cols = list(res.keys())
        return [dict(zip(cols, row)) for row in res.fetchall()]


def fetch_docs() -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        res = conn.execute(docs.select())
        out = []
        for row in res.mappings():
            d = dict(row)
            d["embedding"] = json.loads(d["embedding"]) if d.get("embedding") else None
            out.append(d)
        return out
