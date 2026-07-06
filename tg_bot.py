"""
Telegram-бот для тестирования логики памяти Мизури.
Команды:
  /memory   — показать L1 атомы для текущего пользователя
  /persona  — показать L3 персону
  /state    — внутреннее состояние (insula)
  /beliefs  — убеждения
  /emotion  — эмоциональное состояние (personality)
  /consolidate — принудительная консолидация L2+L3
  /reset    — сбросить память текущего пользователя
  /help     — список команд
"""
import asyncio, logging, sys, os
sys.path.insert(0, os.path.dirname(__file__))

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

from stream_memory import (
    init as memory_init,
    retrieve_for_stream,
    store_stream_episode,
    get_stream_state_block,
    refresh_stream_state,
    get_beliefs_block,
    viewer_model_to_block,
    maybe_consolidate,
    _session_key,
)
from memory import l1, insula
from personality import get_state
from ai import ask_claude

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tg_bot")

TOKEN = "8862338011:AAFmZ6SHYiUbJLyj-94PBniA1bxMa1inSuc"
# Прокси для обхода блокировки Telegram (финский VPS)
# Формат: "socks5://user:pass@host:port" или "http://host:port" или None
PROXY = None  # заполни если нужно

SYSTEM_PROMPT = """Когда я родилась меня назвали Мизури. Мне сейчас 14 лет. У меня есть отец и мать, Куруми и Мизуки."""

# История диалога на пользователя
_histories: dict[int, list[dict]] = {}


def _username(update: Update) -> str:
    return update.effective_user.username or str(update.effective_user.id)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/memory — L1 атомы\n"
        "/persona — L3 персона\n"
        "/state — внутреннее состояние\n"
        "/beliefs — убеждения\n"
        "/emotion — эмоциональное состояние\n"
        "/consolidate — принудительная консолидация L2+L3\n"
        "/reset — сбросить память\n"
        "/help — эта справка\n\n"
        "Просто пиши — Мизури ответит и запомнит."
    )


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = _username(update)
    sk = _session_key(username)
    atoms = l1.get_by_session(sk, limit=20)
    if not atoms:
        await update.message.reply_text("Память пуста.")
        return
    lines = [f"[{a['type']}|p={a['priority']}] {a['content']}" for a in atoms]
    text = f"L1 атомы ({len(atoms)}):\n\n" + "\n".join(lines)
    # Telegram limit 4096
    await update.message.reply_text(text[:4000])


async def cmd_persona(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = _username(update)
    block = viewer_model_to_block(username)
    await update.message.reply_text(block[:4000] if block else "Персона ещё не сформирована.")


async def cmd_state(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    block = get_stream_state_block()
    await update.message.reply_text(block)


async def cmd_beliefs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    block = get_beliefs_block()
    await update.message.reply_text(block if block else "Убеждений нет.")


async def cmd_emotion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = get_state()
    await update.message.reply_text(state.to_prompt_string())


async def cmd_consolidate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = _username(update)
    await update.message.reply_text("Запускаю консолидацию...")
    from stream_memory import _session_key as sk_fn
    from memory import l2, l3
    sk = sk_fn(username)
    atoms = l1.get_by_session(sk, limit=50)
    if not atoms:
        await update.message.reply_text("Нет атомов для консолидации.")
        return
    changed = await l2.run_l2(sk, atoms)
    if changed > 0:
        updated = await l3.run_l3(sk)
        await update.message.reply_text(f"L2: {changed} сцен изменено. L3: {'обновлена' if updated else 'без изменений'}.")
    else:
        await update.message.reply_text("L2: изменений нет.")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = _username(update)
    sk = _session_key(username)
    from memory.db import _conn
    c = _conn()
    c.execute("DELETE FROM l0_conversations WHERE session_key=?", (sk,))
    c.execute("DELETE FROM l1_records WHERE session_key=?", (sk,))
    c.execute("DELETE FROM l1_vec_store WHERE record_id LIKE ?", (f"l1_{sk}%",))
    c.execute("DELETE FROM l2_scene_index WHERE session_key=?", (sk,))
    c.execute("DELETE FROM l3_persona_meta WHERE session_key=?", (sk,))
    c.commit(); c.close()
    # Удаляем файлы сцен и персоны
    from pathlib import Path
    import shutil
    root = Path(__file__).parent
    for d in [root / "data" / "scene_blocks" / sk, root / "data" / "personas" / sk]:
        if d.exists():
            shutil.rmtree(d)
    _histories.pop(update.effective_user.id, None)
    # Сбрасываем in-memory vec index для этого пользователя
    from memory import l1 as l1m
    l1m._vec_index.clear()
    l1m._index_loaded = False
    await update.message.reply_text("Память сброшена.")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = _username(update)
    text = update.message.text.strip()
    if not text:
        return

    await update.message.chat.send_action("typing")

    # Получаем память
    mem_block = await retrieve_for_stream(text, username, top_k=5)
    state_block = get_stream_state_block()
    beliefs_block = get_beliefs_block()
    emotion = get_state()

    system = (
        SYSTEM_PROMPT + "\n\n"
        + emotion.to_prompt_string() + "\n\n"
        + state_block + "\n\n"
        + (beliefs_block + "\n\n" if beliefs_block else "")
        + (mem_block if mem_block else "")
    )

    history = _histories.setdefault(user_id, [])
    reply = await ask_claude(system, history, text, max_tokens=300)

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    if len(history) > 30:
        _histories[user_id] = history[-30:]

    await update.message.reply_text(reply)

    # Обновляем эмоции
    await emotion.update(text)
    emotion.save()

    # Сохраняем в память
    await store_stream_episode(username, text, reply, salience=0.5)
    await refresh_stream_state(text, "нейтральное")
    await maybe_consolidate(username, threshold=40)


def main():
    memory_init()
    builder = ApplicationBuilder().token(TOKEN)
    if PROXY:
        from telegram.request import HTTPXRequest
        builder.request(HTTPXRequest(proxy=PROXY))
    app = builder.build()
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("persona", cmd_persona))
    app.add_handler(CommandHandler("state", cmd_state))
    app.add_handler(CommandHandler("beliefs", cmd_beliefs))
    app.add_handler(CommandHandler("emotion", cmd_emotion))
    app.add_handler(CommandHandler("consolidate", cmd_consolidate))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
