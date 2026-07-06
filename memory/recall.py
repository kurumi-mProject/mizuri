"""
Auto-Recall — порт auto-recall.ts.
Hybrid search (vector + FTS-like keyword) с RRF merge.
Инжектирует L1 воспоминания + L3 персону + L2 сцены в промпт.
"""
from __future__ import annotations
import math
from . import l1 as l1_store
from .l3 import get_persona
from .embeddings import embed_batch
from .db import _conn

RRF_K = 60


def _keyword_search(query: str, session_key: str, top_k: int) -> list[dict]:
    """Простой keyword поиск по content (LIKE)."""
    words = [w for w in query.lower().split() if len(w) > 2]
    if not words:
        return []
    c = _conn()
    # Ищем записи содержащие хотя бы одно слово
    conditions = " OR ".join(["LOWER(content) LIKE ?" for _ in words])
    params = [f"%{w}%" for w in words] + [session_key, top_k * 2]
    rows = c.execute(
        f"SELECT * FROM l1_records WHERE ({conditions}) AND session_key=? ORDER BY priority DESC LIMIT ?",
        params
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _rrf_merge(vec_results: list[dict], kw_results: list[dict], top_k: int) -> list[dict]:
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for rank, r in enumerate(vec_results):
        rid = r["record_id"]
        scores[rid] = scores.get(rid, 0) + 1 / (RRF_K + rank + 1)
        items[rid] = r

    for rank, r in enumerate(kw_results):
        rid = r["record_id"]
        scores[rid] = scores.get(rid, 0) + 1 / (RRF_K + rank + 1)
        items[rid] = r

    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:top_k]
    return [items[rid] for rid in sorted_ids]


def _format_memory_line(r: dict) -> str:
    tag = r["type"]
    if r.get("scene_name"):
        tag += f"|{r['scene_name']}"
    line = f"- [{tag}] {r['content']}"
    ts = r.get("timestamp_str") or r.get("timestamp_start")
    if ts:
        line += f" (время: {ts})"
    return line


async def recall(
    query: str,
    session_key: str,
    top_k: int = 5,
    threshold: float = 0.3,
) -> dict:
    """
    Hybrid recall: vector + keyword → RRF merge.
    Возвращает {memories_block, persona_block, scenes_block}.
    """
    # Vector search
    vecs = await embed_batch([query])
    vec_results = []
    if vecs:
        vec_results = l1_store.search_vector(vecs[0], top_k=top_k * 2, threshold=threshold)

    # Keyword search
    kw_results = _keyword_search(query, session_key, top_k)

    # RRF merge
    merged = _rrf_merge(vec_results, kw_results, top_k)

    memories_block = ""
    if merged:
        lines = [_format_memory_line(r) for r in merged]
        memories_block = (
            "<relevant-memories>\n"
            "Из памяти (для справки):\n\n"
            + "\n".join(lines) +
            "\n</relevant-memories>"
        )

    # L3 Persona
    persona = get_persona(session_key)
    persona_block = f"<user-persona>\n{persona}\n</user-persona>" if persona else ""

    # L2 Scenes (краткий индекс)
    c = _conn()
    scenes = c.execute(
        "SELECT filename, summary FROM l2_scene_index WHERE session_key=? ORDER BY heat DESC LIMIT 5",
        (session_key,)
    ).fetchall()
    c.close()
    scenes_block = ""
    if scenes:
        lines = [f"- `{s['filename']}`: {s['summary']}" for s in scenes]
        scenes_block = "<scene-index>\n" + "\n".join(lines) + "\n</scene-index>"

    return {
        "memories_block": memories_block,
        "persona_block": persona_block,
        "scenes_block": scenes_block,
    }
