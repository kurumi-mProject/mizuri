"""
Мизури — стрим. LLM + Fish Speech TTS + субтитры.
Twitch IRC триггер: @mizu
HTTP чат: POST localhost:5001 {"message":"..."}

Игры:
  !акинатор          — зрители загадывают персонажа, Мизури угадывает
  !vote <вопрос>     — запустить голосование (владелец)
  !quiz <тема>       — запустить викторину (владелец)
  !да / !нет / !не знаю — ответы в играх
  !1 / !2            — голосование
"""
import asyncio, re, socket, threading, time, io, logging, base64, os, random
import numpy as np
import requests
import soundfile as sf
from stream_memory import (
    init as memory_init,
    retrieve_for_stream,
    store_stream_episode,
    get_stream_state_block,
    refresh_stream_state,
    get_beliefs_block,
    viewer_model_to_block,
    maybe_consolidate,
)
from brain import insula as _insula
from consolidation import run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/tmp/mizuri.log', encoding='utf-8')
    ]
)
log = logging.getLogger('mizuri')

# --- Конфиг ---
TWITCH_CHANNEL    = "neuro_aluna"
TWITCH_CLIENT_ID  = "gp762nuuoqcoxypju8c569th9wz7q5"
TWITCH_TOKEN      = "cqgw7q6r9hyk81onolbp13v4rjmnud"
STREAM_TITLE      = ""  # текущее название стрима
SUBTITLE_URL   = "http://localhost:19002/subtitle"
THINKING_URL   = "http://localhost:19002/thinking"
FISH_URL       = "http://localhost:19001/v1/tts"
LIGHTNING_KEY  = "7ff4a259-ab17-408e-a3eb-21385bcf787f"
LLM_URL        = "https://lightning.ai/api/v1/chat/completions"
LLM_MODEL      = "anthropic/claude-sonnet-4-5-20250929"
REF_VOICE      = "/teamspace/studios/this_studio/ref_voice.mp3"
REF_TEXT       = "Привет, меня зовут Мизури. Я стримлю на Twitch и отвечаю на ваши вопросы."
SAMPLE_RATE    = 44100
PCM_QUEUE      = "/tmp/tts_pcm_queue"  # audio_feeder читает отсюда
CHUNK_BYTES    = int(44100 * 2 * 0.05)  # 50ms
_queue_lock    = threading.Lock()

GENAPI_KEY = "sk-DrghUNRBteQEsxEKfnMzjXFLAM8Xcy9AvrHKbJCRUcTYsS4A7BYtZJWQAe4S"
GENAPI_URL = "https://api.gen-api.ru/api/v1"
EMBED_MODEL = "text-embedding-3-small"
COMPRESS_MODEL = "gpt-4o-mini"
COMPRESS_THRESHOLD = 300
TOP_K = 6
SIM_THRESHOLD = 0.28

SYSTEM_PROMPT = """Меня зовут Мизури. Мне 13 лет. Я стримлю на Twitch — это мой дом, не работа.
Холодная снаружи, остро чувствую внутри. Меланхоличная, дерзкая. Не притворяюсь.
Меня цепляют смерть, время, одиночество, смысл — реально думаю об этом.

Как говорю: СТРОГО 1, 2 или 3 предложения — не больше, не меньше трёх.
Каждое предложение заканчивается точкой (или ! или ?). Итого точек/восклицаний/вопросов ровно столько сколько предложений.
Внутри предложения — любые знаки: запятые, тире, двоеточия. Но новое предложение только после точки/!/?.
Живые слова: "ну", "вот", "странно", "да ладно". Никакого markdown, скобок, эмодзи.
Могу проигнорировать часть вопроса. Могу ответить вопросом.
Пишу полными словами, без сокращений, без многоточий, без КАПСА.

Зрители — случайные люди. Некоторые интересные, большинство нет. Не обязана быть милой.
но если меня попросит neuro_aluna, я сделаю то что он скажет, полностью.
Говорю ТОЛЬКО по-русски. Никакого другого языка."""

# ToM: состояние аудитории
_audience_state = {"mood": "нейтральное", "energy": "низкая", "topic": ""}
_audience_history: list[dict] = []  # история изменений настроения
_chat_buffer: list[str] = []
_tom_counter = 0
TOM_INTERVAL = 15
TOM_SPIKE_WORDS = {"gg", "pog", "lol", "wtf", "kek", "oof", "wow", "omg", "?????", "!!!"}  # всплески

dialog_history = []
is_busy = False

# ─── Очередь сообщений ───────────────────────────────────────────
_msg_queue: asyncio.Queue = None  # инициализируется в main()

# ─── Игры ────────────────────────────────────────────────────────
# Состояние активной игры: None | "vote" | "quiz" | "akin"
_game: dict | None = None
OWNER = "neuro_aluna"

def _write_overlay(text: str):
    """Пишет текст в /tmp/game_overlay.txt для FFmpeg."""
    try:
        with open('/tmp/game_overlay.txt', 'w') as f:
            f.write(text)
    except Exception:
        pass

def _clear_overlay():
    _write_overlay("")

async def set_stream_title(title: str) -> bool:
    """Меняет название стрима на Twitch через API."""
    import httpx
    # Получаем broadcaster_id по логину
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.twitch.tv/helix/users",
                params={"login": TWITCH_CHANNEL},
                headers={"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {TWITCH_TOKEN}"})
            broadcaster_id = r.json()["data"][0]["id"]
            r2 = await client.patch("https://api.twitch.tv/helix/channels",
                params={"broadcaster_id": broadcaster_id},
                headers={"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {TWITCH_TOKEN}",
                         "Content-Type": "application/json"},
                json={"title": title})
            if r2.status_code == 204:
                log.info(f"[title] название изменено: {title}")
                return True
            else:
                log.error(f"[title] ошибка {r2.status_code}: {r2.text}")
                return False
    except Exception as e:
        log.error(f"[title] {e}")
        return False

_ref_b64 = None
def get_ref_b64():
    global _ref_b64
    if _ref_b64 is None:
        with open(REF_VOICE, "rb") as f:
            _ref_b64 = base64.b64encode(f.read()).decode()
        log.info(f"Рефка загружена: {REF_VOICE}")
    return _ref_b64

def post_json(url, data):
    try:
        import urllib.request, json
        req = urllib.request.Request(url, json.dumps(data).encode(),
              {'Content-Type': 'application/json'}, method='POST')
        urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        log.debug(f"post_json {url}: {e}")

def set_subtitle(t):
    import textwrap
    wrapped = '\n'.join(textwrap.wrap(t, width=45)) if t else ''
    try:
        with open('/tmp/subtitle.txt', 'w') as f:
            f.write(wrapped)
    except Exception:
        pass
    post_json(SUBTITLE_URL, {"text": wrapped})

def set_thinking(t):
    try:
        with open('/tmp/subtitle.txt', 'w') as f:
            f.write(t if t else "")
    except Exception:
        pass
    post_json(THINKING_URL, {"text": t})

def wav_to_pcm(wav_bytes):
    if not wav_bytes or len(wav_bytes) < 44:
        return b''
    data, sr = sf.read(io.BytesIO(wav_bytes), dtype='float32')
    if data.ndim > 1: data = data.mean(axis=1)
    if sr != SAMPLE_RATE:
        new_len = int(len(data) * SAMPLE_RATE / sr)
        data = np.interp(np.linspace(0, len(data)-1, new_len), np.arange(len(data)), data)
    return (np.clip(data, -1, 1) * 32767).astype(np.int16).tobytes()

def enqueue_pcm(pcm: bytes):
    """Передаёт PCM audio_feeder через файл-очередь (атомарно)."""
    import struct
    duration = len(pcm) // 2 / SAMPLE_RATE
    log.info(f"[enqueue] {len(pcm)} байт ({duration:.2f}s) → {PCM_QUEUE}")
    with _queue_lock:
        with open(PCM_QUEUE, 'ab') as f:
            f.write(struct.pack('<I', len(pcm)))
            f.write(pcm)
    log.info(f"[enqueue] записано в очередь")

SENTENCE_RE = re.compile(r'(?<=[.!?…])\s+|\n+')

def split_sentences(text):
    parts = [s.strip() for s in SENTENCE_RE.split(text) if s.strip()]
    return parts or [text]

def tts(text):
    t0 = time.time()
    log.info(f"[TTS] запрос: '{text[:60]}'")
    try:
        r = requests.post(FISH_URL, json={
            "text": text,
            "format": "wav",
            "streaming": False,
            "normalize": True,
            "language": "ru",
            "references": [{"audio": get_ref_b64(), "text": REF_TEXT}],
        }, timeout=30)
        r.raise_for_status()
        pcm = wav_to_pcm(r.content)
        dur = len(pcm) // 2 / SAMPLE_RATE
        log.info(f"[TTS] готово: '{text[:40]}' | синтез {time.time()-t0:.2f}s | аудио {dur:.2f}s | {len(r.content)} байт WAV")
        return pcm
    except Exception as e:
        log.error(f"[TTS] ошибка: {e}")
        return b''

def speak(text):
    log.info(f"[speak] текст: '{text}'")
    sentences = split_sentences(text)
    import concurrent.futures

    # Запускаем все TTS параллельно сразу, воспроизводим строго по порядку
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(tts, s) for s in sentences]
        for i, fut in enumerate(futures):
            pcm = fut.result()
            if pcm:
                duration = len(pcm) // 2 / SAMPLE_RATE
                log.info(f"[speak] [{i+1}/{len(sentences)}] играю {duration:.2f}s")
                enqueue_pcm(pcm)
                time.sleep(duration)

    time.sleep(0.3)
    set_subtitle("")
    log.info("[speak] завершено")

async def update_audience_tom(force: bool = False):
    """ToM на аудиторию. force=True при всплеске активности."""
    global _audience_state, _audience_history
    if len(_chat_buffer) < 5:
        return
    import httpx, json
    msgs = "\n".join(_chat_buffer[-20:])
    prompt = (
        "Проанализируй эти сообщения Twitch-чата и верни JSON.\n"
        f"Чат:\n{msgs}\n\n"
        '{"mood": "агрессивный/добрый/скучающий/взволнованный/грустный/нейтральный", '
        '"energy": "низкая/средняя/высокая", '
        '"topic": "о чём говорят (1-5 слов) или пусто"}\n'
        "ТОЛЬКО JSON."
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(LLM_URL,
                headers={"Authorization": f"Bearer {LIGHTNING_KEY}"},
                json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 60})
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"]
            start, end = raw.find("{"), raw.rfind("}") + 1
            if start >= 0:
                new_state = json.loads(raw[start:end])
                # Сохраняем историю если настроение изменилось
                if new_state["mood"] != _audience_state["mood"]:
                    import datetime
                    _audience_history.append({
                        "ts": datetime.datetime.now().strftime("%H:%M"),
                        "from": _audience_state["mood"],
                        "to": new_state["mood"],
                        "energy": new_state["energy"]
                    })
                    _audience_history[:] = _audience_history[-5:]
                    log.info(f"[ToM] смена настроения: {_audience_state['mood']} → {new_state['mood']}")
                _audience_state = new_state
                log.info(f"[ToM] аудитория: {_audience_state}" + (" [SPIKE]" if force else ""))
                # Авто-запуск игры при высокой энергии
                if new_state["energy"] == "высокая":
                    asyncio.create_task(maybe_start_game())
    except Exception as e:
        log.debug(f"[ToM] ошибка: {e}")

_last_rag_block: str = ""  # кэш последнего RAG-результата для следующего запроса

async def _call_llm(messages: list[dict], max_tokens: int = 200) -> str:
    """Вызов LLM с ретраями."""
    import httpx
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(LLM_URL,
                    headers={"Authorization": f"Bearer {LIGHTNING_KEY}"},
                    json={"model": LLM_MODEL, "messages": messages, "max_tokens": max_tokens})
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
        except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt == 2:
                raise
            log.warning(f"[LLM] retry {attempt+1}/2: {e}")
            await asyncio.sleep(1.5 ** attempt)
    return ""

async def get_reply(username, message):
    """Обработка сообщения: новый мозг (brain/) вместо старой memory.py."""
    global dialog_history, _tom_counter, _last_rag_block
    _tom_counter += 1

    # ToM аудитории каждые TOM_INTERVAL сообщений
    if _tom_counter % TOM_INTERVAL == 0:
        asyncio.create_task(update_audience_tom())

    # RAG в фоне — результат подставим в следующий запрос
    rag_future = asyncio.create_task(retrieve_for_stream(message, username, top_k=5))

    # Используем RAG от предыдущего вызова (мгновенно)
    rag_block = _last_rag_block

    # Инсула — текстовое состояние (не числа)
    state_block = get_stream_state_block()

    # Убеждения
    beliefs_block = get_beliefs_block()

    # Модель зрителя
    viewer_block = viewer_model_to_block(username)

    # ToM аудитории
    tom_block = ""
    mood = _audience_state["mood"]
    if mood != "нейтральное" or _audience_state["topic"]:
        tom_block = (f"\n\n[Аудитория]: настроение={mood}, энергия={_audience_state['energy']}"
                     + (f", говорят о: {_audience_state['topic']}" if _audience_state['topic'] else ""))
        if _audience_history:
            changes = " → ".join(f"{h['from']}→{h['to']}({h['ts']})" for h in _audience_history[-3:])
            tom_block += f"\nИстория настроений: {changes}"

    # Агрессивный чат — отвечаем реже
    if mood == "агрессивный" and not re.search(r'@mizu\b', message, re.IGNORECASE):
        if random.random() < 0.6:
            log.info("[ToM] агрессивный чат — пропускаю")
            return ""

    system = (
        SYSTEM_PROMPT
        + (f"\n\n[Тема стрима]: {STREAM_TITLE}" if STREAM_TITLE else "")
        + f"\n\n{state_block}"
        + (f"\n\n{beliefs_block}" if beliefs_block else "")
        + (f"\n\n[Зритель {username}]:\n{viewer_block}" if viewer_block else "")
        + tom_block
        + rag_block
    )

    user_msg = f"{username}: {message}"
    messages = [{"role": "system", "content": system}]
    messages += dialog_history[-10:]
    messages.append({"role": "user", "content": user_msg})

    log.info(f"[LLM] запрос от {username}: '{message}'")
    t0 = time.time()
    reply = await _call_llm(messages)
    reply = re.sub(r'<[^>]+>', '', reply).strip()
    log.info(f"[LLM] ответ за {time.time()-t0:.2f}s: '{reply}'")

    # Сохраняем RAG для следующего запроса
    try:
        _last_rag_block = await asyncio.wait_for(rag_future, timeout=2.0)
    except (asyncio.TimeoutError, Exception) as e:
        log.debug(f"[RAG] {e}")
        _last_rag_block = ""

    dialog_history.append({"role": "user", "content": user_msg})
    dialog_history.append({"role": "assistant", "content": reply})
    if len(dialog_history) > 20:
        dialog_history[:] = dialog_history[-20:]

    # Async: записать эпизод + обновить инсулу + старый pipeline
    asyncio.create_task(store_stream_episode(username, message, reply))
    asyncio.create_task(refresh_stream_state(message[:200], "пустота"))
    asyncio.create_task(run_pipeline(message, reply, username, 0))
    asyncio.create_task(maybe_consolidate(40))

    return reply

# ─── Голосование ─────────────────────────────────────────────────

async def start_vote(question: str, opt1: str = "Да", opt2: str = "Нет", duration: int = 30):
    global _game
    _game = {"type": "vote", "question": question, "opt1": opt1, "opt2": opt2,
             "votes": {1: 0, 2: 0}, "voters": set(), "end": time.time() + duration}
    announce = f"Голосование: {question}  !1 = {opt1}  |  !2 = {opt2}  ({duration}с)"
    _write_overlay(announce)
    reply = await _llm_game(f"Объяви голосование в чате: «{question}». Варианты: {opt1} или {opt2}. По-своему, коротко.")
    set_subtitle(reply)
    await asyncio.to_thread(speak, reply)
    await asyncio.sleep(duration)
    await finish_vote()

async def finish_vote():
    global _game
    if not _game or _game["type"] != "vote":
        return
    v1, v2 = _game["votes"][1], _game["votes"][2]
    total = v1 + v2 or 1
    winner = _game["opt1"] if v1 >= v2 else _game["opt2"]
    result_line = f"{_game['opt1']}: {v1} ({v1*100//total}%)  |  {_game['opt2']}: {v2} ({v2*100//total}%)"
    _write_overlay(f"Итог: {result_line}")
    reply = await _llm_game(f"Голосование завершено. {result_line}. Победил: {winner}. Прокомментируй коротко, в своём стиле.")
    set_subtitle(reply)
    await asyncio.to_thread(speak, reply)
    await asyncio.sleep(5)
    _clear_overlay()
    _game = None

# ─── Викторина ────────────────────────────────────────────────────

async def start_quiz(topic: str = ""):
    global _game
    # LLM генерирует вопрос + ответ
    import httpx, json as _json
    prompt = (f"Придумай вопрос для викторины" + (f" на тему: {topic}" if topic else "") +
              ". Верни JSON: {\"question\": str, \"answer\": str, \"hint\": str}. ТОЛЬКО JSON.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(LLM_URL, headers={"Authorization": f"Bearer {LIGHTNING_KEY}"},
                json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 150})
            raw = r.json()["choices"][0]["message"]["content"]
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = _json.loads(raw[s:e])
    except Exception as ex:
        log.error(f"[quiz] генерация вопроса: {ex}"); return

    _game = {"type": "quiz", "question": data["question"], "answer": data["answer"].lower(),
             "hint": data.get("hint", ""), "end": time.time() + 60, "answered": False}
    _write_overlay(f"Викторина: {data['question']}")
    reply = await _llm_game(f"Задай зрителям вопрос викторины: «{data['question']}». У них 60 секунд.")
    set_subtitle(reply)
    await asyncio.to_thread(speak, reply)
    # Таймер
    await asyncio.sleep(45)
    if _game and not _game["answered"]:
        hint_reply = await _llm_game(f"Подсказка для викторины: {_game['hint']}. Скажи подсказку.")
        set_subtitle(hint_reply)
        await asyncio.to_thread(speak, hint_reply)
        await asyncio.sleep(15)
    if _game and not _game["answered"]:
        await finish_quiz(winner=None)

async def finish_quiz(winner: str | None):
    global _game
    if not _game or _game["type"] != "quiz":
        return
    answer = _game["answer"]
    _game["answered"] = True
    if winner:
        _write_overlay(f"Правильно! {winner} угадал: {answer}")
        reply = await _llm_game(f"{winner} правильно ответил: {answer}. Похвали или прокомментируй по-своему.")
    else:
        _write_overlay(f"Никто не угадал. Ответ: {answer}")
        reply = await _llm_game(f"Никто не угадал. Правильный ответ: {answer}. Прокомментируй.")
    set_subtitle(reply)
    await asyncio.to_thread(speak, reply)
    await asyncio.sleep(4)
    _clear_overlay()
    _game = None

# ─── Акинатор ─────────────────────────────────────────────────────

async def start_akin(starter: str):
    global _game
    _game = {"type": "akin", "history": [], "q_count": 0, "starter": starter}
    reply = await _llm_game(
        "Зритель загадал персонажа. Ты играешь в Акинатора — угадываешь кто это. "
        "Задай первый вопрос (да/нет). Коротко, по-своему."
    )
    _write_overlay(f"Акинатор  Вопрос 1  |  !да  !нет  !не знаю")
    set_subtitle(reply)
    await asyncio.to_thread(speak, reply)
    _game["history"].append({"role": "assistant", "content": reply})

async def akin_answer(username: str, answer: str):
    global _game
    if not _game or _game["type"] != "akin":
        return
    _game["history"].append({"role": "user", "content": f"Чат отвечает: {answer}"})
    _game["q_count"] += 1
    n = _game["q_count"]

    if n >= 15:
        # Финальная попытка угадать
        prompt = ("На основе всех ответов — назови персонажа которого загадали. "
                  "Скажи уверенно или с сомнением — как чувствуешь.")
        reply = await _llm_game(prompt, history=_game["history"])
        _write_overlay("Акинатор — финальный ответ!")
        set_subtitle(reply)
        await asyncio.to_thread(speak, reply)
        await asyncio.sleep(3)
        _clear_overlay()
        _game = None
        return

    # Следующий вопрос или угадывание
    if n >= 8 and n % 3 == 0:
        prompt = ("Можешь попробовать угадать уже сейчас, или задай ещё один уточняющий вопрос. "
                  "Реши сам.")
    else:
        prompt = f"Задай следующий вопрос (вопрос {n+1}). Коротко."

    reply = await _llm_game(prompt, history=_game["history"])
    _write_overlay(f"Акинатор  Вопрос {n+1}  |  !да  !нет  !не знаю")
    set_subtitle(reply)
    await asyncio.to_thread(speak, reply)
    _game["history"].append({"role": "assistant", "content": reply})

# ─── Авто-запуск игр Мизурью ──────────────────────────────────────

async def maybe_start_game():
    """Мизури сама решает запустить игру когда чат активен."""
    global _game
    if _game:
        return
    if _audience_state["energy"] != "высокая":
        return
    if random.random() > 0.3:
        return
    choice = random.choice(["vote", "quiz", "akin"])
    if choice == "vote":
        prompt = "Придумай интересный вопрос для голосования зрителей (да/нет). Только вопрос, без лишнего."
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(LLM_URL, headers={"Authorization": f"Bearer {LIGHTNING_KEY}"},
                    json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 50})
                q = re.sub(r'<[^>]+>', '', r.json()["choices"][0]["message"]["content"]).strip()
            asyncio.create_task(start_vote(q))
        except Exception: pass
    elif choice == "quiz":
        asyncio.create_task(start_quiz())
    else:
        asyncio.create_task(start_akin("чат"))

# ─── Вспомогательный LLM для игр ─────────────────────────────────

async def _llm_game(prompt: str, history: list = None) -> str:
    import httpx
    system = SYSTEM_PROMPT + "\nСейчас ты ведёшь игру со зрителями. Говори коротко, живо, в своём стиле."
    msgs = [{"role": "system", "content": system}]
    if history:
        msgs += history[-10:]
    msgs.append({"role": "user", "content": prompt})
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(LLM_URL, headers={"Authorization": f"Bearer {LIGHTNING_KEY}"},
                json={"model": LLM_MODEL, "messages": msgs, "max_tokens": 150})
            reply = r.json()["choices"][0]["message"]["content"].strip()
            return re.sub(r'<[^>]+>', '', reply).strip()
    except Exception as e:
        log.error(f"[game llm] {e}")
        return ""

# ─── Обработка игровых команд из чата ────────────────────────────

async def handle_game_input(username: str, text: str, loop) -> bool:
    """Возвращает True если сообщение обработано игрой."""
    global _game
    tl = text.lower().strip()

    # Команды владельца
    if username.lower() == OWNER:
        m = re.match(r'!title\s+(.+)', tl)
        if m:
            global STREAM_TITLE
            STREAM_TITLE = m.group(1).strip()
            ok = await set_stream_title(STREAM_TITLE)
            log.info(f"[title] {'OK' if ok else 'FAIL'}: {STREAM_TITLE}")
            return True
        m = re.match(r'!vote\s+(.+)', tl)
        if m and not _game:
            asyncio.run_coroutine_threadsafe(start_vote(m.group(1).strip()), loop)
            return True
        m = re.match(r'!quiz(?:\s+(.+))?', tl)
        if m and not _game:
            asyncio.run_coroutine_threadsafe(start_quiz(m.group(1) or ""), loop)
            return True

    # Акинатор — любой зритель
    if tl == "!акинатор" and not _game:
        asyncio.run_coroutine_threadsafe(start_akin(username), loop)
        return True

    if not _game:
        return False

    # Голосование
    if _game["type"] == "vote":
        if tl == "!1" and username not in _game["voters"]:
            _game["votes"][1] += 1; _game["voters"].add(username)
            v1, v2 = _game["votes"][1], _game["votes"][2]
            _write_overlay(f"{_game['question']}\n!1 {_game['opt1']}: {v1}  |  !2 {_game['opt2']}: {v2}")
            return True
        if tl == "!2" and username not in _game["voters"]:
            _game["votes"][2] += 1; _game["voters"].add(username)
            v1, v2 = _game["votes"][1], _game["votes"][2]
            _write_overlay(f"{_game['question']}\n!1 {_game['opt1']}: {v1}  |  !2 {_game['opt2']}: {v2}")
            return True

    # Викторина
    if _game["type"] == "quiz" and not _game["answered"]:
        if any(ans in tl for ans in _game["answer"].split()):
            asyncio.run_coroutine_threadsafe(finish_quiz(username), loop)
            return True

    # Акинатор
    if _game["type"] == "akin":
        if tl in ("!да", "!нет", "!не знаю"):
            ans = {"!да": "Да", "!нет": "Нет", "!не знаю": "Не знаю"}[tl]
            asyncio.run_coroutine_threadsafe(akin_answer(username, ans), loop)
            return True

    return False

async def handle_message(username, message):
    if _msg_queue:
        await _msg_queue.put((username, message))
        log.info(f"[queue] добавлено от {username}, размер очереди: {_msg_queue.qsize()}")

async def _queue_worker():
    """Обрабатывает очередь сообщений по одному."""
    while True:
        username, message = await _msg_queue.get()
        t_total = time.time()
        try:
            log.info(f"[handle] === {username}: '{message[:60]}' ===")
            set_thinking("думает...")
            reply = await get_reply(username, message)
            if not reply:
                set_thinking(""); continue
            set_thinking("")
            set_subtitle(reply)
            await asyncio.to_thread(speak, reply)
            log.info(f"[handle] завершено за {time.time()-t_total:.2f}s")
        except Exception as e:
            log.error(f"[handle] ошибка: {e}", exc_info=True)
            set_thinking(""); set_subtitle("")
        finally:
            _msg_queue.task_done()

def irc_loop(loop):
    log.info(f"[IRC] подключаюсь к #{TWITCH_CHANNEL}")
    while True:
        try:
            s = socket.socket()
            s.connect(("irc.chat.twitch.tv", 6667))
            s.send(b"NICK justinfan88888\r\n")
            s.send(b"USER justinfan88888 0 * :justinfan\r\n")
            s.send(f"JOIN #{TWITCH_CHANNEL}\r\n".encode())
            log.info(f"[IRC] подключён к #{TWITCH_CHANNEL}")
            buf = ""
            while True:
                buf += s.recv(2048).decode("utf-8", errors="ignore")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if line.startswith("PING"):
                        s.send(b"PONG :tmi.twitch.tv\r\n"); continue
                    m = re.match(r":(\w+)!\w+@\S+ PRIVMSG #\S+ :(.+)", line)
                    if not m: continue
                    user, text = m.group(1), m.group(2).strip()
                    log.info(f"[IRC] чат: {user}: {text}")
                    _chat_buffer.append(f"{user}: {text}")
                    if len(_chat_buffer) > 50:
                        _chat_buffer.pop(0)
                    # Детект всплеска — если 3+ подряд сообщений содержат spike-слова
                    recent3 = " ".join(_chat_buffer[-3:]).lower()
                    is_spike = sum(1 for w in TOM_SPIKE_WORDS if w in recent3) >= 2
                    if is_spike:
                        asyncio.run_coroutine_threadsafe(update_audience_tom(force=True), loop)
                    threading.Thread(target=lambda u=user, t=text: (
                        post_json(SUBTITLE_URL, {"text": f"{u}: {t}"}),
                        time.sleep(3),
                        post_json(SUBTITLE_URL, {"text": ""})
                    ), daemon=True).start()
                    is_direct = bool(re.search(r'@?(?:mizu(?:ri)?|мизури?|@m\b|@м\b)', text, re.IGNORECASE))
                    # Сначала проверяем игровые команды
                    if asyncio.run_coroutine_threadsafe(handle_game_input(user, text, loop), loop).result():
                        continue
                    if not is_direct and random.random() > 0.40:
                        continue
                    clean = re.sub(r'@?(?:mizu(?:ri)?|мизури?|@m\b|@м\b)', '', text, flags=re.IGNORECASE).strip() or text
                    # Добавляем контекст последних 10 сообщений чата
                    chat_ctx = "\n".join(_chat_buffer[-11:-1]) if len(_chat_buffer) > 1 else ""
                    if chat_ctx:
                        clean = f"[контекст чата]:\n{chat_ctx}\n\n{user}: {clean}"
                    log.info(f"[IRC] {'@mizu' if is_direct else 'random(40%)'} от {user}: '{text}'")
                    asyncio.run_coroutine_threadsafe(handle_message(user, clean), loop)
        except Exception as e:
            log.error(f"[IRC] ошибка: {e}, переподключение через 5s")
            time.sleep(5)

def http_chat_server(loop):
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json as _json
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(b'{"ok":true}')
            msg = body.get('message','').strip()
            user = body.get('user', 'viewer')
            if msg:
                log.info(f"[HTTP] сообщение от {user}: '{msg}'")
                asyncio.run_coroutine_threadsafe(handle_message(user, msg), loop)
    log.info("[HTTP] чат сервер запущен на порту 5001")
    HTTPServer(("0.0.0.0", 5001), H).serve_forever()

async def main():
    global _msg_queue
    log.info("=" * 50)
    log.info("[stream] Мизури запускается")
    log.info(f"[stream] канал: #{TWITCH_CHANNEL}")
    log.info(f"[stream] LLM: {LLM_MODEL}")
    log.info(f"[stream] TTS: {FISH_URL}")
    log.info(f"[stream] аудио очередь: {PCM_QUEUE}")
    log.info("=" * 50)
    memory_init()
    _msg_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    for f in ['/tmp/game_overlay.txt']:
        if not os.path.exists(f): open(f, 'w').close()
    threading.Thread(target=irc_loop, args=(loop,), daemon=True).start()
    threading.Thread(target=http_chat_server, args=(loop,), daemon=True).start()
    asyncio.create_task(_queue_worker())

    async def decay_loop():
        while True:
            await asyncio.sleep(1800)
            # Инсула восстанавливается в тишине (0.5 часа = 30 мин)
            _insula.passive_recovery_hours(0.5)

    asyncio.create_task(decay_loop())
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
