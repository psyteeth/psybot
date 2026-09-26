import logging

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
from bot.config import ADMIN_USERNAME, LIMIT_RELATIONSHIP_MESSAGES, RELATIONSHIP_LIMIT_TEXT
from bot.keyboards import back_to_menu_keyboard
from bot.sheets import sheets_logger

logger = logging.getLogger(__name__)

(
    A_EVENT, B_NARRATIVE, B_CONFIRM, C_CONSEQUENCE, C_FEELING, D_CONFIRM, D_QUESTION,
    E_SUMMARY, E_FOLLOWUP,
) = range(9)

Q_A = (
    "Опиши событие или поведение другого человека, от которого тебе дискомфортно: "
    "триггерит, бесит, раздражает, обламывает, достаёт."
)
Q_B = "Как бы ты хотел, чтобы было иначе? Что другой человек должен был сделать по-другому?"
Q_C_TEMPLATE = (
    "Когда {narrative_short}не совпадает с тем, что происходит на самом деле, что ты чувствуешь? "
    "Как ты реагируешь, когда веришь, что должно быть именно так, а так не происходит?"
)
CRISIS_TEXT = (
    "Похоже, тут речь о насилии, угрозах или опасности. Такую ситуацию лучше разбирать со "
    f"специалистом напрямую. Пиши {ADMIN_USERNAME}."
)
WRAP_TEXT = "Мы прошли уже много — давай подведём предварительный итог."
E_NO_INSIGHT_TEXT = (
    "Похоже, сейчас ответ не находится — и это тоже нормально, не обязательно сразу. Если захочется "
    f"разобрать это глубже — приходи на диагностику, пиши {ADMIN_USERNAME}."
)
DECLINE_D_TEXT = "Ок, как скажешь. Если захочешь вернуться — я здесь."

D_FIELDS = {
    1: "d1_logical", 2: "d2_empirical", 3: "d3_pragmatic", 4: "d4_hedonistic",
    5: "d5_catastrophe_scale", 6: "d6_historical", 7: "d7_double_standard", 8: "d8_semantic",
}


def _row_for_sheets(row) -> list:
    return [
        row["started_at"], row["user_id"], row["username"] or "", row["session_num"],
        row["a_event"], row["b_narrative_raw"], row["b_narrative_confirmed"], row["c_consequence"],
        row["d1_logical"], row["d2_empirical"], row["d3_pragmatic"], row["d4_hedonistic"],
        row["d5_catastrophe_scale"], row["d6_historical"], row["d7_double_standard"], row["d8_semantic"],
        row["e_summary"], row["exit_step"] or "", "да" if row["completed"] else "нет",
        row["message_count"], row["ended_at"] or "",
    ]


async def _log_session(session_id: int) -> None:
    row = db.get_relationship_session(session_id)
    await sheets_logger.append("Отношения", _row_for_sheets(row))


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    db.upsert_user(user.id, user.username)

    return await _start_session(update, context, via_query=True)


async def _start_session(update: Update, context: ContextTypes.DEFAULT_TYPE, via_query: bool) -> int:
    user = update.effective_user
    send = update.callback_query.edit_message_text if via_query else update.effective_message.reply_text

    if db.count_relationship_sessions(user.id) >= await limits.get_limit(user.id, "relationships"):
        db.log_limit_hit(user.id, "relationships")
        await sheets_logger.append(
            "Лимиты", [db.now(), user.id, user.username or "", "Отношения", "упёрся в лимит"]
        )
        await send(RELATIONSHIP_LIMIT_TEXT, reply_markup=back_to_menu_keyboard())
        return ConversationHandler.END

    session_id = db.create_relationship_session(user.id, user.username)
    context.user_data["rel_session_id"] = session_id
    hostility.reset_session(context)
    await send(Q_A)
    return A_EVENT


async def _close_after_hostility(session_id: int, exit_step: str) -> None:
    db.finish_relationship_session(session_id, exit_step=exit_step, completed=False)
    await _log_session(session_id)


async def _bump_messages(session_id: int) -> int:
    return db.increment_relationship_messages(session_id)


async def _ask_e_question(context: ContextTypes.DEFAULT_TYPE, session_id: int) -> str:
    row = db.get_relationship_session(session_id)
    question = await llm.generate_e_question(row["a_event"], row["b_narrative_confirmed"])
    context.user_data["rel_e_question"] = question
    return question


async def _ask_feeling_question(session_id: int) -> str:
    row = db.get_relationship_session(session_id)
    corpus = f"{row['a_event'] or ''} {row['b_narrative_raw'] or ''} {row['c_consequence'] or ''}"
    gender = await llm.detect_gender_hint(corpus)
    if gender == "masc":
        return "Каким ты себя чувствуешь в этот момент?"
    if gender == "fem":
        return "Какой ты себя чувствуешь в этот момент?"
    return "Как ты себя чувствуешь в этот момент?"


async def _maybe_wrap_to_summary(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, count: int
) -> bool:
    if count > LIMIT_RELATIONSHIP_MESSAGES:
        await update.effective_message.reply_text(WRAP_TEXT)
        question = await _ask_e_question(context, session_id)
        await update.effective_message.reply_text(question)
        return True
    return False


async def a_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_a", conv_handler=conv_handler, state=A_EVENT,
        process=lambda text: _process_a_event(update, context, text),
    )


async def _process_a_event(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="A", bot_question=Q_A, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return A_EVENT
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, a_event=text)

    if await llm.detect_crisis(text):
        db.finish_relationship_session(session_id, exit_step="crisis", completed=False)
        await _log_session(session_id)
        await update.effective_message.reply_text(CRISIS_TEXT, reply_markup=back_to_menu_keyboard())
        return ConversationHandler.END

    await update.effective_message.reply_text(Q_B)
    return B_NARRATIVE


async def b_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_b", conv_handler=conv_handler, state=B_NARRATIVE,
        process=lambda text: _process_b_narrative(update, context, text),
    )


async def _process_b_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="B", bot_question=Q_B, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return B_NARRATIVE
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, b_narrative_raw=text)
    context.user_data["rel_raw_narrative"] = text

    row = db.get_relationship_session(session_id)
    reformulated = await llm.reformulate_narrative(row["a_event"], text)
    context.user_data["rel_pending_narrative"] = reformulated
    await update.effective_message.reply_text(reformulated)
    return B_CONFIRM


async def b_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_b_confirm", conv_handler=conv_handler, state=B_CONFIRM,
        process=lambda text: _process_b_confirm(update, context, text),
    )


async def _process_b_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    pending_question = context.user_data.get("rel_pending_narrative", "")

    status = await hostility.precheck(
        update, context, branch="relationships", step="B_confirm",
        bot_question=pending_question, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return B_CONFIRM
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    count = await _bump_messages(session_id)

    verdict = await llm.classify_confirmation(text)
    if verdict == "correct":
        row = db.get_relationship_session(session_id)
        reformulated = await llm.reformulate_narrative(
            row["a_event"], context.user_data["rel_raw_narrative"], correction=text
        )
        context.user_data["rel_pending_narrative"] = reformulated
        await update.effective_message.reply_text(reformulated)
        return B_CONFIRM

    confirmed = context.user_data.pop("rel_pending_narrative")
    db.update_relationship_session(session_id, b_narrative_confirmed=confirmed)

    if await _maybe_wrap_to_summary(update, context, session_id, count):
        return E_SUMMARY

    await update.effective_message.reply_text(
        Q_C_TEMPLATE.format(narrative_short="то, как ты хочешь, ")
    )
    return C_CONSEQUENCE


async def c_consequence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_c", conv_handler=conv_handler, state=C_CONSEQUENCE,
        process=lambda text: _process_c_consequence(update, context, text),
    )


async def _process_c_consequence(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="C",
        bot_question=Q_C_TEMPLATE.format(narrative_short="то, как ты хочешь, "),
        text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return C_CONSEQUENCE
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    count = await _bump_messages(session_id)
    db.update_relationship_session(session_id, c_consequence=text)

    if await _maybe_wrap_to_summary(update, context, session_id, count):
        return E_SUMMARY

    feeling_question = await _ask_feeling_question(session_id)
    context.user_data["rel_feeling_question"] = feeling_question
    await update.effective_message.reply_text(feeling_question)
    return C_FEELING


async def c_feeling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_c_feeling", conv_handler=conv_handler, state=C_FEELING,
        process=lambda text: _process_c_feeling(update, context, text),
    )


async def _process_c_feeling(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    feeling_question = context.user_data.get("rel_feeling_question", "")

    status = await hostility.precheck(
        update, context, branch="relationships", step="C_feeling",
        bot_question=feeling_question, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return C_FEELING
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    count = await _bump_messages(session_id)
    row = db.get_relationship_session(session_id)
    combined_consequence = f"{row['c_consequence']}\nЧувство: {text}"
    db.update_relationship_session(session_id, c_consequence=combined_consequence)

    if await _maybe_wrap_to_summary(update, context, session_id, count):
        return E_SUMMARY

    advice = await llm.generate_i_would_advice(row["a_event"], row["b_narrative_confirmed"], combined_consequence)
    await update.effective_message.reply_text(advice)
    context.user_data["rel_d_offer_text"] = advice
    return D_CONFIRM


async def d_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_d_confirm", conv_handler=conv_handler, state=D_CONFIRM,
        process=lambda text: _process_d_confirm(update, context, text),
    )


async def _process_d_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    offer_text = context.user_data.get("rel_d_offer_text", "")

    status = await hostility.precheck(
        update, context, branch="relationships", step="D_confirm",
        bot_question=offer_text, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return D_CONFIRM
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)

    agreed = await llm.classify_yes_no(text)
    if not agreed:
        db.finish_relationship_session(session_id, exit_step="declined_D", completed=False)
        await _log_session(session_id)
        await update.effective_message.reply_text(DECLINE_D_TEXT, reply_markup=back_to_menu_keyboard())
        context.user_data.pop("rel_session_id", None)
        return ConversationHandler.END

    row = db.get_relationship_session(session_id)
    question = await llm.adapt_dispute_question(1, row["b_narrative_confirmed"])
    context.user_data["rel_d_index"] = 1
    await update.effective_message.reply_text(question)
    return D_QUESTION


async def d_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_d", conv_handler=conv_handler, state=D_QUESTION,
        process=lambda text: _process_d_question(update, context, text),
    )


async def _process_d_question(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    # По умолчанию модуль панчлайнов на шаге D выключен: сопротивление вроде
    # «да это бред, он всё равно виноват» — материал самого разбора, не выпад
    # против бота (см. ТЗ-дополнение, п.4 исключений). Кризис проверяем всегда.
    status = await hostility.precheck(
        update, context, branch="relationships", step="D", skip_hostility=True, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END

    count = await _bump_messages(session_id)
    idx = context.user_data["rel_d_index"]
    db.update_relationship_session(session_id, **{D_FIELDS[idx]: text})

    if await _maybe_wrap_to_summary(update, context, session_id, count):
        return E_SUMMARY

    if idx < 8:
        idx += 1
        context.user_data["rel_d_index"] = idx
        row = db.get_relationship_session(session_id)
        question = await llm.adapt_dispute_question(idx, row["b_narrative_confirmed"])
        await update.effective_message.reply_text(question)
        return D_QUESTION

    e_question = await _ask_e_question(context, session_id)
    await update.effective_message.reply_text(e_question)
    return E_SUMMARY


async def _finish_e(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, closing_text: str) -> int:
    db.finish_relationship_session(session_id, exit_step="E", completed=True)
    await _log_session(session_id)
    await update.effective_message.reply_text(closing_text, reply_markup=back_to_menu_keyboard())
    context.user_data.pop("rel_session_id", None)
    context.user_data.pop("rel_e_first_answer", None)
    return ConversationHandler.END


async def e_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_e", conv_handler=conv_handler, state=E_SUMMARY,
        process=lambda text: _process_e_summary(update, context, text),
    )


async def _process_e_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    e_question = context.user_data.get("rel_e_question", "")

    status = await hostility.precheck(
        update, context, branch="relationships", step="E", bot_question=e_question, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return E_SUMMARY
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, e_summary=text)

    row = db.get_relationship_session(session_id)
    analysis = await llm.analyze_e_insight(row["a_event"], row["b_narrative_confirmed"], "", text)
    if analysis["has_insight"]:
        return await _finish_e(update, context, session_id, analysis["reflection"])

    followup = await llm.ask_e_followup(row["b_narrative_confirmed"], text)
    context.user_data["rel_e_first_answer"] = text
    await update.effective_message.reply_text(followup)
    return E_FOLLOWUP


async def e_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_e_followup", conv_handler=conv_handler, state=E_FOLLOWUP,
        process=lambda text: _process_e_followup(update, context, text),
    )


async def _process_e_followup(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="E_followup", text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return E_FOLLOWUP
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    first_answer = context.user_data.get("rel_e_first_answer", "")
    db.update_relationship_session(session_id, e_summary=f"{first_answer}\n{text}")

    row = db.get_relationship_session(session_id)
    analysis = await llm.analyze_e_insight(row["a_event"], row["b_narrative_confirmed"], first_answer, text)
    if analysis["has_insight"]:
        return await _finish_e(update, context, session_id, analysis["reflection"])

    return await _finish_e(update, context, session_id, E_NO_INSIGHT_TEXT)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot.handlers.menu import show_menu

    session_id = context.user_data.pop("rel_session_id", None)
    if session_id:
        db.finish_relationship_session(session_id, exit_step="cancelled", completed=False)
        await _log_session(session_id)
    await show_menu(update, context)
    return ConversationHandler.END


conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(entry, pattern="^menu:relationships$")],
    states={
        A_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_event)],
        B_NARRATIVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, b_narrative)],
        B_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, b_confirm)],
        C_CONSEQUENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_consequence)],
        C_FEELING: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_feeling)],
        D_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_confirm)],
        D_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_question)],
        E_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_summary)],
        E_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_followup)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CommandHandler("start", cancel),
        CallbackQueryHandler(cancel, pattern="^menu:back$"),
    ],
    name="relationships_conversation",
    allow_reentry=True,
)
