"""
stream_memory.py — фасад новой системы памяти (L0→L1→L2→L3).
Заменяет старый stream_memory.py + brain/ + consolidation.py.
"""
from __future__ import annotations
import hashlib
from memory.db import init as _db_init
from memory import l0, l1, l1_extractor, l2, l3, insula, recall as _recall


# ── Инициализация ─────────────────────────────────────────────────────────────

def init() -> None:
    _db_init()
    insula.get()  # создаёт запись если нет


# ── Идентификация зрителей ────────────────────────────────────────────────────

def _session_key(username: str) -> str:
    """Стабильный session_key из username."""
    return f"viewer_{hashlib.md5(username.lower().encode()).hexdigest()[:12]}"


# ── Recall (перед ответом) ────────────────────────────────────────────────────

async def retrieve_for_stream(query: str, username: str, top_k: int = 5) -> str:
    sk = _session_key(username)
    result = await _recall.recall(query, sk, top_k=top_k)
    parts = [v for v in [result["memories_block"], result["persona_block"]] if v]
    return ("\n\n[ПАМЯТЬ]:\n" + "\n\n".join(parts)) if parts else ""


# ── Capture (после ответа) ────────────────────────────────────────────────────

async def store_stream_episode(
    username: str,
    message: str,
    reply: str,
    salience: float = 0.5,
    emotional_tag: str = "пустота",
    prediction_error: float = 0.3,
) -> None:
    sk = _session_key(username)

    # L0: записываем сырой диалог
    l0.record_turn(sk, message, reply)

    # L1: extraction из последних сообщений
    messages = l0.query_for_l1(sk, limit=20)
    await l1_extractor.extract_and_store(sk, messages)

    # Инсула
    insula.apply_turn(salience, prediction_error, intimacy=0.1)


# ── Состояние ─────────────────────────────────────────────────────────────────

def get_stream_state_block() -> str:
    return insula.to_prompt_block()


async def refresh_stream_state(last_message: str, emotion_hint: str) -> None:
    await insula.refresh_narrative(last_message, emotion_hint)


# ── Убеждения ─────────────────────────────────────────────────────────────────

def get_beliefs_block() -> str:
    from memory.db import _conn
    c = _conn()
    rows = c.execute("SELECT text, level, strength FROM beliefs ORDER BY strength DESC LIMIT 6").fetchall()
    c.close()
    if not rows:
        return ""
    lines = [f"• [{r['level']}] {r['text']}" for r in rows]
    return "[Убеждения]:\n" + "\n".join(lines)


# ── Модель зрителя ────────────────────────────────────────────────────────────

def viewer_model_to_block(username: str) -> str:
    sk = _session_key(username)
    persona = l3.get_persona(sk)
    if not persona:
        return ""
    # Берём только первые 500 символов персоны для промпта
    return f"[Профиль {username}]:\n{persona[:500]}"


# ── Консолидация (L2 + L3) ────────────────────────────────────────────────────

async def maybe_consolidate(username: str, threshold: int = 40) -> None:
    """Запускает L2/L3 если накопилось достаточно L1 атомов."""
    sk = _session_key(username)
    count = l1.count(sk)
    if count < threshold:
        return
    print(f"[memory] консолидация для {username} ({count} атомов)...")
    atoms = l1.get_by_session(sk, limit=50)
    changed = await l2.run_l2(sk, atoms)
    if changed > 0:
        await l3.run_l3(sk)
