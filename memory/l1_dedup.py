"""
L1 Dedup — порт l1-dedup.ts + prompts/l1-dedup.ts.
Batch conflict detection: vector recall → один LLM-вызов на весь батч.
"""
from __future__ import annotations
import json, re
from .db import cosine


SYSTEM_PROMPT = """Ты — детектор конфликтов памяти. Сравни новые воспоминания с пулом существующих и реши что делать с каждым.

**Язык**: merged_content — язык существующих записей; JSON-ключи — английские.

## Правила

- **store**: новая информация, добавить.
- **skip**: уже есть лучше, игнорировать.
- **update**: то же событие/факт, новое точнее — заменить старое.
- **merge**: взаимодополняющие записи — объединить в одну.

Можно объединять разные типы (persona/episodic/instruction) если они об одном.
target_ids — массив ID записей для удаления/замены.

## Формат вывода — строго JSON-массив:
[
  {
    "record_id": "id новой записи",
    "action": "store|skip|update|merge",
    "target_ids": [],
    "merged_content": "итоговый текст (для update/merge)",
    "merged_type": "persona|episodic|instruction (для update/merge)",
    "merged_priority": 80,
    "merged_timestamps": []
  }
]
Только JSON."""


def _find_candidates(new_vec: list[float] | None, existing: list[dict], top_k: int = 5) -> list[dict]:
    if not new_vec or not existing:
        return []
    scored = []
    for rec in existing:
        # existing записи не имеют vec в памяти — используем только те что есть в индексе
        from . import l1 as l1_store
        vec = l1_store._vec_index.get(rec["record_id"])
        if vec:
            s = cosine(new_vec, vec)
            if s > 0.4:
                scored.append((s, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:top_k]]


def _format_prompt(memories: list[dict], existing: list[dict], vecs: list) -> str:
    # Строим unified pool
    pool: dict[str, dict] = {}
    per_mem_candidates: dict[str, list[str]] = {}

    for mem, vec in zip(memories, vecs):
        candidates = _find_candidates(vec, existing)
        cids = []
        for c in candidates:
            if c["record_id"] not in pool:
                pool[c["record_id"]] = c
            cids.append(c["record_id"])
        per_mem_candidates[mem["record_id"]] = cids

    pool_list = [
        {"record_id": r["record_id"], "content": r["content"], "type": r["type"],
         "priority": r["priority"], "scene_name": r.get("scene_name", "")}
        for r in pool.values()
    ]
    pool_section = (
        f"## Пул существующих записей ({len(pool_list)} шт.)\n\n{json.dumps(pool_list, ensure_ascii=False, indent=2)}"
        if pool_list else "## Пул существующих записей\n\n(пусто — все новые записи сохранить)"
    )

    parts = []
    for i, mem in enumerate(memories):
        cids = per_mem_candidates.get(mem["record_id"], [])
        mem_json = json.dumps({
            "record_id": mem["record_id"],
            "content": mem["content"],
            "type": mem["type"],
            "priority": mem.get("priority", 50),
            "scene_name": mem.get("scene_name", ""),
        }, ensure_ascii=False, indent=2)
        related = json.dumps(cids) if cids else "[] (нет похожих — store)"
        parts.append(f"### Запись {i+1} (record_id: {mem['record_id']})\n{mem_json}\n\n【Связанные ID】{related}")

    return (
        f"{pool_section}\n\n{'═'*50}\n\n"
        f"## Новые записи ({len(memories)} шт.)\n\n"
        + "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n".join(parts)
        + "\n\nВыведи JSON-массив решений."
    )


def _parse(raw: str, memories: list[dict]) -> list[dict]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    m = re.search(r"\[[\s\S]*\]", cleaned)
    decisions = []
    if m:
        try:
            decisions = json.loads(m.group(0))
        except Exception:
            pass
    # Заполняем пропущенные решения как store
    decided = {d["record_id"] for d in decisions if "record_id" in d}
    for mem in memories:
        if mem["record_id"] not in decided:
            decisions.append({"record_id": mem["record_id"], "action": "store", "target_ids": []})
    return decisions


async def batch_dedup(
    memories: list[dict],
    existing: list[dict],
    vecs: list,
) -> list[dict]:
    """
    Batch conflict detection.
    memories: список с полем record_id (временный).
    existing: текущие L1 записи сессии.
    vecs: embeddings для memories (параллельный список).
    """
    from ai import ask_fast

    if not memories:
        return []

    # Проверяем есть ли вообще кандидаты
    has_candidates = any(
        bool(_find_candidates(vec, existing))
        for vec in vecs
    )
    if not has_candidates:
        return [{"record_id": m["record_id"], "action": "store", "target_ids": []} for m in memories]

    prompt = _format_prompt(memories, existing, vecs)
    try:
        raw = await ask_fast([{"role": "user", "content": prompt}], max_tokens=600,
                             system=SYSTEM_PROMPT)
        return _parse(raw, memories)
    except Exception as e:
        print(f"[l1_dedup] {e}")
        return [{"record_id": m["record_id"], "action": "store", "target_ids": []} for m in memories]
