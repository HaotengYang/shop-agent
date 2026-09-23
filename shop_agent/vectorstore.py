"""Vector retrieval over the docs table.

Dev: embeddings stored as JSON in SQLite, cosine similarity in numpy.
Prod: same rows in PostgreSQL with a pgvector column; swap `search` for an `<=>` query.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
from sqlalchemy import update

from . import db
from .llm import get_llm


def index_docs() -> int:
    """Embed every doc that has no embedding yet. Returns count embedded."""
    rows = db.fetch_docs()
    todo = [r for r in rows if not r["embedding"]]
    if not todo:
        return 0
    vecs = get_llm().embed([f"{r['title']}\n{r['content']}" for r in todo])
    eng = db.get_engine()
    with eng.begin() as conn:
        for r, v in zip(todo, vecs):
            conn.execute(update(db.docs).where(db.docs.c.id == r["id"]).values(embedding=json.dumps(v)))
    return len(todo)


def search(query: str, k: int = 3) -> list[dict[str, Any]]:
    rows = [r for r in db.fetch_docs() if r["embedding"]]
    if not rows:
        return []
    q = np.array(get_llm().embed([query])[0])
    mat = np.array([r["embedding"] for r in rows])
    qn = np.linalg.norm(q) or 1.0
    sims = mat @ q / (np.linalg.norm(mat, axis=1) * qn + 1e-9)
    order = np.argsort(-sims)[:k]
    out = []
    for i in order:
        r = dict(rows[int(i)])
        r.pop("embedding", None)
        r["score"] = float(sims[int(i)])
        out.append(r)
    return out
