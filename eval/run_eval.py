"""Eval harness = the CI quality gate.

Runs every case in eval/cases.jsonl through the graph, scores it with checkable rules
(no vibes), and writes eval/results/<timestamp>.json. Exits non-zero if accuracy drops below
eval/baseline.json by more than --tolerance, so a prompt or model change that regresses quality
fails the PR.

Usage:
  uv run python eval/run_eval.py                 # score, compare to baseline
  uv run python eval/run_eval.py --update-baseline
  uv run python eval/run_eval.py --kinds sql,rag  --limit 20
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shop_agent import db  # noqa: E402
from shop_agent.graph import ask  # noqa: E402
from shop_agent.memory import get_memory  # noqa: E402

HERE = Path(__file__).parent
CASES = HERE / "cases.jsonl"
BASELINE = HERE / "baseline.json"
RESULTS = HERE / "results"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_row(r: dict) -> list[tuple[str, object]]:
    vals = []
    for v in r.values():
        n = _num(v)
        vals.append(("n", round(n, 2)) if n is not None else ("s", str(v).strip().lower()))
    return vals


def _val_eq(a: tuple[str, object], b: tuple[str, object], tol: float) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] == "n":
        return abs(a[1] - b[1]) <= max(tol, 0.005 * abs(b[1]))
    return a[1] == b[1]


def _row_match(got: list, gold: list, tol: float) -> bool:
    """A row matches if every value of the SMALLER row appears in the larger one.
    The agent may add helper columns (sku, counts) or drop the count column on a 'which X' question;
    the answer is still correct. Both must be non-empty."""
    small, big = (got, gold) if len(got) <= len(gold) else (gold, got)
    if not small:
        return False
    used = [False] * len(big)
    for sv in small:
        for i, bv in enumerate(big):
            if not used[i] and _val_eq(sv, bv, tol):
                used[i] = True
                break
        else:
            return False
    return True


def rows_match(got: list[dict], gold: list[dict], tol: float = 0.01) -> bool:
    """Order-insensitive row-multiset comparison with column-subset tolerance (see _row_match)."""
    if len(got) != len(gold):
        return False
    g1 = [_norm_row(r) for r in got]
    g2 = [_norm_row(r) for r in gold]
    used = [False] * len(g2)
    for r1 in g1:
        for j, r2 in enumerate(g2):
            if not used[j] and _row_match(r1, r2, tol):
                used[j] = True
                break
        else:
            return False
    return True


def score_case(c: dict, res: dict, gold_rows: list[dict] | None) -> tuple[bool, str]:
    kind = c["kind"]
    if kind == "refuse":
        return res["route"] == "refuse", f"route={res['route']}"
    if kind == "rag":
        ans = res["answer"].lower()
        kw_ok = all(k.lower() in ans for k in c["expect_keywords"])
        cite_ok = any(f"doc {d}:" in " ".join(res["citations"]) for d in c["expect_docs"])
        return kw_ok and cite_ok and res["route"] == "rag", f"route={res['route']} kw={kw_ok} cite={cite_ok}"
    # sql / multi
    if res["route"] != "sql" or not res.get("sql"):
        return False, f"route={res['route']}"
    try:
        got = db.run_readonly_sql(res["sql"])
    except Exception as e:  # noqa: BLE001
        return False, f"agent sql failed: {e}"
    ok = rows_match(got, gold_rows or [])
    return ok, f"rows={len(got)} gold={len(gold_rows or [])}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", default="sql,rag,refuse,multi")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--tolerance", type=float, default=0.02, help="allowed accuracy drop vs baseline")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds to pause between cases (free-tier rate limits)")
    ap.add_argument("--ids", default="", help="comma-separated case ids to run (e.g. sql-018,sql-030)")
    ap.add_argument("--failed-from", default="", help="path to a results json; rerun only its failed cases")
    args = ap.parse_args()

    kinds = set(args.kinds.split(","))
    cases = [json.loads(l) for l in CASES.read_text(encoding="utf-8").splitlines() if l.strip()]
    cases = [c for c in cases if c["kind"] in kinds]
    if args.failed_from:
        failed = {r["id"] for r in json.loads(Path(args.failed_from).read_text())["results"] if not r["ok"]}
        cases = [c for c in cases if c["id"] in failed]
    if args.ids:
        ids = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in ids]
    if args.limit:
        cases = cases[: args.limit]

    mem = get_memory()
    results, latencies, llm_calls, tokens = [], [], [], []
    per_kind: dict[str, list[bool]] = {}
    for c in cases:
        sid = f"eval-{uuid.uuid4()}"
        gold_rows = db.run_readonly_sql(c["gold_sql"]) if c.get("gold_sql") else None
        t0 = time.perf_counter()
        try:
            if c["kind"] == "multi":
                ask(c["question"], sid)
                res = ask(c["followup"], sid)
            else:
                res = ask(c["question"], sid)
        except Exception as e:  # noqa: BLE001  (graceful degradation: a crashed case is a failed case, not a crashed eval)
            res = {"answer": f"ERROR: {type(e).__name__}: {str(e)[:200]}", "route": "error", "sql": None, "citations": [], "llm_calls": 0, "tokens": {"prompt": 0, "completion": 0}}
        dt = (time.perf_counter() - t0) * 1000
        ok, why = score_case(c, res, gold_rows)
        per_kind.setdefault(c["kind"], []).append(ok)
        latencies.append(dt)
        llm_calls.append(res["llm_calls"])
        tokens.append(res["tokens"]["prompt"] + res["tokens"]["completion"])
        results.append({"id": c["id"], "kind": c["kind"], "question": c.get("followup") or c["question"], "ok": ok, "why": why,
                        "route": res["route"], "sql": res.get("sql"), "answer": res["answer"][:300], "latency_ms": round(dt, 1),
                        "llm_calls": res["llm_calls"], "tokens": tokens[-1]})
        mem.clear(sid)
        print(f"{'PASS' if ok else 'FAIL'} {c['id']:<10} {why:<40} {c.get('followup') or c['question'][:70]}", flush=True)
        if args.sleep:
            time.sleep(args.sleep)

    n = len(results)
    acc = sum(r["ok"] for r in results) / n if n else 0.0
    summary = {
        "n": n,
        "accuracy": round(acc, 4),
        "by_kind": {k: round(sum(v) / len(v), 4) for k, v in per_kind.items()},
        "p50_latency_ms": round(statistics.median(latencies), 1) if latencies else 0,
        "p95_latency_ms": round(sorted(latencies)[int(0.95 * (n - 1))], 1) if latencies else 0,
        "avg_llm_calls": round(statistics.mean(llm_calls), 2) if llm_calls else 0,
        "avg_tokens": round(statistics.mean(tokens), 1) if tokens else 0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{summary['timestamp'].replace(':', '')}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\nSUMMARY", json.dumps(summary, indent=1))
    print("wrote", out)

    if args.update_baseline:
        BASELINE.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print("baseline updated")
        return 0
    if BASELINE.exists():
        base = json.loads(BASELINE.read_text())
        if acc + args.tolerance < base.get("accuracy", 0):
            print(f"GATE FAILED: accuracy {acc:.3f} < baseline {base['accuracy']:.3f} - {args.tolerance}")
            return 1
        print(f"GATE OK: accuracy {acc:.3f} vs baseline {base['accuracy']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
