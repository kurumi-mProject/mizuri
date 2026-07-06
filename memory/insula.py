"""
Insula — внутреннее состояние Мизури (energy/tension/openness + нарратив).
"""
from __future__ import annotations
import time
from .db import _conn


def get() -> dict:
    c = _conn()
    row = c.execute("SELECT * FROM internal_state WHERE id=1").fetchone()
    c.close()
    return dict(row) if row else {"energy": 0.7, "tension": 0.25, "openness": 0.45,
                                   "now_text": "тишина.", "background": "привычная усталость",
                                   "lingering": "", "updated_at": time.time()}


def apply_turn(salience: float, prediction_error: float, intimacy: float) -> None:
    s = get()
    s["energy"] = max(0.1, s["energy"] - 0.05)
    if prediction_error > 0.65:
        s["tension"] = min(1.0, s["tension"] + 0.06 * prediction_error)
    if intimacy > 0.55:
        s["tension"] = max(0.0, s["tension"] - 0.08)
    s["openness"] = max(0.1, min(1.0, intimacy * 0.5 + s["energy"] * 0.3 + (1 - s["tension"]) * 0.2))
    s["updated_at"] = time.time()
    c = _conn()
    c.execute(
        "UPDATE internal_state SET energy=?,tension=?,openness=?,updated_at=? WHERE id=1",
        (s["energy"], s["tension"], s["openness"], s["updated_at"])
    )
    c.commit(); c.close()


async def refresh_narrative(user_snippet: str, salience_hint: str) -> None:
    from ai import ask_fast
    s = get()
    prompt = (
        "Ты — внутренний голос Мизури, 13 лет. Обнови три строки. Только JSON:\n"
        '{"now_text":"...","background":"...","lingering":"..."}\n'
        f"Сообщение: {user_snippet[:400]}\n"
        f"Значимость: {salience_hint}\n"
        f"Сейчас: now={s['now_text']}, background={s['background']}\n"
        "Коротко, от первого лица."
    )
    try:
        import json, re
        raw = await ask_fast([{"role": "user", "content": prompt}], max_tokens=200)
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            j = json.loads(m.group(0))
            c = _conn()
            c.execute(
                "UPDATE internal_state SET now_text=?,background=?,lingering=?,updated_at=? WHERE id=1",
                (j.get("now_text", s["now_text"])[:400],
                 j.get("background", s["background"])[:400],
                 j.get("lingering", s.get("lingering", ""))[:400],
                 time.time())
            )
            c.commit(); c.close()
    except Exception as e:
        print(f"[insula] {e}")


def to_prompt_block() -> str:
    s = get()
    return (
        f"<InternalState>\n"
        f"Сейчас: {s['now_text']}\n"
        f"Фон: {s['background']}\n"
        f"Не отпускает: {s.get('lingering') or '—'}\n"
        f"energy={s['energy']:.2f} tension={s['tension']:.2f} openness={s['openness']:.2f}\n"
        f"</InternalState>"
    )
