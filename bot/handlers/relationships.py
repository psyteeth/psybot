import html
import logging
import random
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import db, debounce, hostility, limits, llm
from bot.config import (
    ADMIN_USERNAME,
    ENABLE_PRIOR_EVENT_QUESTION,
    LIMIT_RELATIONSHIP_MESSAGES,
    RELATIONSHIP_LIMIT_TEXT,
    ROADMAP_URL,
    TESTS_URL,
)
from bot.keyboards import back_to_menu_keyboard
from bot.sheets import sheets_logger

logger = logging.getLogger(__name__)

(
    A_EVENT, A_PRIOR_EVENT, A_OTHER_PERSON,
    B_NARRATIVE, B_CONFIRM, C_CONSEQUENCE, C_FEELING, C_DISCOMFORT,
    D_CONFIRM, D_QUESTION,
    E_DISCOMFORT_AFTER, E_SUMMARY_CONFIRM, E_SUMMARY_CORRECTION, E_SUMMARY, E_FOLLOWUP,
) = range(15)

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

PRIOR_EVENT_QUESTION = "А что было до этого? Может, чуть раньше что-то уже задело?"
OTHER_PERSON_QUESTION = "А кто это для тебя?"
DISCOMFORT_BEFORE_Q = "Насколько тебе сейчас дискомфортно от этой ситуации, от 0 до 10?"
DISCOMFORT_AFTER_Q = "И ещё раз, от 0 до 10: насколько тебе дискомфортно от этой ситуации сейчас?"
DISCOMFORT_RETRY_TEXT = "Напиши, пожалуйста, просто число от 0 до 10."


def _parse_discomfort(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{1,2})\s*[.!]?\s*", text)
    if not m:
        return None
    n = int(m.group(1))
    return n if 0 <= n <= 10 else None

SUMMARY_CONFIRM_SUFFIX = "\n\nПохоже ли это на правду?"
PARANOID_BOT_TEMPLATE = (
    "Прости, я совсем забыл сказать, что я бот-параноик, и сейчас я ощущаю космический посыл "
    "передать тебе следующую информацию из космоса: попахивает тем, что тебе хочется {hidden_need}, "
    "голос передаёт тебе: {punchline}"
)
FINAL_INSIGHT_TEMPLATE = (
    "{opener} сформулировать новую реакцию.\n\n"
    "В следующий раз в подобной ситуации ты можешь {insertion}\n\n"
    "Меняй своё мышление, а не других людей.\n"
    "Психостоматология №1\n\n"
    f'<a href="{ROADMAP_URL}">Зайти в работу</a>\n'
    "Пройти серию тестов и получить персональный портрет коммуникации - "
    f'<a href="{TESTS_URL}">здесь</a>'
)
FINAL_INSIGHT_OPENERS = ["Похоже, у тебя получилось", "Кажется, у тебя получилось"]

D_FIELDS = {
    1: "d1_logical", 2: "d2_empirical", 3: "d3_pragmatic", 4: "d4_hedonistic",
    5: "d5_catastrophe_scale", 6: "d6_historical", 7: "d7_double_standard", 8: "d8_semantic",
}


def _shift_value(before, after):
    if before is None or after is None:
        return ""
    return before - after


def _row_for_sheets(row) -> list:
    return [
        row["started_at"], row["user_id"], row["username"] or "", row["session_num"],
        row["a_event"], row["b_narrative_raw"], row["b_narrative_confirmed"], row["c_consequence"],
        row["d1_logical"], row["d2_empirical"], row["d3_pragmatic"], row["d4_hedonistic"],
        row["d5_catastrophe_scale"], row["d6_historical"], row["d7_double_standard"], row["d8_semantic"],
        row["e_summary"], row["exit_step"] or "", "да" if row["completed"] else "нет",
        row["message_count"], row["ended_at"] or "",
        row["event_before"] or "", row["other_person"] or "",
        row["discomfort_before"] if row["discomfort_before"] is not None else "",
        row["discomfort_after"] if row["discomfort_after"] is not None else "",
        _shift_value(row["discomfort_before"], row["discomfort_after"]),
        row["reflection_before_e"] or "",
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

    if db.count_relationship_sessions_this_month(user.id) >= await limits.get_limit(
        context.bot, user.id, "relationships"
    ):
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
    db.finish_relationship_session(session_id, exit_step=exit_step)
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


def _pick_reflection_quotes(row) -> tuple[str | None, str]:
    """Самые содержательные ответы D1-D8 по длине (словам) — цитируем дословно,
    ничего не пересказываем и не интерпретируем (см. ТЗ-доп. №2, п.2.3)."""
    candidates = [
        row[f] for f in (
            "d1_logical", "d2_empirical", "d3_pragmatic", "d4_hedonistic",
            "d5_catastrophe_scale", "d6_historical", "d7_double_standard", "d8_semantic",
        )
    ]
    candidates = [c for c in candidates if c]
    if not candidates:
        return None, ""
    top = sorted(candidates, key=lambda t: len(t.split()), reverse=True)[:2]
    if len(top) == 1:
        text = f'На одном из вопросов ты сказал(а): "{top[0]}".'
    else:
        text = f'На одном из вопросов ты сказал(а): "{top[0]}". А на другом: "{top[1]}".'
    return text, " | ".join(top)


async def _start_e_finale(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> int:
    """Общая точка входа в финал разбора — что при нормальном прохождении всех D1-D8,
    что при форсированном сворачивании по лимиту 40 сообщений (см. _maybe_wrap_to_summary).
    Цитирует содержательные ответы (2.3), затем переспрашивает дискомфорт (2.1), и только
    потом идёт Voss-резюме/подтверждение."""
    row = db.get_relationship_session(session_id)
    quote_text, stored_quotes = _pick_reflection_quotes(row)
    if quote_text:
        await update.effective_message.reply_text(quote_text)
    db.update_relationship_session(session_id, reflection_before_e=stored_quotes)
    await update.effective_message.reply_text(DISCOMFORT_AFTER_Q)
    return E_DISCOMFORT_AFTER


async def _maybe_wrap_to_summary(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, count: int
) -> int | None:
    if count > LIMIT_RELATIONSHIP_MESSAGES:
        await update.effective_message.reply_text(WRAP_TEXT)
        return await _start_e_finale(update, context, session_id)
    return None


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
        db.finish_relationship_session(session_id, exit_step="crisis")
        await _log_session(session_id)
        await update.effective_message.reply_text(CRISIS_TEXT, reply_markup=back_to_menu_keyboard())
        return ConversationHandler.END

    if ENABLE_PRIOR_EVENT_QUESTION:
        await update.effective_message.reply_text(PRIOR_EVENT_QUESTION)
        return A_PRIOR_EVENT

    return await _classify_or_ask_other_person(update, context, session_id)


async def _classify_or_ask_other_person(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int
) -> int:
    row = db.get_relationship_session(session_id)
    other = await llm.classify_other_person(row["a_event"])
    if other:
        db.update_relationship_session(session_id, other_person=other)
        await update.effective_message.reply_text(Q_B)
        return B_NARRATIVE
    await update.effective_message.reply_text(OTHER_PERSON_QUESTION)
    return A_OTHER_PERSON


async def a_prior_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_a_prior", conv_handler=conv_handler, state=A_PRIOR_EVENT,
        process=lambda text: _process_a_prior_event(update, context, text),
    )


async def _process_a_prior_event(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="A_prior",
        bot_question=PRIOR_EVENT_QUESTION, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return A_PRIOR_EVENT
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, event_before=text)
    return await _classify_or_ask_other_person(update, context, session_id)


async def a_other_person(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_a_other", conv_handler=conv_handler, state=A_OTHER_PERSON,
        process=lambda text: _process_a_other_person(update, context, text),
    )


async def _process_a_other_person(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="A_other",
        bot_question=OTHER_PERSON_QUESTION, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return A_OTHER_PERSON
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    resolved = await llm.classify_other_person(text) or "другое"
    db.update_relationship_session(session_id, other_person=resolved)
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

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

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

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

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

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

    await update.effective_message.reply_text(DISCOMFORT_BEFORE_Q)
    return C_DISCOMFORT


async def c_discomfort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_c_discomfort", conv_handler=conv_handler, state=C_DISCOMFORT,
        process=lambda text: _process_c_discomfort(update, context, text),
    )


async def _process_c_discomfort(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="C_discomfort",
        bot_question=DISCOMFORT_BEFORE_Q, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return C_DISCOMFORT
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    value = _parse_discomfort(text)
    if value is None:
        await update.effective_message.reply_text(DISCOMFORT_RETRY_TEXT)
        return C_DISCOMFORT

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, discomfort_before=value)

    row = db.get_relationship_session(session_id)
    advice = await llm.generate_i_would_advice(row["a_event"], row["b_narrative_confirmed"], row["c_consequence"])
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
        db.finish_relationship_session(session_id, exit_step="declined_D")
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

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

    if idx < 8:
        idx += 1
        context.user_data["rel_d_index"] = idx
        row = db.get_relationship_session(session_id)
        question = await llm.adapt_dispute_question(idx, row["b_narrative_confirmed"])
        await update.effective_message.reply_text(question)
        return D_QUESTION

    return await _start_e_finale(update, context, session_id)


async def e_discomfort_after(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_e_discomfort", conv_handler=conv_handler, state=E_DISCOMFORT_AFTER,
        process=lambda text: _process_e_discomfort_after(update, context, text),
    )


async def _process_e_discomfort_after(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="E_discomfort",
        bot_question=DISCOMFORT_AFTER_Q, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return E_DISCOMFORT_AFTER
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    value = _parse_discomfort(text)
    if value is None:
        await update.effective_message.reply_text(DISCOMFORT_RETRY_TEXT)
        return E_DISCOMFORT_AFTER

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, discomfort_after=value)
    return await _send_summary_confirm(update, context, session_id)


async def _finish_e(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, insertion: str | None) -> int:
    db.finish_relationship_session(session_id, exit_step="E")
    await _log_session(session_id)

    if insertion:
        closing_text = FINAL_INSIGHT_TEMPLATE.format(
            opener=random.choice(FINAL_INSIGHT_OPENERS), insertion=html.escape(insertion)
        )
        await update.effective_message.reply_text(
            closing_text, reply_markup=back_to_menu_keyboard(), parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(E_NO_INSIGHT_TEXT, reply_markup=back_to_menu_keyboard())

    context.user_data.pop("rel_session_id", None)
    context.user_data.pop("rel_e_first_answer", None)
    context.user_data.pop("rel_summary", None)
    return ConversationHandler.END


async def _send_summary_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> int:
    row = db.get_relationship_session(session_id)
    d_answers = {i: row[D_FIELDS[i]] for i in range(1, 9)}
    summary = await llm.generate_session_summary(
        row["a_event"], row["b_narrative_confirmed"], row["c_consequence"], d_answers
    )
    context.user_data["rel_summary"] = summary
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да", callback_data="e_confirm:yes")],
            [InlineKeyboardButton("Хочу кое-то добавить/подправить", callback_data="e_confirm:add")],
        ]
    )
    message = update.callback_query.message if update.callback_query else update.effective_message
    await message.reply_text(summary + SUMMARY_CONFIRM_SUFFIX, reply_markup=keyboard)
    return E_SUMMARY_CONFIRM


async def e_summary_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session_id = context.user_data["rel_session_id"]

    if query.data.endswith(":add"):
        await query.message.reply_text("Что добавить или поправить?")
        return E_SUMMARY_CORRECTION

    e_question = await _ask_e_question(context, session_id)
    await query.message.reply_text(e_question)
    return E_SUMMARY


async def e_summary_correction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_e_correction", conv_handler=conv_handler, state=E_SUMMARY_CORRECTION,
        process=lambda text: _process_e_summary_correction(update, context, text),
    )


async def _process_e_summary_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]

    status = await hostility.precheck(
        update, context, branch="relationships", step="E_correction",
        bot_question="Что добавить или поправить?", text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(session_id, "self_harm_crisis")
        return ConversationHandler.END
    if status == "hostile":
        return E_SUMMARY_CORRECTION
    if status == "closed":
        await _close_after_hostility(session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    summary = context.user_data.get("rel_summary", "")

    edit_analysis = await llm.analyze_self_serving_edit(summary, text)
    if edit_analysis["self_serving"]:
        paranoid_text = PARANOID_BOT_TEMPLATE.format(
            hidden_need=edit_analysis["hidden_need"], punchline=edit_analysis["punchline"]
        )
        await update.effective_message.reply_text(paranoid_text)

    row = db.get_relationship_session(session_id)
    db.update_relationship_session(session_id, e_summary=f"Поправка к резюме: {text}")
    narrative_with_correction = f"{row['b_narrative_confirmed']}\n(поправка от пользователя: {text})"
    e_question = await llm.generate_e_question(row["a_event"], narrative_with_correction)
    context.user_data["rel_e_question"] = e_question
    await update.effective_message.reply_text(e_question)
    return E_SUMMARY


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

    return await _finish_e(update, context, session_id, None)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot.handlers.menu import show_menu

    session_id = context.user_data.pop("rel_session_id", None)
    if session_id:
        db.finish_relationship_session(session_id, exit_step="cancelled")
        await _log_session(session_id)
    await show_menu(update, context)
    return ConversationHandler.END


conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(entry, pattern="^menu:relationships$")],
    states={
        A_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_event)],
        A_PRIOR_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_prior_event)],
        A_OTHER_PERSON: [MessageHandler(filters.TEXT & ~filters.COMMAND, a_other_person)],
        B_NARRATIVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, b_narrative)],
        B_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, b_confirm)],
        C_CONSEQUENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_consequence)],
        C_FEELING: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_feeling)],
        C_DISCOMFORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_discomfort)],
        D_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_confirm)],
        D_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_question)],
        E_DISCOMFORT_AFTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_discomfort_after)],
        E_SUMMARY_CONFIRM: [CallbackQueryHandler(e_summary_confirm, pattern="^e_confirm:")],
        E_SUMMARY_CORRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_summary_correction)],
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
