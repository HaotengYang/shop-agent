# Storefront Analytics Agent

A seller asks questions in plain English about their Shopify store ("What was revenue in July?", "Which channel had the most orders?", "Is the charcoal powder fluoride-free?") and gets an answer grounded in the store's own data: SQL over orders / products / customers / ad spend, or retrieval over product and policy documents. Multi-turn, guardrailed, evaluated, deployable.

Built graph-first: the workflow was drawn before any input arrived, evaluation cases were written before the agent.

## The graph

```
START -> load_memory -> guard -+-> refuse -----------------------------------+-> save_memory -> END
                               |                                             |
                               +-> router -+-> text_to_sql -> validate_sql -+-> run_sql -> compose
                                           |        ^                       |      |
                                           |        +--- retry (max 2) -----+------+
                                           +-> retrieve -> compose
                                           +-> refuse
```

| Node | Type | What it does |
|---|---|---|
| load_memory | function | last 6 turns for this session (JSON file in dev, MongoDB in prod) |
| guard | function | deterministic input guardrail: empty, too long, prompt-injection patterns. Zero LLM calls |
| router | LLM | free-text question is an open set, so this is the one LLM router: `sql`, `rag`, or `refuse` |
| text_to_sql | LLM | schema-constrained SQL for the current dialect; sees prior turns for follow-ups |
| validate_sql | function | read-only SELECT only, allowlisted tables, no multi-statements, LIMIT appended |
| run_sql | function | executes; on error feeds the message back to text_to_sql, stop rule at 2 retries |
| retrieve | function | vector search over docs (numpy cosine in dev, pgvector in prod) |
| compose | LLM | answer from evidence only; refuses to invent numbers when rows are empty |
| refuse | function | scoped refusal with the reason |
| save_memory | function | append the turn |

Design rules followed (Google Cloud Tech, "Graph Engineering with ADK"): predictable work goes in functions and reasoning goes in the model; deterministic router when the set is closed, LLM router when the input is free text; count the LLM calls (3 per SQL question, 2 per RAG question); every loop has a stop rule; the agent never grades its own work, the eval harness does.

## Run it

```bash
uv sync
uv run python -m shop_agent.seed          # synthetic 6-month store (deterministic)
uv run python -c "from shop_agent import vectorstore; print(vectorstore.index_docs())"
uv run uvicorn shop_agent.api:app --reload  # http://127.0.0.1:8000
```

Without `LLM_API_KEY` the agent runs on a mock model (fixed answers) so the graph, guardrails and tests work offline. Set the key in `.env` (see `.env.example`) to use a real model; any OpenAI-compatible endpoint works.

## Evaluate (the CI quality gate)

```bash
uv run python eval/build_cases.py       # regenerates eval/cases.jsonl (102 cases: 64 sql, 22 rag, 10 refuse, 6 multi-turn)
uv run python eval/run_eval.py          # scores, compares to eval/baseline.json, non-zero exit on regression
uv run python eval/run_eval.py --update-baseline
```

Scoring is checkable, not vibes: SQL cases execute `gold_sql` and compare row multisets with numeric tolerance; RAG cases require expected keywords and a citation of the expected doc; refuse cases require the refuse route; multi-turn cases grade the follow-up. The harness reports accuracy by kind, p50/p95 latency, average LLM calls and tokens.

## Tests

```bash
uv run pytest -q
```

Tests cover the contract, not the model: injection is refused with zero LLM calls, the SQL validator blocks writes and unknown tables, the retry loop stops after `MAX_SQL_RETRIES`, memory carries the prior turn into the prompt.

## Real store data

`sync_shopify.py` (to be run with the seller's own Shopify Admin API token in `.env`) fills the same tables from Orders and Products; ad spend comes from a CSV export. The public demo runs on the synthetic dataset so nothing private is exposed.

## Deploy

- API: FastAPI wrapped with Mangum on AWS Lambda behind API Gateway; three stages (dev, staging, prod) driven by GitHub Actions on branch.
- Data: PostgreSQL with pgvector (`DATABASE_URL`), MongoDB Atlas for conversation memory (`MONGODB_URI`).
- Tracing: Langfuse keys in `.env` enable per-step traces of latency, token cost and tool calls.

## Layout

```
shop_agent/   config, db (SQLAlchemy Core), seed, llm (OpenAI-compatible + Mock), memory, vectorstore, nodes, graph, api
eval/         build_cases.py, cases.jsonl, run_eval.py, baseline.json, results/
tests/        contract tests in mock mode
```
