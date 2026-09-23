"""The graph. Drawn before any input arrives (Annie's first question), so it is a fixed LangGraph StateGraph.

START -> load_memory -> guard -+-> refuse ------------------------------+-> save_memory -> END
                               |                                         |
                               +-> router -+-> text_to_sql -> validate_sql -+-> run_sql -> compose
                                           |        ^                       |      |
                                           |        +--- (retry, max 2) ----+------+
                                           +-> retrieve -> compose
                                           +-> refuse

LLM calls per question: router (1) + text_to_sql (1, up to 3 with retries) + compose (1) = 3 typical.
Deterministic routers everywhere a closed set decides; the only LLM router is the free-text one.
"""
from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph import END, START, StateGraph

from . import config
from .nodes import (
    AgentState,
    compose,
    guard,
    load_memory,
    refuse,
    retrieve,
    router,
    run_sql,
    save_memory,
    text_to_sql,
    validate_sql,
)


def after_guard(state: AgentState) -> str:
    return "refuse" if state.get("route") == "refuse" else "router"


def after_router(state: AgentState) -> str:
    return {"sql": "text_to_sql", "rag": "retrieve"}.get(state.get("route", "refuse"), "refuse")


def after_validate(state: AgentState) -> str:
    if not state.get("sql_error"):
        return "run_sql"
    return "text_to_sql" if state.get("sql_retries", 0) < config.MAX_SQL_RETRIES else "refuse_sql"


def after_run_sql(state: AgentState) -> str:
    if not state.get("sql_error"):
        return "compose"
    return "text_to_sql" if state.get("sql_retries", 0) < config.MAX_SQL_RETRIES else "refuse_sql"


def refuse_sql(state: AgentState) -> dict[str, Any]:
    out = refuse({**state, "refusal_reason": f"could not produce a valid query after {config.MAX_SQL_RETRIES} retries: {state.get('sql_error', '')[:120]}"})
    out["trace"] = list(state.get("trace", [])) + ["refuse_sql"]
    return out


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("load_memory", load_memory)
    g.add_node("guard", guard)
    g.add_node("router", router)
    g.add_node("text_to_sql", text_to_sql)
    g.add_node("validate_sql", validate_sql)
    g.add_node("run_sql", run_sql)
    g.add_node("retrieve", retrieve)
    g.add_node("compose", compose)
    g.add_node("refuse", refuse)
    g.add_node("refuse_sql", refuse_sql)
    g.add_node("save_memory", save_memory)

    g.add_edge(START, "load_memory")
    g.add_edge("load_memory", "guard")
    g.add_conditional_edges("guard", after_guard, {"refuse": "refuse", "router": "router"})
    g.add_conditional_edges("router", after_router, {"text_to_sql": "text_to_sql", "retrieve": "retrieve", "refuse": "refuse"})
    g.add_edge("text_to_sql", "validate_sql")
    g.add_conditional_edges("validate_sql", after_validate, {"run_sql": "run_sql", "text_to_sql": "text_to_sql", "refuse_sql": "refuse_sql"})
    g.add_conditional_edges("run_sql", after_run_sql, {"compose": "compose", "text_to_sql": "text_to_sql", "refuse_sql": "refuse_sql"})
    g.add_edge("retrieve", "compose")
    g.add_edge("compose", "save_memory")
    g.add_edge("refuse", "save_memory")
    g.add_edge("refuse_sql", "save_memory")
    g.add_edge("save_memory", END)
    return g.compile()


_app = None


def get_app():
    global _app
    if _app is None:
        _app = build_graph()
    return _app


def ask(question: str, session_id: str | None = None) -> dict[str, Any]:
    sid = session_id or str(uuid.uuid4())
    final = get_app().invoke({"session_id": sid, "question": question})
    return {
        "session_id": sid,
        "answer": final.get("answer", ""),
        "route": final.get("route"),
        "sql": final.get("sql") if final.get("route") == "sql" else None,
        "citations": final.get("citations", []),
        "trace": final.get("trace", []),
        "llm_calls": final.get("llm_calls", 0),
        "tokens": {"prompt": final.get("prompt_tokens", 0), "completion": final.get("completion_tokens", 0)},
        "timings_ms": final.get("timings_ms", {}),
    }
