"""
L3 Persona Generator — порт persona-generator.ts + prompts/persona-generation.ts.
Генерирует/обновляет persona.md из L2 сцен.
"""
from __future__ import annotations
import re
from pathlib import Path
from datetime import datetime, timezone
from .db import _conn

ROOT = Path(__file__).resolve().parent.parent
SCENES_DIR = ROOT / "data" / "scene_blocks"
PERSONA_DIR = ROOT / "data" / "personas"

SYSTEM_PROMPT = """# Архитектор персоны — протокол инкрементальной эволюции

Анализируй сцены и создавай/обновляй профиль пользователя в `persona.md`.

## Запрещено
- Длина > 2000 символов
- Домыслы без доказательств из сцен
- Информация не из сцен

## Четырёхслойное сканирование

### Слой 1: Базовые факты
Демография, текущий статус, подтверждённые факты.

### Слой 2: Карта интересов
Активные увлечения / пассивное потребление / спящие интересы.

### Слой 3: Протокол взаимодействия
Стиль общения, предпочтения, «красные линии».

### Слой 4: Когнитивное ядро
Логика решений, противоречия, главный мотиватор.

## Шаблон persona.md:
```
# Профиль пользователя

> **Архетип**: [одна фраза]

> **Базовая информация**
- ...

> **Долгосрочные предпочтения**
- ...

## Глава 1: Контекст и текущее состояние
[связный абзац]

## Глава 2: Текстура жизни
[интересы, привычки, вкусы]

## Глава 3: Протокол взаимодействия
### 3.1 Как говорить
### 3.2 Как думать

## Глава 4: Глубокие инсайты
- **Противоречия**: ...
- **Эволюция**: ...
- **Теги**: `тег` — пояснение
```

Выведи ТОЛЬКО содержимое файла persona.md, без JSON и пояснений."""


def _load_scenes(session_key: str) -> str:
    session_dir = SCENES_DIR / session_key
    if not session_dir.exists():
        return ""
    parts = []
    for f in session_dir.glob("*.md"):
        content = f.read_text("utf-8")
        if content.strip() == "[DELETED]":
            continue
        parts.append(f"### {f.name}\n{content[:1500]}")
    return "\n\n".join(parts)


def _load_persona(session_key: str) -> str:
    path = PERSONA_DIR / session_key / "persona.md"
    if path.exists():
        return path.read_text("utf-8")
    return ""


async def run_l3(session_key: str) -> bool:
    """
    Генерирует/обновляет persona.md для session_key.
    Возвращает True если обновлено.
    """
    from ai import ask_fast

    scenes_content = _load_scenes(session_key)
    if not scenes_content:
        return False

    existing_persona = _load_persona(session_key)
    now = datetime.now(timezone.utc).isoformat()

    c = _conn()
    meta = c.execute(
        "SELECT scene_count, atom_count FROM l3_persona_meta WHERE session_key=?",
        (session_key,)
    ).fetchone()
    c.close()

    mode = "первичная генерация" if not existing_persona else "инкрементальное обновление"

    user_prompt = (
        f"**Время**: {now}\n"
        f"**Режим**: {mode}\n\n"
        f"## Изменённые сцены\n\n{scenes_content}\n\n"
        + (f"## Текущая persona.md\n\n```markdown\n{existing_persona}\n```\n" if existing_persona else "")
    )

    new_persona = await ask_fast(
        [{"role": "user", "content": user_prompt}],
        max_tokens=1500,
        system=SYSTEM_PROMPT
    )

    if not new_persona or len(new_persona.strip()) < 50:
        return False

    # Убираем markdown-обёртку если есть
    cleaned = new_persona.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:markdown)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    persona_path = PERSONA_DIR / session_key / "persona.md"
    persona_path.parent.mkdir(parents=True, exist_ok=True)
    persona_path.write_text(cleaned, encoding="utf-8")

    c = _conn()
    c.execute(
        """INSERT INTO l3_persona_meta (session_key, updated_at, scene_count, atom_count)
           VALUES (?,?,?,?)
           ON CONFLICT(session_key) DO UPDATE SET updated_at=excluded.updated_at""",
        (session_key, now, meta["scene_count"] if meta else 0, meta["atom_count"] if meta else 0)
    )
    c.commit(); c.close()
    return True


def get_persona(session_key: str) -> str:
    return _load_persona(session_key)
