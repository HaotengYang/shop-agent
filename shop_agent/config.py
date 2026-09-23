"""Central config. Everything comes from environment variables with dev-friendly defaults.

Dev default: SQLite file + JSON memory + mock LLM (no keys needed).
Prod: DATABASE_URL=postgresql+psycopg://... , MONGODB_URI=..., LLM_API_KEY=...
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'shop.db'}")
MONGODB_URI = os.getenv("MONGODB_URI")  # optional; falls back to JSON file memory
MEMORY_FILE = DATA_DIR / "memory.json"

LLM_API_KEY = os.getenv("LLM_API_KEY")  # OpenAI-compatible key; absent => MockLLM
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")

LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com")

# "Today" for relative-date questions. Fixed in eval so answers stay checkable.
AS_OF = os.getenv("AS_OF", "2026-09-01")

# Stop rules (Annie: "always enforce stop rules and cost caps")
MAX_SQL_RETRIES = int(os.getenv("MAX_SQL_RETRIES", "2"))
MAX_LLM_RETRIES = int(os.getenv("MAX_LLM_RETRIES", "6"))  # 429 / 5xx / timeout backoff attempts (free tier is ~15 RPM)
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "6"))
MAX_QUESTION_CHARS = int(os.getenv("MAX_QUESTION_CHARS", "500"))
SQL_ROW_LIMIT = int(os.getenv("SQL_ROW_LIMIT", "200"))
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "45"))
