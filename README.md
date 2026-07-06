# 🌸 Mizuri — AI VTuber with Persistent Memory

Полноценный AI VTuber-персонаж с Live2D-аватаром, стримингом на Twitch, многоуровневой долгосрочной памятью и динамическим эмоциональным состоянием. Mizuri — меланхоличный AI-персонаж, который помнит каждого зрителя и развивается со временем.

## Возможности

- **Live2D аватар** — рендеринг модели в реальном времени через OpenGL/EGL (1920×1080, 60 FPS)
- **Стриминг на Twitch** — прямой RTMP-поток через FFmpeg
- **Многоуровневая память**:
  - `L0` — сырые логи диалогов
  - `L1` — атомарные факты (экстракция через GPT)
  - `L2` — дедупликация и сжатие
  - `L3` — долгосрочная персона пользователя
- **Эмоциональное состояние** — 6 осей (valence, arousal, sociality, existential, irritation, vulnerability) с затуханием к базовому состоянию
- **Semantic recall** — поиск релевантных воспоминаний через embeddings (OpenAI)
- **TTS + синхронизация рта** — аудиофидер с Live2D mouth-open параметром
- **Telegram-бот** — тестирование памяти и состояния через команды
- **Убеждения и интересы** — начальная личность из JSON, развивается в процессе

## Стек

- Python 3.11+
- [Live2D Cubism SDK](https://www.live2d.com/en/sdk/about/) + PyOpenGL
- OpenCV + NumPy — обработка кадров
- FFmpeg — стриминг RTMP
- Claude (Haiku) через Lightning.ai — основная модель
- GPT-4o-mini через gen-api.ru — компрессия памяти
- OpenAI Embeddings (`text-embedding-3-small`) — семантический поиск
- SQLite — хранение памяти
- python-telegram-bot — Telegram интерфейс
- Svelte (viewer.html, overlay_tools.html) — веб-оверлей

## Архитектура памяти

```
Диалог → L0 (raw log)
           ↓
        L1 Extractor (GPT: факты из диалога)
           ↓
        L1 Dedup (удаление дублей)
           ↓
        L2 Compressor (сжатие устаревшего)
           ↓
        L3 Persona (долгосрочный портрет пользователя)
           ↓
        Recall (embedding-поиск при генерации ответа)
```

## Установка

```bash
git clone https://github.com/kurumi-mProject/mizuri.git
cd mizuri
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

### Запуск стрима

```bash
python stream_main.py
```

### Запуск Telegram-бота (тестирование памяти)

```bash
python tg_bot.py
```

### Генерация песни

```bash
python gen_song.py
```

## Переменные окружения (.env)

| Переменная | Описание |
|---|---|
| `GENAPI_KEY` | API ключ gen-api.ru (GPT / embeddings) |
| `LIGHTNING_KEY` | API ключ Lightning.ai (Claude) |
| `TWITCH_STREAM_KEY` | Ключ стрима Twitch |
| `MODEL_PATH` | Путь к Live2D модели `.model3.json` |
| `TG_BOT_TOKEN` | Токен Telegram-бота для отладки |

## Команды Telegram-бота

| Команда | Описание |
|---|---|
| `/memory` | L1-атомы текущего пользователя |
| `/persona` | L3-персона пользователя |
| `/state` | Внутреннее состояние (insula) |
| `/beliefs` | Убеждения персонажа |
| `/emotion` | Текущее эмоциональное состояние |
| `/consolidate` | Принудительная консолидация L2+L3 |
| `/reset` | Сброс памяти пользователя |

## Структура проекта

```
mizuri/
├── stream_main.py        # Главный стрим-цикл
├── server.py             # OpenGL/Live2D рендер-сервер
├── audio_feeder.py       # TTS аудиофидер
├── tg_bot.py             # Telegram debug-бот
├── personality.py        # Эмоциональное состояние
├── ai.py                 # Клиент к Claude / GPT
├── gen_song.py           # Генерация песен
├── bg_capture.py         # Захват фона
├── config.py             # Конфигурация
├── memory/
│   ├── db.py             # SQLite схема
│   ├── embeddings.py     # OpenAI embeddings
│   ├── l0.py             # Raw log recorder
│   ├── l1.py             # Fact extractor
│   ├── l1_dedup.py       # Дедупликация
│   ├── l1_extractor.py   # GPT-экстракция
│   ├── l2.py             # Компрессор
│   ├── l3.py             # Персона
│   ├── insula.py         # Внутреннее состояние
│   └── recall.py         # Semantic search
├── data/
│   ├── initial_beliefs.json   # Начальные убеждения
│   └── initial_interests.json # Начальные интересы
├── libs/                 # Live2D / Pixi.js
├── viewer.html           # Live2D веб-вьюер
└── overlay_tools.html    # Стрим-оверлей
```

## Эмоциональная модель

Персонаж имеет 6 эмоциональных осей, каждая плавно возвращается к базовому состоянию (меланхолия):

| Ось | Базовое | Описание |
|---|---|---|
| `valence` | −0.2 | Позитивность/негативность |
| `arousal` | −0.1 | Активность/пассивность |
| `sociality` | −0.1 | Открытость/замкнутость |
| `existential` | +0.4 | Экзистенциальная тревога |
| `irritation` | 0.0 | Раздражение |
| `vulnerability` | +0.1 | Уязвимость |
