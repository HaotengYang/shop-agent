# Reflection

The write-up a reviewer asked for in the 2sum homework spec: key architecture decisions, what I would change with more time, edge cases found but not fully solved, how testing was done.

## Decisions and why

**Graph before code.** The workflow was drawn first (README diagram) because the answer to "can you draw the workflow before the input arrives?" was yes: a question is either a number from tables, a fact from documents, or out of scope. That made it a fixed LangGraph StateGraph, not a dynamic workflow.

**Functions where the work is predictable, the model where it is not.** Seven of eleven nodes are plain functions (memory load, guardrail, SQL validation, SQL execution, retrieval, refusal, memory save). The model is used three times at most per question: to route, to write SQL, to phrase the answer. This kept a SQL question at 3 LLM calls and a RAG question at 2, and it made most of the system testable without a key.

**One LLM router, everything else deterministic.** The user's question is free text, an open set, so classification needs a model. Every other branch (guard pass or fail, SQL valid or not, retry or give up) is a closed set decided by code. This is the "deterministic router wins when the set is closed and the signal is in the data" rule.

**Evaluation set first.** 102 cases were written before the graph existed. SQL cases carry a gold SQL that is executed at scoring time, so the standard answer is computed, not typed, and the check is non-debatable. The first real-model run scored 82.4% and the failure analysis drove the next three changes (below).

**Grounding over fluency.** The compose node only sees SQL rows or document excerpts. When SQL returns no rows the agent says so instead of answering from the question alone. Every SQL answer carries the SQL; every RAG answer carries doc ids.

**Stop rules everywhere there is a loop.** Text-to-SQL retries at most twice on a validator or execution error, then refuses with the error. HTTP calls retry with backoff at most six times on 429 / 5xx / timeout. Memory loads at most six turns. There is no unbounded loop in the system.

**Same code for dev and prod.** SQLAlchemy Core runs on SQLite locally and PostgreSQL in production; the vector search is numpy cosine locally and a pgvector query in production; memory is a JSON file locally and MongoDB in production. Swapping is one environment variable each.

## What the first eval round taught

82.4% overall, SQL 73%. Reading the 17 failures one by one (not the aggregate) showed three distinct causes:

| Cause | Count | Fix |
|---|---|---|
| Scorer too strict: the agent returned an extra helper column, or only the label on a "which X" question. The answer was right. | 8 | Row match now accepts column subsets in either direction |
| Question wording ambiguous: do "orders" include refunds, is Canada `CA` or `Canada`, which table holds "attributed" orders | 8 | Nine conventions added to the schema prompt; two case questions rewritten to say what they mean |
| Rate limit exhausted retries | 1 | More retries with longer backoff, eval paces itself |

The lesson matches the Neurotype example in Google's graph engineering material: the score told me something was wrong, only the per-case rationale told me where. Half of the "errors" were in the ruler, not the agent.

## Second eval round (2026-09-18)

After the scorer and convention fixes: **95.1% overall** (97 of 102). SQL 96.9%, RAG 100%, refusal 100%, multi-turn 50%. The baseline file now holds this run.

The five misses, read one by one again:

| Cause | Count | Fix |
|---|---|---|
| In follow-up turns the model formatted values inside SQL (`printf('$%.2f', ...)`, a CASE that renamed channel codes), copying the style of its own previous answer from memory. The numbers were right, the rows were strings. | 3 (all multi-turn) | New schema convention: return raw values, formatting belongs to the answer node |
| Tie in the data: two products both sold 36 units, gold picked one with LIMIT 1 | 1 | Case now asks for the minimum unit count, not the product name |
| "Per customer" was ambiguous: all customers or customers who ordered | 1 | Question reworded |

All five pass when re-run in isolation after the fix. The headline number stays at 95.1% until the next full run, because a prompt change can move other cases.

The multi-turn result is the interesting one: memory made the agent imitate its own output format. Conversation history is an input like any other and needs the same discipline as the schema prompt.

## Third full run (2026-09-23)

With the raw-values convention and the two reworded cases: **102 of 102**. Baseline updated.

A perfect score on a set I have been editing is not evidence of generalisation; it is evidence that the scorer, the conventions and the questions now agree with each other. Two of the three rounds of fixes changed the ruler, not the agent. The honest next step is a held-out set: 20 to 30 new questions written by someone else (the store owner), never used for prompt tuning, scored once. That number, not this one, is the one to quote.

## Edge cases found, not fully solved

- **Ambiguous time phrases.** "Last month" is anchored to a fixed `AS_OF` date so eval stays reproducible; in production it should be the request time, which means the eval and prod prompts differ by one line.
- **Aggregation traps.** The model once filtered `attributed_orders > 0` before summing, silently dropping zero days from a cost-per-order ratio. A prompt convention now says aggregate first; a stronger fix would be a property test that re-runs the agent SQL without any `WHERE` on a metric column and compares.
- **Follow-ups that change the entity.** "How much revenue did it make that month?" after "which product sold most" needs the previous answer's entity, not just its filters. It passed, but only because the prior answer text was in memory; a structured `last_entity` slot in state would be more robust.
- **Prompt injection through documents.** Guardrails cover the question. A product description containing instructions would reach the compose prompt unfiltered. Not exploited in tests; would need output-side checks.
- **Free-tier limits.** The eval takes about fifteen minutes at 4 seconds per case because of a ~15 requests per minute cap. A paid tier or a second key removes this; it does not affect the design.

## What I would do with more time

1. **Langfuse traces** on every node, so the per-step latency and token numbers in the state also land in a dashboard.
2. **Lambda + API Gateway** deployment with three GitHub Actions stages; the FastAPI app is ready, Mangum is the only missing piece.
3. **Real store data** via `sync_shopify.py`; the synthetic set exists so the demo exposes nothing private.
4. **Semantic eval for RAG answers.** Keyword plus citation is checkable but coarse; an LLM-as-judge with a rubric, run by a different model than the agent, would grade phrasing without letting the agent grade itself.
5. **Property tests for SQL** (result count never exceeds `LIMIT`, revenue never includes refunded rows) as a second line behind the gold-SQL comparison.

## Testing

- `tests/`: 17 contract tests in mock mode, no network. They test edges, not the model: injection is refused with zero LLM calls; the validator blocks writes, multi-statements and unknown tables; the retry loop stops after two; memory carries the prior turn into the router prompt.
- `eval/`: 102 checkable cases across four kinds, run against the real model, compared to a stored baseline, non-zero exit on regression. CI runs the contract tests on every push and the eval when a key is configured.
