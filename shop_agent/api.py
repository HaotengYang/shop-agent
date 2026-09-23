"""FastAPI surface. Run locally with `uv run uvicorn shop_agent.api:app --reload`;
on AWS wrap with Mangum for Lambda + API Gateway (see README).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import config
from .graph import ask

app = FastAPI(title="Storefront Analytics Agent", version="0.1.0")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=config.MAX_QUESTION_CHARS)
    session_id: str | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "llm": "mock" if not config.LLM_API_KEY else config.LLM_MODEL, "as_of": config.AS_OF}


@app.post("/ask")
def ask_endpoint(req: AskRequest) -> dict:
    try:
        return ask(req.question, req.session_id)
    except Exception as e:  # graceful degradation: never 500 with a stack trace
        raise HTTPException(status_code=503, detail=f"agent unavailable: {type(e).__name__}") from e


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """<!doctype html><title>Storefront Analytics Agent</title>
<style>body{font:15px/1.5 system-ui;max-width:720px;margin:40px auto;padding:0 16px}pre{white-space:pre-wrap;background:#f4f4f4;padding:10px}</style>
<h2>Storefront Analytics Agent</h2>
<p>Ask about orders, products, customers, ad spend, or policies.</p>
<input id=q style="width:80%" placeholder="Total revenue in July 2026?"><button onclick=go()>Ask</button>
<pre id=out></pre>
<script>
let sid=null;
async function go(){const q=document.getElementById('q').value;document.getElementById('out').textContent='...';
const r=await fetch('/ask',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({question:q,session_id:sid})});
const j=await r.json();sid=j.session_id||sid;document.getElementById('out').textContent=JSON.stringify(j,null,2);}
</script>"""
