"""
L1 Writer/Reader — порт l1-writer.ts + l1-reader.ts.
Хранит атомарные факты в l1_records + векторный индекс в памяти (dict).
"""
from __future__ import annotations
import time, uuid, json
from datetime import datetime, timezone
from .db import _conn, pack_vec, unpack_vec, cosine

# In-memory vector index: record_id → embedding
_vec_index: dict[str, list[float]] = {}
_index_loaded = False


def _ensure_index() -> None:
    global _index_loaded
    if _index_loaded:
        return
    c = _conn()
    rows = c.execute("SELECT record_id, embedding FROM l1_vec_store").fetchall()
    c.close()
    for r in rows:
        if r["embedding"]:
            _vec_index[r["record_id"]] = unpack_vec(r["embedding"])
    _index_loaded = True


def _init_vec_table() -> None:
    c = _conn()
    c.execute("""CREATE TABLE IF NOT EXISTS l1_vec_store (
        record_id TEXT PRIMARY KEY,
        embedding BLOB NOT NULL,
        updated_time TEXT NOT NULL
    )""")
    c.commit(); c.close()


def write(
    session_key: str,
    content: str,
    mem_type: str,
    priority: int,
    scene_name: str = "",
    source_msg_ids: list[str] | None = None,
    metadata: dict | None = None,
    timestamp_str: str = "",
    timestamp_start: str = "",
    timestamp_end: str = "",
    session_id: str = "",
    embedding: list[float] | None = None,
) -> str:
    """Записывает L1 запись, возвращает record_id."""
    _init_vec_table()
    rid = f"l1_{session_key}_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    c = _conn()
    c.execute(
        "INSERT INTO l1_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid, session_key, session_id, content, mem_type, priority,
            scene_name,
            json.dumps(source_msg_ids or []),
            json.dumps(metadata or {}),
            timestamp_str, timestamp_start, timestamp_end,
            now, now
        )
    )
    if embedding:
        c.execute(
            "INSERT OR REPLACE INTO l1_vec_store VALUES (?,?,?)",
            (rid, pack_vec(embedding), now)
        )
        _vec_index[rid] = embedding
    c.commit(); c.close()
    return rid


def update(record_id: str, content: str, mem_type: str, priority: int,
           merged_timestamps: list[str] | None = None,
           embedding: list[float] | None = None) -> None:
    """Обновляет существующую запись (action=update/merge)."""
    _init_vec_table()
    now = datetime.now(timezone.utc).isoformat()
    c = _conn()
    c.execute(
        "UPDATE l1_records SET content=?,type=?,priority=?,timestamp_str=?,updated_time=? WHERE record_id=?",
        (content, mem_type, priority,
         merged_timestamps[0] if merged_timestamps else "",
         now, record_id)
    )
    if embedding:
        c.execute(
            "INSERT OR REPLACE INTO l1_vec_store VALUES (?,?,?)",
            (record_id, pack_vec(embedding), now)
        )
        _vec_index[record_id] = embedding
    c.commit(); c.close()


def delete(record_id: str) -> None:
    c = _conn()
    c.execute("DELETE FROM l1_records WHERE record_id=?", (record_id,))
    c.execute("DELETE FROM l1_vec_store WHERE record_id=?", (record_id,))
    _vec_index.pop(record_id, None)
    c.commit(); c.close()


def search_vector(query_vec: list[float], top_k: int = 5, threshold: float = 0.3) -> list[dict]:
    """Косинусный поиск по in-memory индексу."""
    _ensure_index()
    scored = []
    for rid, vec in _vec_index.items():
        s = cosine(query_vec, vec)
        if s >= threshold:
            scored.append((rid, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    if not scored:
        return []
    top_ids = [r for r, _ in scored[:top_k]]
    scores = {r: s for r, s in scored[:top_k]}
    c = _conn()
    rows = c.execute(
        f"SELECT * FROM l1_records WHERE record_id IN ({','.join('?'*len(top_ids))})",
        top_ids
    ).fetchall()
    c.close()
    result = []
    for r in rows:
        d = dict(r)
        d["score"] = scores.get(d["record_id"], 0.0)
        result.append(d)
    result.sort(key=lambda x: x["score"], reverse=True)
    return result


def get_by_session(session_key: str, limit: int = 100) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT * FROM l1_records WHERE session_key=? ORDER BY updated_time DESC LIMIT ?",
        (session_key, limit)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def count(session_key: str) -> int:
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM l1_records WHERE session_key=?", (session_key,)).fetchone()[0]
    c.close()
    return n
