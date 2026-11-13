"""ТЗ-доп. №8: «не знаю → уточню у автора».

Бот не придумывает ответ, которого нет в материалах, а предлагает: «Это я не знаю, это надо
спрашивать. Хочешь, я уточню?». На «Да, уточни» вопрос с контекстом уходит автору (AUTHOR_CHAT_ID),
автор отвечает реплаем — ответ уходит пользователю с подписью «Ответ автора:», пишется в листы
«Вопросы автору» и «Ответы автора» и пополняет базу ответов автора (author_answers.all_entries).

Обработчики регистрируются в группе -1 (раньше веток): кнопки работают в любом состоянии
разговора, а реплай автора не съедает его собственный активный разбор."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from bot import db
from bot.config import AUTHOR_CHAT_ID
from bot.sheets import AUTHOR_ANSWERS_TAB, AUTHOR_QUESTIONS_TAB, sheets_logger

logger = logging.getLogger(__name__)

UNKNOWN_TEXT = "Это я не знаю, это надо спрашивать. Хочешь, я уточню?"
SENT_TEXT = "Передал вопрос автору. Как ответит — пришлю сюда."
DECLINED_TEXT = "Хорошо. Если захочешь — спроси по-другому, отвечу по материалам."
STALE_TEXT = "Этот вопрос уже не актуален — задай его ещё раз, если нужно."
ANSWER_PREFIX = "Ответ автора:\n\n"
PENDING_KEY = "author_q_pending"


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, уточни", callback_data="ask_author:yes"),
        InlineKeyboardButton("Нет", callback_data="ask_author:no"),
    ]])


def remember(context: ContextTypes.DEFAULT_TYPE, question: str, history: str, branch: str = "Концепция") -> None:
    """Запомнить вопрос до нажатия кнопки — показ самого UNKNOWN_TEXT делает ветка своим _send."""
    context.user_data[PENDING_KEY] = {"question": question, "context": history, "branch": branch}


async def choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    pending = context.user_data.pop(PENDING_KEY, None)
    if query.data != "ask_author:yes":
        await query.message.reply_text(DECLINED_TEXT)
        raise ApplicationHandlerStop
    if not pending or not AUTHOR_CHAT_ID:
        await query.message.reply_text(STALE_TEXT)
        raise ApplicationHandlerStop

    user = update.effective_user
    question_id, asked_at = db.create_author_question(
        user.id, user.username, pending["branch"], pending["question"], pending["context"],
    )
    relay = (
        f"❓ Вопрос от @{user.username or '—'} ({user.id}), ветка «{pending['branch']}»\n"
        f"Вопрос: {pending['question']}\n"
        f"Контекст:\n{pending['context'] or '—'}\n\n"
        "Ответь реплаем на это сообщение — ответ уйдёт пользователю."
    )
    try:
        sent = await context.bot.send_message(chat_id=AUTHOR_CHAT_ID, text=relay[:4000])
        db.set_author_question_msg(question_id, sent.message_id)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить вопрос автору")
    await sheets_logger.append(
        AUTHOR_QUESTIONS_TAB,
        [asked_at, user.id, user.username or "", pending["branch"], pending["question"], pending["context"], "", "", ""],
    )
    await query.message.reply_text(SENT_TEXT)
    raise ApplicationHandlerStop


async def author_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    row = db.get_author_question_by_msg(message.reply_to_message.message_id)
    if row is None:
        return  # реплай не на вопрос — пусть обработают ветки
    answer = message.text or ""
    delivered = True
    try:
        await context.bot.send_message(chat_id=row["user_id"], text=ANSWER_PREFIX + answer)
    except (Forbidden, BadRequest) as e:
        delivered = False
        logger.warning("Ответ автора не доставлен user_id=%s: %s", row["user_id"], e)
    answered_at = db.answer_author_question(row["id"], answer, delivered)
    await sheets_logger.update_author_question(row["asked_at"], row["user_id"], answered_at, answer, delivered)
    await sheets_logger.append(
        AUTHOR_ANSWERS_TAB,
        [answered_at, row["user_id"], row["username"] or "", "из вопросов пользователей", row["question"], answer],
    )
    if delivered:
        await message.reply_text("✅ Доставлено пользователю.")
    else:
        await message.reply_text(
            "⚠️ Не доставлено: пользователь заблокировал бота или удалил чат. Ответ сохранён в «Ответы автора»."
        )
    raise ApplicationHandlerStop


def handlers() -> list:
    out = [CallbackQueryHandler(choice, pattern="^ask_author:")]
    if AUTHOR_CHAT_ID:
        out.append(MessageHandler(
            filters.Chat(chat_id=int(AUTHOR_CHAT_ID)) & filters.REPLY & filters.TEXT & ~filters.COMMAND,
            author_reply,
        ))
    return out
