# 🌸 Mizuri — AI VTuber with Persistent Memory

Полноценный AI VTuber-персонаж с Live2D-аватаром, стримингом на Twitch, многоуровневой долгосрочной памятью и динамическим эмоциональным состоянием. Mizuri — меланхоличный AI-персонаж, который помнит каждого зрителя и развивается со временем.

> 💡 Проект создан по подобию [Neuro-sama](https://www.twitch.tv/vedal987) — первого AI VTuber, разработанного Vedal987. Mizuri реализует ту же концепцию: живой AI-стример с собственной личностью, долгосрочной памятью и эмоциональным состоянием.

## Возможности

### Стриминг
- **Live2D аватар** — рендеринг модели в реальном времени (1920×1080, 60 FPS) через PyOpenGL/EGL
- **Прямой RTMP-поток** — трансляция на Twitch через FFmpeg
- **Fish Speech TTS** — синтез речи с синхронизацией движения рта (mouth_open параметр)
- **Субтитры** — отображение текста в реальном времени через HTTP
- **Twitch IRC** — чтение чата, ответы на @mizu

### Интерактивные игры
- **!акинатор** — зрители загадывают персонажа, Mizuri угадывает через 20 вопросов
- **!vote** — голосования по любому вопросу
- **!quiz** — викторина на заданную тему
- **HTTP чат** — внешние сервисы могут отправлять сообщения через `POST localhost:5001`

### Многоуровневая память
- **L0** — сырые логи всех диалогов (каждая реплика)
- **L1** — атомарные факты, извлечённые GPT из диалогов
- **L1 Dedup** — дедупликация повторяющихся фактов
- **L2** — сжатие и архивирование устаревших фактов
- **L3** — долгосрочная персона каждого зрителя (до 2000 символов)
- **Semantic Recall** — поиск релевантных воспоминаний через embeddings при генерации ответа

### Эмоциональное состояние
- **6 осей** — valence, arousal, sociality, existential, irritation, vulnerability
- **Затухание** — эмоции плавно возвращаются к базовому состоянию (меланхолия)
- **Реакция на события** — диалог с интересными зрителями поднимает настроение

### Telegram debug-бот
- `/memory` — L1-атомы пользователя
- `/persona` — L3-персона
- `/state` — внутреннее состояние (insula)
- `/beliefs` — убеждения персонажа
- `/emotion` — текущее эмоциональное состояние
- `/consolidate` — принудительная консолидация L2+L3

## Стек

| Компонент | Технология |
|---|---|
| Язык | Python 3.11+ |
| Рендеринг | PyOpenGL + Live2D Cubism SDK |
| Захват кадров | OpenCV + NumPy |
| Стриминг | FFmpeg (RTMP) |
| Основная LLM | Claude (Sonnet) через Lightning.ai |
| Компрессия памяти | GPT-4o-mini через gen-api.ru |
| Embeddings | OpenAI `text-embedding-3-small` |
| TTS | Fish Speech (локальный HTTP-сервер) |
| База данных | SQLite |
| Telegram | python-telegram-bot |

## Архитектура памяти

```
Диалог со зрителем
        ↓
   L0 Recorder
   (сырые логи)
        ↓
 L1 Extractor (GPT)
 (факты из диалога)
        ↓
   L1 Dedup
(удаление дублей)
        ↓
  L2 Compressor
(сжатие старых фактов)
        ↓
  L3 Persona (GPT)
(портрет зрителя)
        ↓
  Recall Engine
(embedding-поиск при следующем диалоге)
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
# Запустить Fish Speech TTS сервер (отдельно)
# Запустить Live2D рендер-сервер
python server.py &
# Запустить основной стрим-цикл
python stream_main.py
```

### Запуск только Telegram debug-бота

```bash
python tg_bot.py
```

## Переменные окружения (.env)

| Переменная | Описание |
|---|---|
| `GENAPI_KEY` | API ключ gen-api.ru (GPT / embeddings) |
| `LIGHTNING_KEY` | API ключ Lightning.ai (Claude) |
| `TWITCH_STREAM_KEY` | Ключ стрима Twitch |
| `MODEL_PATH` | Путь к Live2D модели `.model3.json` |
| `TG_BOT_TOKEN` | Токен Telegram debug-бота |

## Эмоциональная модель

| Ось | Базовое | Описание |
|---|---|---|
| `valence` | −0.2 | Позитивность / негативность |
| `arousal` | −0.1 | Активность / пассивность |
| `sociality` | −0.1 | Открытость / замкнутость |
| `existential` | +0.4 | Экзистенциальная тревога |
| `irritation` | 0.0 | Раздражение |
| `vulnerability` | +0.1 | Уязвимость |

## Структура проекта

```
mizuri/
├── stream_main.py        # Главный стрим-цикл (LLM + TTS + IRC + игры)
├── server.py             # OpenGL/Live2D рендер-сервер
├── audio_feeder.py       # TTS аудиофидер (PCM очередь)
├── tg_bot.py             # Telegram debug-бот
├── personality.py        # Эмоциональное состояние (6 осей)
├── ai.py                 # Клиент к Claude / GPT
├── gen_song.py           # Генерация песен
├── bg_capture.py         # Захват фона
├── config.py             # Конфигурация
├── stream_memory.py      # Интерфейс памяти для стрима
├── view_model.py         # Модель зрителя
├── memory/
│   ├── db.py             # SQLite схема и подключение
│   ├── embeddings.py     # OpenAI embeddings
│   ├── l0.py             # Raw log recorder
│   ├── l1.py             # Fact store
│   ├── l1_extractor.py   # GPT-экстракция фактов
│   ├── l1_dedup.py       # Дедупликация
│   ├── l2.py             # Компрессор сцен
│   ├── l3.py             # Генератор персоны
│   ├── insula.py         # Внутреннее состояние
│   └── recall.py         # Semantic search
├── data/
│   ├── initial_beliefs.json   # Начальные убеждения Mizuri
│   └── initial_interests.json # Начальные интересы
├── libs/                 # Live2D / Pixi.js
├── viewer.html           # Live2D веб-вьюер
└── overlay_tools.html    # Стрим-оверлей инструменты
```
