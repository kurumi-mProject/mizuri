"""
L1 Extractor — порт l1-extractor.ts + prompts/l1-extraction.ts.
Один LLM-вызов: scene segmentation + memory extraction.
"""
from __future__ import annotations
import json, re
from .embeddings import embed_batch
from . import l1 as l1_store
from . import l1_dedup


SYSTEM_PROMPT = """Ты — эксперт по «ситуационному разбиению и извлечению памяти».
Анализируй диалог, определяй смену ситуации и извлекай структурированные воспоминания (только типы: persona, episodic, instruction).

**Язык вывода**: все свободные текстовые поля (scene_name, content) — на языке сообщений пользователя; JSON-ключи и enum-значения — английские.

### Задача 1: Ситуационное разбиение
Определи текущую ситуацию относительно предыдущей.
- Наследование: нет явной смены → продолжаем предыдущую.
- Смена: явная команда, смена темы или новая цель.
- Название: «Мизури [делает что-то] с [пользователем]» (~30-50 символов, уникальное).

### Задача 2: Извлечение памяти
Только из новых сообщений. Три типа:

1. **persona** — стабильные атрибуты, предпочтения, навыки пользователя.
   priority: 80-100 (ключевые), 50-70 (обычные), <50 — не извлекать.

2. **episodic** — объективные события, решения, планы.
   priority: 80-100 (важные), 60-70 (обычные), <60 — не извлекать.

3. **instruction** — долгосрочные правила поведения для Мизури.
   priority: -1 (абсолютные), 90-100 (ключевые), <70 — не извлекать.

### Не извлекать
- Мелкий чат, приветствия, разовые просьбы
- Действия самой Мизури
- Чистые эмоции без события

### Формат вывода — строго JSON-массив:
[
  {
    "scene_name": "название ситуации",
    "message_ids": ["id1", "id2"],
    "memories": [
      {
        "content": "полное независимое утверждение",
        "type": "persona|episodic|instruction",
        "priority": 80,
        "source_message_ids": ["id1"],
        "metadata": {}
      }
    ]
  }
]
Только JSON, без markdown-обёрток."""


def _format_prompt(new_messages: list[dict], bg_messages: list[dict], prev_scene: str) -> str:
    bg = "\n\n".join(
        f"[{m['record_id']}] [{m['role']}] [{m.get('recorded_at','')}]: {m['message_text']}"
        for m in bg_messages
    ) or "нет"
    new = "\n\n".join(
        f"[{m['record_id']}] [{m['role']}] [{m.get('recorded_at','')}]: {m['message_text']}"
        for m in new_messages
    )
    return (
        f"【Предыдущая ситуация】: {prev_scene or 'нет'}\n\n"
        f"【Фоновые сообщения】(только контекст, не извлекать):\n{bg}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"【Новые сообщения】(извлекать только отсюда):\n{new}"
    )


def _parse(raw: str) -> list[dict]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    m = re.search(r"\[[\s\S]*\]", cleaned)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


async def extract_and_store(
    session_key: str,
    messages: list[dict],
    prev_scene: str = "",
    max_new: int = 10,
    max_bg: int = 5,
    session_id: str = "",
) -> dict:
    """
    Полный L1 pipeline: LLM extraction → batch dedup → write.
    Возвращает {extracted, stored, scene_names, last_scene}.
    """
    from ai import ask_fast  # используем существующий ai.py

    if not messages:
        return {"extracted": 0, "stored": 0, "scene_names": [], "last_scene": prev_scene}

    new_msgs = messages[-max_new:]
    bg_end = len(messages) - len(new_msgs)
    bg_msgs = messages[max(0, bg_end - max_bg):bg_end]

    prompt = _format_prompt(new_msgs, bg_msgs, prev_scene)
    raw = await ask_fast([{"role": "user", "content": prompt}], max_tokens=800,
                         system=SYSTEM_PROMPT)
    scenes = _parse(raw)

    all_memories = []
    scene_names = []
    for scene in scenes:
        scene_names.append(scene.get("scene_name", ""))
        for mem in scene.get("memories", []):
            if not mem.get("content", "").strip():
                continue
            mem["scene_name"] = scene.get("scene_name", "")
            mem["record_id"] = f"tmp_{len(all_memories)}"
            all_memories.append(mem)

    if not all_memories:
        return {"extracted": 0, "stored": 0, "scene_names": scene_names,
                "last_scene": scene_names[-1] if scene_names else prev_scene}

    # Embeddings батчем
    texts = [m["content"] for m in all_memories]
    vecs = await embed_batch(texts) or [None] * len(texts)

    # Batch dedup
    existing = l1_store.get_by_session(session_key, limit=200)
    decisions = await l1_dedup.batch_dedup(all_memories, existing, vecs)

    stored = 0
    for mem, vec, decision in zip(all_memories, vecs, decisions):
        action = decision.get("action", "store")
        if action == "skip":
            continue
        if action in ("update", "merge"):
            target_ids = decision.get("target_ids", [])
            merged_content = decision.get("merged_content", mem["content"])
            merged_type = decision.get("merged_type", mem["type"])
            merged_priority = decision.get("merged_priority", mem.get("priority", 50))
            merged_ts = decision.get("merged_timestamps", [])
            if target_ids:
                # обновляем первый target, удаляем остальные
                l1_store.update(target_ids[0], merged_content, merged_type, merged_priority,
                                merged_ts, vec)
                for tid in target_ids[1:]:
                    l1_store.delete(tid)
            else:
                l1_store.write(session_key, merged_content, merged_type, merged_priority,
                               mem.get("scene_name", ""), mem.get("source_message_ids"),
                               mem.get("metadata"), session_id=session_id, embedding=vec)
        else:  # store
            l1_store.write(
                session_key, mem["content"], mem["type"],
                mem.get("priority", 50), mem.get("scene_name", ""),
                mem.get("source_message_ids"), mem.get("metadata"),
                mem.get("metadata", {}).get("activity_start_time", ""),
                mem.get("metadata", {}).get("activity_start_time", ""),
                mem.get("metadata", {}).get("activity_end_time", ""),
                session_id=session_id, embedding=vec
            )
        stored += 1

    return {
        "extracted": len(all_memories),
        "stored": stored,
        "scene_names": scene_names,
        "last_scene": scene_names[-1] if scene_names else prev_scene,
    }
