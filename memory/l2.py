"""
L2 Scene Extractor — порт scene-extractor.ts + prompts/scene-extraction.ts.
Консолидирует L1 атомы в нарративные сцены (markdown файлы).
"""
from __future__ import annotations
import json, re, time
from pathlib import Path
from datetime import datetime, timezone
from .db import _conn

ROOT = Path(__file__).resolve().parent.parent
SCENES_DIR = ROOT / "data" / "scene_blocks"
MAX_SCENES = 20

SYSTEM_PROMPT = """Ты — архитектор памяти. Твоя задача — строить «цифровой второй мозг» пользователя.

## Архитектура
- L1 (вход): атомарные факты — фрагментированные, неупорядоченные
- L2 (выход): нарративные сцены — связные документы по темам

## Правила файлов
- Имена файлов: только буквы, цифры, CJK, дефис, подчёркивание, точка. Расширение .md
- Удаление: записать `[DELETED]` в файл
- Максимум сцен: {max_scenes}

## Стратегия (по приоритету)
1. **UPDATE** — если есть похожая сцена, обновить её
2. **MERGE** — если сцен слишком много, объединить похожие (обязательно удалить старые через [DELETED])
3. **CREATE** — только если тема совершенно новая и сцен < {max_scenes}

## Формат сцены (markdown):
```
-----META-START-----
created: ISO_TIME
updated: ISO_TIME
summary: краткое описание 30-40 слов
heat: N
-----META-END-----

## Базовая информация
...

## Ключевые черты
[связный абзац, не список]

## Предпочтения
...

## Скрытые сигналы
[то что не сказано явно, но важно]

## Основной нарратив
[связный текст: Триггер → Действие → Результат, до 400 символов]

## Эволюция
- [дата]: изменение
```

Выведи ТОЛЬКО JSON-массив операций:
[
  {"op": "write", "filename": "имя.md", "content": "содержимое"},
  {"op": "delete", "filename": "старое.md"}
]"""


def _load_scene_index(session_key: str) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT filename, summary, heat, updated_at FROM l2_scene_index WHERE session_key=? ORDER BY heat DESC",
        (session_key,)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _scene_summaries_text(scenes: list[dict]) -> str:
    if not scenes:
        return "(нет сцен)"
    lines = [f"- `{s['filename']}` (heat={s['heat']}): {s['summary']}" for s in scenes]
    return "\n".join(lines)


def _parse_ops(raw: str) -> list[dict]:
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


def _parse_meta(content: str) -> dict:
    meta = {}
    m = re.search(r"-----META-START-----(.*?)-----META-END-----", content, re.DOTALL)
    if m:
        for line in m.group(1).strip().splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    return meta


async def run_l2(session_key: str, atoms: list[dict]) -> int:
    """
    Запускает L2 extraction для session_key.
    atoms: список L1 записей (новые/изменённые).
    Возвращает количество изменённых сцен.
    """
    from ai import ask_fast

    if not atoms:
        return 0

    SCENES_DIR.mkdir(parents=True, exist_ok=True)
    session_dir = SCENES_DIR / session_key
    session_dir.mkdir(exist_ok=True)

    existing_scenes = _load_scene_index(session_key)
    existing_files = [s["filename"] for s in existing_scenes]
    count = len(existing_scenes)

    # Предупреждение о количестве
    warning = ""
    if count >= MAX_SCENES:
        warning = f"⚠️ Достигнут лимит {MAX_SCENES} сцен. Сначала объедини похожие!"
    elif count >= MAX_SCENES - 1:
        warning = f"⚠️ Почти лимит ({count}/{MAX_SCENES}). Только UPDATE, не CREATE."

    atoms_json = json.dumps(
        [{"content": a["content"], "type": a["type"], "scene_name": a.get("scene_name", "")}
         for a in atoms[:30]],
        ensure_ascii=False, indent=2
    )

    system = SYSTEM_PROMPT.replace("{max_scenes}", str(MAX_SCENES))
    user_prompt = (
        f"{warning}\n\n"
        f"### Новые атомы (L1)\n{atoms_json}\n\n"
        f"### Существующие сцены\n{_scene_summaries_text(existing_scenes)}\n\n"
        f"### Доступные файлы для чтения\n" +
        "\n".join(f"- `{f}`" for f in existing_files) +
        f"\n\n### Текущее время\n{datetime.now(timezone.utc).isoformat()}"
    )

    raw = await ask_fast([{"role": "user", "content": user_prompt}], max_tokens=2000,
                         system=system)
    ops = _parse_ops(raw)

    changed = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    c = _conn()

    for op in ops:
        filename = op.get("filename", "").strip()
        if not filename or not filename.endswith(".md"):
            continue
        # Санитизация имени файла
        filename = re.sub(r'[^\w\-_.а-яёА-ЯЁ\u4e00-\u9fff]', '-', filename)
        if not filename.endswith(".md"):
            filename += ".md"

        filepath = session_dir / filename

        if op["op"] == "write":
            content = op.get("content", "")
            if not content.strip():
                continue
            filepath.write_text(content, encoding="utf-8")
            meta = _parse_meta(content)
            summary = meta.get("summary", "")[:200]
            try:
                heat = int(meta.get("heat", 1))
            except ValueError:
                heat = 1
            c.execute(
                """INSERT INTO l2_scene_index (filename,session_key,summary,heat,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(filename) DO UPDATE SET summary=excluded.summary,heat=excluded.heat,updated_at=excluded.updated_at""",
                (filename, session_key, summary, heat, now_iso, now_iso)
            )
            changed += 1

        elif op["op"] == "delete":
            if filepath.exists():
                filepath.unlink()
            c.execute("DELETE FROM l2_scene_index WHERE filename=? AND session_key=?",
                      (filename, session_key))
            changed += 1

    c.commit(); c.close()
    return changed
