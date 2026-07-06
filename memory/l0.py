"""
L0 Recorder — порт l0-recorder.ts.
Записывает сырые сообщения диалога в l0_conversations.
"""
from __future__ import annotations
import time, uuid
from datetime import datetime, timezone
from .db import _conn


def record_turn(session_key: str, user_msg: str, assistant_msg: str, session_id: str = "") -> list[str]:
    """Записывает пару user/assistant, возвращает record_ids."""
    now_iso = datetime.now(timezone.utc).isoformat()
    ts = time.time()
    ids = []
    c = _conn()
    for role, text in [("user", user_msg), ("assistant", assistant_msg)]:
        rid = f"l0_{session_key}_{int(ts*1000)}_{role}_{uuid.uuid4().hex[:6]}"
        c.execute(
            "INSERT OR IGNORE INTO l0_conversations VALUES (?,?,?,?,?,?,?)",
            (rid, session_key, session_id, role, text[:8000], now_iso, ts)
        )
        ids.append(rid)
    c.commit(); c.close()
    return ids


def query_for_l1(session_key: str, after_ts: float = 0, limit: int = 50) -> list[dict]:
    """Читает L0 для L1 extraction — хронологически."""
    c = _conn()
    rows = c.execute(
        "SELECT record_id,session_key,session_id,role,message_text,recorded_at,timestamp "
        "FROM l0_conversations WHERE session_key=? AND timestamp>? "
        "ORDER BY timestamp ASC LIMIT ?",
        (session_key, after_ts, limit)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def count(session_key: str) -> int:
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM l0_conversations WHERE session_key=?", (session_key,)).fetchone()[0]
    c.close()
    return n
