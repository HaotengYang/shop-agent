"""Conversation memory: MongoDB in prod (MONGODB_URI), a JSON file in dev. Same interface.

Only the last MAX_HISTORY_TURNS turns are ever loaded into the prompt (token budget).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from . import config

_lock = threading.Lock()


class JsonFileMemory:
    def __init__(self, path=config.MEMORY_FILE):
        self.path = path

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def history(self, session_id: str, limit: int = config.MAX_HISTORY_TURNS) -> list[dict[str, Any]]:
        with _lock:
            return self._load().get(session_id, [])[-limit:]

    def append(self, session_id: str, question: str, answer: str, meta: dict[str, Any] | None = None) -> None:
        with _lock:
            data = self._load()
            data.setdefault(session_id, []).append(
                {"ts": datetime.now(timezone.utc).isoformat(), "q": question, "a": answer, "meta": meta or {}}
            )
            self._save(data)

    def clear(self, session_id: str) -> None:
        with _lock:
            data = self._load()
            data.pop(session_id, None)
            self._save(data)


class MongoMemory:
    def __init__(self, uri: str):
        from pymongo import MongoClient  # optional dependency

        self.col = MongoClient(uri)["shop_agent"]["conversations"]

    def history(self, session_id: str, limit: int = config.MAX_HISTORY_TURNS) -> list[dict[str, Any]]:
        cur = self.col.find({"session_id": session_id}, {"_id": 0}).sort("ts", -1).limit(limit)
        return list(reversed(list(cur)))

    def append(self, session_id: str, question: str, answer: str, meta: dict[str, Any] | None = None) -> None:
        self.col.insert_one(
            {"session_id": session_id, "ts": datetime.now(timezone.utc).isoformat(), "q": question, "a": answer, "meta": meta or {}}
        )

    def clear(self, session_id: str) -> None:
        self.col.delete_many({"session_id": session_id})


_memory: Any = None


def get_memory() -> Any:
    global _memory
    if _memory is None:
        _memory = MongoMemory(config.MONGODB_URI) if config.MONGODB_URI else JsonFileMemory()
    return _memory
