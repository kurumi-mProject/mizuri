import asyncio
import httpx
import config

async def ask_claude(system: str, history: list[dict], user_message: str,
                     max_tokens: int = 600, image_b64: str = None) -> str:
    messages = [{"role": "system", "content": system}]
    messages += history[-12:]
    if image_b64:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": user_message or "что на фото?"}
        ]
    else:
        content = user_message
    messages.append({"role": "user", "content": content})
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    f"{config.LIGHTNING_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {config.LIGHTNING_KEY}"},
                    json={"model": config.MAIN_MODEL, "messages": messages, "max_tokens": max_tokens}
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
        except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
            print(f"[ai] retry {attempt+1}/2 после {type(e).__name__}")

async def ask_fast(messages: list[dict], max_tokens: int = 200, system: str = None) -> str:
    """Быстрый вызов — для анализа, extraction, dedup."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{config.LIGHTNING_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.LIGHTNING_KEY}"},
            json={"model": config.MAIN_MODEL, "messages": msgs, "max_tokens": max_tokens}
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

async def analyze_user_update(mizuri_reply: str, user_message: str, current_model: dict) -> dict:
    """После ответа анализирует что изменилось в отношениях — возвращает обновления для user_model"""
    prompt = (
        "Проанализируй этот обмен сообщениями и верни JSON с обновлениями модели пользователя.\n"
        f"Сообщение пользователя: {user_message}\n"
        f"Ответ Мизури: {mizuri_reply}\n"
        f"Текущее мнение Мизури о пользователе: {current_model.get('mizuri_opinion','')}\n\n"
        "Верни JSON: {\"mizuri_opinion\": str, \"their_current_state\": str, "
        "\"what_they_want\": str, \"relation_delta\": float (-0.1..+0.1), "
        "\"new_fact\": str или null, \"emotion_triggered\": str или null, "
        "\"unresolved_thread\": str или null}\n"
        "ТОЛЬКО JSON."
    )
    try:
        raw = await ask_fast([{"role": "user", "content": prompt}], max_tokens=250)
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
    except Exception as e:
        print(f"[ai] analyze_user_update error: {e}")
    return {}

async def tom_revision(user_message: str, mizuri_reply: str, current_model: dict, recent_memories: list[str] = None) -> dict:
    """
    Theory of Mind revision: пересматривает модель пользователя после диалога.
    Возвращает {revised_opinion, belief_correction} или пустой dict при ошибке.
    """
    opinion = current_model.get("mizuri_opinion", "")
    if not opinion:
        return {}
    facts = current_model.get("facts", [])
    mem_block = ""
    if recent_memories:
        mem_block = "Что ты знаешь об этом человеке из памяти:\n" + "\n".join(f"• {m}" for m in recent_memories[:5]) + "\n\n"
    elif facts:
        mem_block = "Факты об этом человеке:\n" + "\n".join(f"• {f}" for f in facts[:5]) + "\n\n"
    prompt = (
        "Ты — Мизури, девочка 13 лет, живая личность. Пиши от первого лица, в женском роде. Не называй себя ботом или ИИ.\n"
        f"{mem_block}"
        f"До этого разговора ты думала о человеке: «{opinion}»\n"
        f"Человек написал: «{user_message}»\n"
        f"Ты ответила: «{mizuri_reply}»\n\n"
        "Пересмотри свою модель с учётом всего что знаешь. Твоё прежнее понимание подтвердилось или что-то изменилось?\n"
        "Верни JSON: {\"revised_opinion\": str, \"belief_correction\": str или null}\n"
        "revised_opinion — обновлённое мнение (1-2 предложения от первого лица, женский род).\n"
        "belief_correction — что именно изменилось, или null если всё совпало.\n"
        "ТОЛЬКО JSON."
    )
    try:
        raw = await ask_fast([{"role": "user", "content": prompt}], max_tokens=300)
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
    except Exception as e:
        print(f"[ai] tom_revision error: {e}")
    return {}

import json
