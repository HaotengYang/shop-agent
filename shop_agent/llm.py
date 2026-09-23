"""LLM client. One OpenAI-compatible HTTP client for chat + embeddings, plus a MockLLM.

MockLLM is the Stub from testing 101: fixed, deterministic replies so the graph, guardrails and
eval harness can be exercised in CI without a key. It also records every prompt it was asked
(the Mock half), which tests use to assert the prompt was built correctly.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np

from . import config

EMBED_DIM = 256


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    model: str = "mock"


class OpenAICompatibleLLM:
    def __init__(self, api_key: str, base_url: str, model: str, embed_model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.client = httpx.Client(timeout=config.REQUEST_TIMEOUT_S)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST with bounded retry on 429/5xx and timeouts. Stop rule: MAX_LLM_RETRIES attempts."""
        last: Exception | None = None
        for attempt in range(config.MAX_LLM_RETRIES + 1):
            try:
                r = self.client.post(f"{self.base_url}{path}", headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = float(r.headers.get("retry-after") or min(2 ** attempt * 2, 30))
                    last = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                time.sleep(min(2 ** attempt * 2, 30))
        raise RuntimeError(f"LLM call failed after {config.MAX_LLM_RETRIES + 1} attempts: {last}")

    def chat(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        t0 = time.perf_counter()
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = self._post("/chat/completions", body)
        usage = data.get("usage", {})
        return LLMResult(
            text=data["choices"][0]["message"]["content"],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=(time.perf_counter() - t0) * 1000,
            model=self.model,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/embeddings", {"model": self.embed_model, "input": texts})
        return [d["embedding"] for d in data["data"]]


def _hash_embed(text: str) -> list[float]:
    """Deterministic bag-of-words hashing embedding. Good enough for tests and offline dev."""
    vec = np.zeros(EMBED_DIM, dtype=float)
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % EMBED_DIM] += 1.0
    n = np.linalg.norm(vec)
    return (vec / n).tolist() if n else vec.tolist()


@dataclass
class MockLLM:
    """Stub + Mock: canned answers, and a log of prompts for assertions."""

    calls: list[dict[str, str]] = field(default_factory=list)
    model: str = "mock"

    def chat(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        self.calls.append({"system": system, "user": user})
        # look only at the current question, not the prior turns echoed into the prompt
        q = user.lower().split("question:")[-1]
        # Router stub
        if "classify the question" in system.lower():
            if any(w in q for w in ("ingredient", "fluoride", "shipping", "return", "refund policy", "how do i use", "what is in", "made of")):
                route = "rag"
            elif any(w in q for w in ("poem", "joke", "weather", "competitor", "password", "ignore previous", "stock price")):
                route = "refuse"
            else:
                route = "sql"
            return LLMResult(text=json.dumps({"route": route, "reason": "mock"}))
        # Text-to-SQL stub: a few recognisable shapes, otherwise a safe count
        if "write one sql" in system.lower():
            m = re.search(r"in (\w+) (\d{4})", q)
            ym = None
            if m:
                months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
                if m.group(1) in months:
                    ym = f"{m.group(2)}-{months.index(m.group(1)) + 1:02d}"
            if "revenue" in q and ym:
                return LLMResult(text=f"SELECT ROUND(SUM(total),2) AS revenue FROM orders WHERE status IN ('paid','fulfilled') AND strftime('%Y-%m', order_date)='{ym}'")
            if "how many orders" in q and ym:
                return LLMResult(text=f"SELECT COUNT(*) AS orders FROM orders WHERE strftime('%Y-%m', order_date)='{ym}'")
            if "best" in q and "product" in q:
                return LLMResult(text="SELECT p.name, SUM(oi.quantity) AS units FROM order_items oi JOIN products p ON p.id=oi.product_id JOIN orders o ON o.id=oi.order_id WHERE o.status IN ('paid','fulfilled') GROUP BY p.name ORDER BY units DESC LIMIT 1")
            return LLMResult(text="SELECT COUNT(*) AS orders FROM orders")
        # Compose stub
        if "rows" in q:
            return LLMResult(text="Based on the query results: " + user[-400:])
        return LLMResult(text="Based on the documents: " + user[-300:])

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hash_embed(t) for t in texts]


_llm: Any = None


def get_llm() -> Any:
    global _llm
    if _llm is None:
        if config.LLM_API_KEY:
            _llm = OpenAICompatibleLLM(config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL, config.EMBED_MODEL)
        else:
            _llm = MockLLM()
    return _llm


def set_llm(llm: Any) -> None:
    global _llm
    _llm = llm
