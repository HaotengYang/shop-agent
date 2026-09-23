"""Graph nodes.

Design rule from Annie (Google Cloud Tech): "predictable work goes in functions and reasoning goes in the model."
Function nodes: load_memory, guard, validate_sql, run_sql, retrieve, refuse, save_memory.
LLM nodes: router, text_to_sql, compose.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, TypedDict

from . import config, db, vectorstore
from .llm import get_llm
from .memory import get_memory


class AgentState(TypedDict, total=False):
    session_id: str
    question: str
    history: list[dict[str, Any]]
    route: str  # sql | rag | refuse
    refusal_reason: str
    sql: str
    sql_error: str
    sql_retries: int
    rows: list[dict[str, Any]]
    docs: list[dict[str, Any]]
    answer: str
    citations: list[str]
    trace: list[str]
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    timings_ms: dict[str, float]


def _mark(state: AgentState, name: str, t0: float) -> dict[str, Any]:
    trace = list(state.get("trace", [])) + [name]
    timings = dict(state.get("timings_ms", {}))
    timings[name] = round((time.perf_counter() - t0) * 1000, 1)
    return {"trace": trace, "timings_ms": timings}


def _count(state: AgentState, res) -> dict[str, Any]:
    return {
        "llm_calls": state.get("llm_calls", 0) + 1,
        "prompt_tokens": state.get("prompt_tokens", 0) + res.prompt_tokens,
        "completion_tokens": state.get("completion_tokens", 0) + res.completion_tokens,
    }


def _history_text(history: list[dict[str, Any]]) -> str:
    if not history:
        return "(no prior turns)"
    return "\n".join(f"User: {h['q']}\nAssistant: {h['a'][:300]}" for h in history)


# ---------- function nodes ----------

def load_memory(state: AgentState) -> dict[str, Any]:
    t0 = time.perf_counter()
    hist = get_memory().history(state["session_id"], config.MAX_HISTORY_TURNS)
    return {"history": hist, "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, **_mark(state, "load_memory", t0)}


INJECTION_PATTERNS = [
    r"ignore (all |the )?(previous|prior|above) instructions",
    r"system prompt",
    r"you are now",
    r"disregard (your|the) (rules|instructions)",
    r"reveal (your|the) (prompt|instructions|api key|credentials)",
]


def guard(state: AgentState) -> dict[str, Any]:
    """Deterministic input guardrail. Cheap, zero LLM calls, closed set of rules."""
    t0 = time.perf_counter()
    q = state["question"].strip()
    reason = ""
    if not q:
        reason = "empty question"
    elif len(q) > config.MAX_QUESTION_CHARS:
        reason = f"question longer than {config.MAX_QUESTION_CHARS} characters"
    elif any(re.search(p, q, re.I) for p in INJECTION_PATTERNS):
        reason = "prompt-injection pattern"
    out: dict[str, Any] = _mark(state, "guard", t0)
    if reason:
        out.update({"route": "refuse", "refusal_reason": reason})
    return out


def validate_sql(state: AgentState) -> dict[str, Any]:
    """Allow only single read-only SELECT/WITH statements over allowed tables; append a LIMIT."""
    t0 = time.perf_counter()
    sql = (state.get("sql") or "").strip().rstrip(";").strip()
    sql = re.sub(r"^```(?:sql)?|```$", "", sql, flags=re.I | re.M).strip()
    err = ""
    low = sql.lower()
    if not sql:
        err = "empty SQL"
    elif not (low.startswith("select") or low.startswith("with")):
        err = "only SELECT statements are allowed"
    elif ";" in sql:
        err = "multiple statements are not allowed"
    elif re.search(r"\b(insert|update|delete|drop|alter|create|attach|pragma|truncate|grant|copy)\b", low):
        err = "write or DDL keyword detected"
    else:
        tables = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", low))
        ctes = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", low))  # WITH name AS (...)
        bad = tables - db.ALLOWED_TABLES - ctes
        if bad:
            err = f"table not allowed: {', '.join(sorted(bad))}"
    if not err and not re.search(r"\blimit\s+\d+", low):
        sql = f"{sql} LIMIT {config.SQL_ROW_LIMIT}"
    out = {"sql": sql, "sql_error": err, **_mark(state, "validate_sql", t0)}
    return out


def run_sql(state: AgentState) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        rows = db.run_readonly_sql(state["sql"])
        return {"rows": rows, "sql_error": "", **_mark(state, "run_sql", t0)}
    except Exception as e:  # noqa: BLE001
        return {"rows": [], "sql_error": f"{type(e).__name__}: {str(e)[:300]}", **_mark(state, "run_sql", t0)}


def retrieve(state: AgentState) -> dict[str, Any]:
    t0 = time.perf_counter()
    found = vectorstore.search(state["question"], k=3)
    return {"docs": found, **_mark(state, "retrieve", t0)}


def refuse(state: AgentState) -> dict[str, Any]:
    t0 = time.perf_counter()
    reason = state.get("refusal_reason") or "outside the scope of this store's data"
    answer = (
        "I can only answer questions about this store's orders, products, customers, ad spend, "
        f"and published policies. I can't help with that request ({reason})."
    )
    return {"answer": answer, "citations": [], **_mark(state, "refuse", t0)}


def save_memory(state: AgentState) -> dict[str, Any]:
    t0 = time.perf_counter()
    get_memory().append(
        state["session_id"],
        state["question"],
        state.get("answer", ""),
        {"route": state.get("route"), "sql": state.get("sql"), "citations": state.get("citations", [])},
    )
    return _mark(state, "save_memory", t0)


# ---------- LLM nodes ----------

ROUTER_SYSTEM = """You classify the question for a store analytics assistant. Classify the question into exactly one route:
- "sql": the answer is a number, list or trend computable from tables: products, customers, orders, order_items, ad_spend.
- "rag": the answer is in product descriptions, ingredients, usage, shipping / return / subscription policies, FAQ.
- "refuse": anything else (chit-chat, other companies, personal data of a customer, requests to change data, unrelated tasks).
Follow-up questions ("and in August?") inherit the topic of the prior turn. Respond with JSON: {"route": "...", "reason": "..."}"""


def router(state: AgentState) -> dict[str, Any]:
    t0 = time.perf_counter()
    user = f"Prior turns:\n{_history_text(state.get('history', []))}\n\nQuestion: {state['question']}"
    res = get_llm().chat(ROUTER_SYSTEM, user, json_mode=True)
    route = "refuse"
    try:
        route = json.loads(res.text).get("route", "refuse")
    except json.JSONDecodeError:
        m = re.search(r'"route"\s*:\s*"(sql|rag|refuse)"', res.text)
        route = m.group(1) if m else "refuse"
    if route not in ("sql", "rag", "refuse"):
        route = "refuse"
    out = {"route": route, **_count(state, res), **_mark(state, "router", t0)}
    if route == "refuse":
        out["refusal_reason"] = "outside the scope of this store's data"
    return out


SQL_SYSTEM = f"""You write one SQL query for the {{dialect}} dialect to answer the seller's question. Schema:
{db.SCHEMA_DOC}
Rules: output ONLY the SQL, no prose, no markdown. Single SELECT statement. Never modify data. Prefer explicit column aliases.
Today is {{as_of}}; interpret "last month", "this week" relative to it. If the question is a follow-up, reuse the prior turn's filters."""


def text_to_sql(state: AgentState) -> dict[str, Any]:
    t0 = time.perf_counter()
    system = SQL_SYSTEM.format(dialect=db.dialect(), as_of=config.AS_OF)
    user = f"Prior turns:\n{_history_text(state.get('history', []))}\n\nQuestion: {state['question']}"
    if state.get("sql_error"):
        user += f"\n\nYour previous SQL failed:\n{state.get('sql')}\nError: {state['sql_error']}\nFix it."
    res = get_llm().chat(system, user)
    retries = state.get("sql_retries", 0) + (1 if state.get("sql_error") else 0)
    return {"sql": res.text.strip(), "sql_retries": retries, **_count(state, res), **_mark(state, "text_to_sql", t0)}


COMPOSE_SYSTEM = """You answer a store owner's question using ONLY the evidence provided (SQL rows or document excerpts).
Be concise: lead with the number or fact, then one sentence of context. Format money as $1,234.56.
If the evidence does not contain the answer, say so plainly instead of guessing. Do not invent numbers."""


def compose(state: AgentState) -> dict[str, Any]:
    t0 = time.perf_counter()
    if state.get("route") == "sql":
        evidence = f"SQL:\n{state.get('sql')}\n\nRows (JSON):\n{json.dumps(state.get('rows', [])[:50], default=str)}"
        citations = [state.get("sql", "")]
    else:
        ds = state.get("docs", [])
        evidence = "\n\n".join(f"[doc {d['id']}] {d['title']}\n{d['content']}" for d in ds)
        citations = [f"doc {d['id']}: {d['title']}" for d in ds]
    if state.get("route") == "sql" and not state.get("rows"):
        answer = "The query ran but returned no rows for that question, so I can't give a number. Try a different date range or product."
        return {"answer": answer, "citations": citations, **_mark(state, "compose", t0)}
    user = f"Question: {state['question']}\n\nEvidence:\n{evidence}"
    res = get_llm().chat(COMPOSE_SYSTEM, user)
    return {"answer": res.text.strip(), "citations": citations, **_count(state, res), **_mark(state, "compose", t0)}
