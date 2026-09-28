import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import db, limits, llm, stats
from bot.config import ADMIN_CHAT_ID, NO_ACCESS_TEXT
from bot.keyboards import main_menu_keyboard
from bot.sheets import sheets_logger

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "Привет! Я могу разобрать вопрос по отношениям или по зубам. "
    "Ещё могу рассказать о концепции психостоматологии."
)

BRANCH_HINT = {
    "relationships": "Похоже, ты про отношения. Нажми кнопку ниже, чтобы начать разбор.",
    "teeth": "Похоже, ты про зубы. Нажми кнопку ниже, чтобы начать.",
    "concept": "Похоже, ты про концепцию психостоматологии. Нажми кнопку ниже.",
}


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = WELCOME_TEXT) -> None:
    user = update.effective_user
    db.upsert_user(user.id, user.username)

    if not await limits.has_access(context.bot, user.id):
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(NO_ACCESS_TEXT)
        else:
            await update.effective_message.reply_text(NO_ACCESS_TEXT)
        return

    keyboard = await main_menu_keyboard(context.bot, user.id)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await show_menu(update, context)


async def back_to_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from telegram.ext import ConversationHandler

    context.user_data.clear()
    await show_menu(update, context)
    return ConversationHandler.END


async def limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/limits <user_id> — только для админа (ADMIN_CHAT_ID): сколько у человека
    осталось по всем трём веткам, с учётом персональных переопределений."""
    caller_id = update.effective_user.id
    if not ADMIN_CHAT_ID or str(caller_id) != str(ADMIN_CHAT_ID):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Использование: /limits <user_id>\n"
            "user_id можно взять из колонки user_id в любом листе таблицы логов."
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id должен быть числом.")
        return

    tier = await limits.get_tier(context.bot, target_id)
    data = await limits.summary(context.bot, target_id)
    lines = [f"Лимиты для user_id {target_id} (тариф: {tier}):"]
    for info in data.values():
        left = "∞" if info["left"] == float("inf") else info["left"]
        lim = "∞" if info["limit"] == float("inf") else info["limit"]
        lines.append(f"{info['label']}: осталось {left} из {lim} (использовано {info['used']})")
    lines.append(
        "\nЧтобы поменять лимит вручную — впиши строку в лист «Лимиты (ручные)» таблицы логов: "
        "user_id | ветка (Отношения/Зубы/Концепция) | лимит (число или «безлимит»). Подхватится в "
        "течение 5 минут. Чтобы дать тариф «родственник» — впиши user_id в лист «Родственники»."
    )
    await update.effective_message.reply_text("\n".join(lines))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — сводка по реальным (не тестовым) данным. Только для ADMIN_CHAT_ID."""
    caller_id = update.effective_user.id
    if not ADMIN_CHAT_ID or str(caller_id) != str(ADMIN_CHAT_ID):
        return
    text = await stats.compute_summary()
    await update.effective_message.reply_text(text)


async def weekly_stats_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Автосводка раз в неделю (понедельник 10:00 по Бангкоку), см. bot/main.py."""
    if not ADMIN_CHAT_ID:
        return
    text = await stats.compute_summary()
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"📊 Еженедельная сводка\n\n{text}")
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить еженедельную сводку админу")


async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/send <user_id> <текст> — только для ADMIN_CHAT_ID: отправляет сообщение указанному
    пользователю от имени бота и логирует отправку. Текст берём из сырого текста сообщения
    (не context.args), чтобы сохранить переносы строк на многоабзацных сообщениях."""
    caller_id = update.effective_user.id
    if not ADMIN_CHAT_ID or str(caller_id) != str(ADMIN_CHAT_ID):
        return

    raw = update.effective_message.text or ""
    parts = raw.split(maxsplit=2)
    if len(parts) < 3:
        await update.effective_message.reply_text("Использование: /send <user_id> <текст>")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await update.effective_message.reply_text("user_id должен быть числом.")
        return

    text = parts[2]
    try:
        await context.bot.send_message(chat_id=target_id, text=text)
        await sheets_logger.append("Админ-сообщения", [db.now(), target_id, text, "отправлено"])
        await update.effective_message.reply_text("Отправлено.")
    except TelegramError as e:
        await sheets_logger.append("Админ-сообщения", [db.now(), target_id, text, f"ошибка: {e}"])
        await update.effective_message.reply_text(f"Не удалось отправить (пользователь мог заблокировать бота): {e}")


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chatid — показывает id текущего чата. Нужен один раз, чтобы узнать id приватного
    «чата исцеления отношений» и вписать его в RELATIONSHIPS_CHAT_ID в .env (бот должен
    быть добавлен в чат участником)."""
    chat = update.effective_chat
    await update.effective_message.reply_text(f"chat_id этого чата: {chat.id}")


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Свободный текст вне веток: /start-приветствие или намёк на ветку."""
    text = update.effective_message.text or ""
    intent = await llm.classify_menu_intent(text)
    if intent == "greeting":
        await show_menu(update, context)
    else:
        await show_menu(update, context, text=BRANCH_HINT[intent])
