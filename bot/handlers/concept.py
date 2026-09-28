import logging
import random

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import db, debounce, hostility, limits, llm
from bot.author_answers import ENTRIES as AUTHOR_ANSWERS
from bot.config import ADMIN_CHAT_ID, ADMIN_USERNAME, CONCEPT_CHAT_USERNAME, limit_exhausted_text
from bot.keyboards import back_to_menu_keyboard
from bot.sheets import concept_store, sheets_logger

logger = logging.getLogger(__name__)

ASKING, CLARIFY = range(2)

INTRO_TEXT = "Спрашивай что угодно о концепции психостоматологии — отвечу по нашим материалам."
LIMIT_TEXT = limit_exhausted_text("Концепция")
NOT_CONVERGED_TEXT = (
    "Кажется, мы с тобой смотрим на это по-разному, и словами тут вряд ли договоримся. "
    f"Если хочешь обсудить дальше — пиши {ADMIN_USERNAME}."
)
OFFTOPIC_TEXT = (
    f"Это не про концепцию психостоматологии — такое лучше обсудить в чате психостоматологии, "
    f"{CONCEPT_CHAT_USERNAME}. Чтобы получить туда доступ, напиши {ADMIN_USERNAME} — добавят."
)
DISPUTE_STREAK_THRESHOLD = 2


async def _log_turn(context: ContextTypes.DEFAULT_TYPE, user, step: str, who: str, text: str, msg_type: str = "обычный") -> None:
    """ТЗ-доп. №6, ч.1 — линейный лог ветки «Концепция» (без матрицы: шаги не фиксированные).
    session_id — на время одного цикла вопрос→(уточнения)→ответ, живёт в user_data."""
    session_id = context.user_data.get("concept_session_id", "")
    await sheets_logger.append(
        "Диалоги",
        [db.now(), user.id, session_id, user.username or "", "concept", "", step, who, (text or "")[:2000], msg_type],
    )


async def _send(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str, msg_type: str = "обычный", **kwargs):
    target = update.callback_query.message if update.callback_query else update.effective_message
    result = await target.reply_text(text, **kwargs)
    await _log_turn(context, update.effective_user, step, "бот", text, msg_type)
    return result

MAX_CLARIFY_ROUNDS = 3
CLARIFY_OPENERS = [
    "Так, давай разберёмся: ",
    "Так-так-так, давай ещё раз, попытаюсь тебя понять: ",
    "Давай я тебя ещё раз помучаю, потому что я не просто бот — я тупой бот, поэтому прости меня "
    "за это, ещё раз спрошу: ",
]

RANT_OPENERS = [
    "Ого, походу, разметелило тебя там 😄 Слышу, без драмы.",
    "Чувствую жар в вопросе 🔥 Окей, без паники, разберёмся.",
    "Ага, накипело — понимаю. Погнали спокойно, по порядку.",
]


async def _check_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, user, via_query: bool) -> bool:
    """True, если лимит исчерпан и ответ уже отправлен."""
    if db.get_concept_message_count(user.id) >= await limits.get_limit(context.bot, user.id, "concept"):
        db.log_limit_hit(user.id, "concept")
        await sheets_logger.append(
            "Лимиты", [db.now(), user.id, user.username or "", "Концепция", "упёрся в лимит"]
        )
        send = update.callback_query.edit_message_text if via_query else update.effective_message.reply_text
        await send(LIMIT_TEXT, reply_markup=back_to_menu_keyboard())
        return True
    return False


async def _maybe_rant_opener(update: Update, text: str) -> None:
    if await llm.detect_rant(text):
        await update.effective_message.reply_text(random.choice(RANT_OPENERS))


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    db.upsert_user(user.id, user.username)

    if await _check_limit(update, context, user, via_query=True):
        return ConversationHandler.END

    context.user_data["concept_dispute_streak"] = 0
    context.user_data.pop("concept_last_topic", None)
    context.user_data.pop("concept_session_id", None)
    hostility.reset_session(context)
    await query.edit_message_text(INTRO_TEXT, reply_markup=back_to_menu_keyboard())
    await _log_turn(context, user, "intro", "бот", INTRO_TEXT)
    return ASKING


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Первое сообщение раунда: сырой вопрос пользователя. Не отвечаем сразу —
    сперва уточняем, что человек на самом деле хочет узнать (буквально и подспудно)."""
    return await debounce.collect(
        update, context, step_id="concept_ask", conv_handler=conv_handler, state=ASKING,
        process=lambda text: _process_ask(update, context, text),
    )


async def _process_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> int:
    user = update.effective_user

    if await _check_limit(update, context, user, via_query=False):
        return ConversationHandler.END

    status = await hostility.precheck(
        update, context, branch="concept", step="ask", text_override=question
    )
    if status == "crisis":
        return ConversationHandler.END
    if status == "closed":
        db.increment_concept_messages(user.id)
        await hostility.maybe_send_self_harm_note(update, context)
        return ConversationHandler.END
    if status == "hostile":
        return ASKING

    db.increment_concept_messages(user.id)

    # session_id разбора — на весь цикл вопрос→(уточнения)→ответ (ТЗ-доп. №6, ч.1)
    context.user_data["concept_session_id"] = f"{user.id}_{db.now()}"
    await _log_turn(context, user, "ask", "человек", question)

    prior_topic = context.user_data.get("concept_last_topic")
    if await llm.is_offtopic_concept(question, prior_topic=prior_topic):
        await _send(update, context, "offtopic", OFFTOPIC_TEXT, msg_type="отказ", reply_markup=back_to_menu_keyboard())
        context.user_data.pop("concept_session_id", None)
        return ASKING

    await _maybe_rant_opener(update, question)

    context.user_data["concept_raw_question"] = question
    context.user_data["concept_clarify_round"] = 1
    clarified = await llm.clarify_question(question)
    context.user_data["concept_pending_clarified"] = clarified
    await _send(update, context, "clarify", CLARIFY_OPENERS[0] + clarified, msg_type="уточнение")
    return CLARIFY


async def clarify_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="concept_clarify", conv_handler=conv_handler, state=CLARIFY,
        process=lambda text: _process_clarify_reply(update, context, text),
    )


async def _process_clarify_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    user = update.effective_user

    if await _check_limit(update, context, user, via_query=False):
        return ConversationHandler.END

    pending_question = context.user_data.get("concept_pending_clarified", "")
    status = await hostility.precheck(
        update, context, branch="concept", step="clarify",
        bot_question=pending_question, text_override=text,
    )
    if status == "crisis":
        return ConversationHandler.END
    if status == "closed":
        db.increment_concept_messages(user.id)
        await hostility.maybe_send_self_harm_note(update, context)
        return ConversationHandler.END
    if status == "hostile":
        return CLARIFY

    db.increment_concept_messages(user.id)

    await _log_turn(context, user, "clarify", "человек", text)
    await _maybe_rant_opener(update, text)

    raw_question = context.user_data.get("concept_raw_question", "")
    round_ = context.user_data.get("concept_clarify_round", 1)

    verdict = await llm.classify_confirmation(text) if round_ < MAX_CLARIFY_ROUNDS else "confirm"

    if verdict == "correct":
        round_ += 1
        context.user_data["concept_clarify_round"] = round_
        clarified = await llm.clarify_question(raw_question, correction=text)
        context.user_data["concept_pending_clarified"] = clarified
        await _send(update, context, "clarify", CLARIFY_OPENERS[round_ - 1] + clarified, msg_type="уточнение")
        return CLARIFY

    resolved_question = context.user_data.pop("concept_pending_clarified", raw_question)
    context.user_data.pop("concept_clarify_round", None)

    await concept_store.ensure_fresh()
    context_text = concept_store.main_narrative()
    relevant_title = await llm.pick_relevant_sheet(resolved_question, concept_store.titles())
    if relevant_title:
        context_text += "\n\n" + concept_store.get(relevant_title)

    dispute = await llm.is_dispute(raw_question)
    similar_nums = await llm.pick_similar_author_answers(resolved_question, AUTHOR_ANSWERS)
    author_examples = [e for e in AUTHOR_ANSWERS if e["num"] in similar_nums]
    answer = await llm.answer_concept_question(resolved_question, context_text, author_examples)

    streak = context.user_data.get("concept_dispute_streak", 0)
    streak = streak + 1 if dispute else 0
    context.user_data["concept_dispute_streak"] = streak

    escalated = streak >= DISPUTE_STREAK_THRESHOLD
    if escalated:
        answer = f"{answer}\n\n{NOT_CONVERGED_TEXT}"

    await _send(update, context, "answer", answer, reply_markup=back_to_menu_keyboard())
    context.user_data["concept_last_topic"] = resolved_question
    context.user_data.pop("concept_session_id", None)

    await sheets_logger.append(
        "Концепция",
        [db.now(), user.id, user.username or "", raw_question, answer,
         "да" if dispute else "нет", "да" if (escalated and ADMIN_CHAT_ID) else "нет",
         resolved_question],
    )

    if ADMIN_CHAT_ID:
        try:
            relay = (
                f"❓ {user.username or user.id}: {raw_question}\n"
                f"(уточнено: {resolved_question})\n\n💬 {answer}"
            )
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=relay[:4000])
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить релей вопроса/ответа админу")

    if escalated and ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"⚠️ Пользователь @{user.username or user.id} не сходится во взглядах в ветке "
                    f"«Концепция». Вопрос: {raw_question}"
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить уведомление об эскалации админу")
        context.user_data["concept_dispute_streak"] = 0

    context.user_data.pop("concept_raw_question", None)
    await hostility.maybe_send_self_harm_note(update, context)
    return ASKING


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot.handlers.menu import show_menu

    await show_menu(update, context)
    await hostility.maybe_send_self_harm_note(update, context)
    return ConversationHandler.END


conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(entry, pattern="^menu:concept$")],
    states={
        ASKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask)],
        CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, clarify_reply)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CommandHandler("start", cancel),
        CallbackQueryHandler(cancel, pattern="^menu:back$"),
    ],
    name="concept_conversation",
    persistent=True,
    allow_reentry=True,
)
