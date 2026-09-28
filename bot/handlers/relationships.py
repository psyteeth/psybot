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

from bot import db, debounce, dialogue_matrix, hostility, limits, llm
from bot.config import (
    ADMIN_USERNAME,
    ENABLE_PRIOR_EVENT_QUESTION,
    EXIT_INTENT_CONFIDENCE_THRESHOLD,
    LIMIT_RELATIONSHIP_MESSAGES,
    RELATIONSHIP_LIMIT_TEXT,
    ROADMAP_URL,
    TESTS_URL,
)
from bot.keyboards import back_to_menu_keyboard, menu_and_teeth_keyboard
from bot.sheets import sheets_logger

logger = logging.getLogger(__name__)

# Новые состояния (D5_QUESTION, D5_HUNDRED_FOLLOWUP, EXIT_INTENT_CLARIFY) дописаны В КОНЕЦ,
# а не вставлены по смыслу между старыми — persistent=True хранит состояние как целое число
# на диске (PicklePersistence), и перенумерация уже существующих состояний сломала бы разборы,
# начатые до деплоя этого изменения.
(
    A_EVENT, A_PRIOR_EVENT, A_OTHER_PERSON,
    B_NARRATIVE, B_CONFIRM, C_CONSEQUENCE, C_FEELING, C_DISCOMFORT,
    D_CONFIRM, D_QUESTION,
    E_DISCOMFORT_AFTER, E_SUMMARY_CONFIRM, E_SUMMARY_CORRECTION, E_SUMMARY, E_FOLLOWUP,
    D5_QUESTION, D5_HUNDRED_FOLLOWUP, EXIT_INTENT_CLARIFY,
) = range(18)

Q_A = (
    "Опиши событие или поведение другого человека, от которого тебе дискомфортно: "
    "триггерит, бесит, раздражает, обламывает, достаёт."
)
Q_B = "Как бы ты хотел, чтобы было иначе? Что другой человек должен был сделать по-другому?"
Q_C_TEMPLATE = (
    "Когда {narrative_short}не совпадает с тем, что происходит на самом деле — что ты чувствуешь "
    "и как реагируешь?"
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


# --- ТЗ-доп. №5, раздел 1: шкала катастроф с опорой 100 ---
D5_QUESTION_TEMPLATE = (
    "Шкала катастроф от 0 до 100. Сейчас будет жёстко.\n\n"
    "100 — это полная жопа: ты в катастрофе, лежишь в больнице, у тебя нет рук или ног, всё "
    "болит, и так ты будешь жить ещё много лет.\n\n"
    "Оцени по этой шкале, от 0 до этого 100, то, что происходит у тебя: {situation}"
)
D5_HUNDRED_FOLLOWUP_Q = "Это так же, как лежать без рук и ног и жить с этой болью годами?"
SCALE_100_RETRY_TEXT = "Напиши, пожалуйста, просто число от 0 до 100."


def _parse_scale_100(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{1,3})\s*[.!]?\s*", text)
    if not m:
        return None
    n = int(m.group(1))
    return n if 0 <= n <= 100 else None


# --- ТЗ-доп. №5, раздел 2: избегание или интеграция ---
EXIT_INTENT_STEP1_TEMPLATE = (
    "Стоп, это очень важный момент, на него стоит обратить внимание.\n\n"
    "Скажи честно: это больше про то, что ты лучше, а он плохой, и с такими тебе не по пути? Или "
    "про то, что ты начинаешь позволять себе вести себя так же, как он, — {behavior} — по "
    "отношению к людям?"
)
AVOIDANCE_TEXT = (
    "Если это избегание — ты лучше, он плохой, и с такими ты больше не общаешься, — "
    "психостоматология рекомендует здесь хорошенечко подумать."
)
INTEGRATION_TEXT = (
    "Если ты начинаешь позволять себе вести себя так же — {behavior} — по отношению к людям, это "
    "очень интересный момент. Обрати на него внимание: может быть, это начало чего-то нового в "
    "жизни."
)
EXIT_INTENT_CLOSING_TEXT = (
    f"Хочется подробностей — приходи в работу с зубами: {ROADMAP_URL} или пиши {ADMIN_USERNAME}."
)


async def _check_exit_intent(context: ContextTypes.DEFAULT_TYPE, session_id: int, step: str, text: str) -> None:
    """Запоминает только ПЕРВОЕ срабатывание за сессию (шаг_выхода_из_контакта — одно значение),
    дальше не тратим лишние вызовы классификатора."""
    if context.user_data.get("exit_intent_flagged"):
        return
    verdict = await llm.classify_exit_intent(text)
    if verdict["exit_intent"] and verdict["confidence"] >= EXIT_INTENT_CONFIDENCE_THRESHOLD:
        context.user_data["exit_intent_flagged"] = True
        db.update_relationship_session(session_id, exit_intent=1, exit_intent_step=step)


SELF_REFUSAL_TEXT = (
    "Тогда сейчас разбирать не будем. Разбор работает, когда тебя бесит кто-то другой, — приходи, "
    "когда такое случится.\n\n"
    "А то, что ты переживаешь из-за себя, — во-первых, может говорить об оголяющихся шейках зубов. "
    f"С этим можно прийти на диагностику: пиши {ADMIN_USERNAME}. Во-вторых, это просто не "
    "соответствует логике разбора.\n\n"
    "И не надо тут на вопрос «кто тебя бесит» отвечать «я сам». Мы в Психостоматологии №1 знаем, "
    "что это петушиный крик псевдосвятости."
)

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
        "да" if row["self_request"] else "нет",
        row["self_check_step"] or "",
        row["self_original_answer"] or "",
        "да" if row["self_reformulated"] else "нет",
        "да" if row["self_refused"] else "нет",
        row["note"] or "",
        row["d5_comment"] or "",
        row["d5_original"] if row["d5_original"] is not None else "",
        "да" if row["exit_intent"] else "нет",
        row["exit_intent_step"] or "",
        row["avoidance_or_integration"] or "",
        row["exit_intent_answer"] or "",
        _dialogue_session_id(row),
        row["attempts_a"] if row["attempts_a"] is not None else "",
        row["attempts_b"] if row["attempts_b"] is not None else "",
        row["dozhim_outcome"] or "",
    ]


def _dialogue_session_id(row) -> str:
    """session_id для склейки «Диалоги»/«Диалоги (матрица)» с «Отношения»/«Зубы»
    (ТЗ-доп. №6) — считается на лету из user_id+started_at, отдельная колонка в БД не нужна."""
    return f"{row['user_id']}_{row['started_at']}"


async def _log_session(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> None:
    row = db.get_relationship_session(session_id)
    await sheets_logger.append("Отношения", _row_for_sheets(row))
    try:
        await dialogue_matrix.build_relationship_matrix_column(
            _dialogue_session_id(row), row["username"] or "", row["started_at"]
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось построить столбец матрицы для сессии %s", session_id)


async def _log_turn(session_id: int, user, step: str, who: str, text: str, msg_type: str = "обычный") -> None:
    """ТЗ-доп. №5, п.3.1 / ТЗ-доп. №6, ч.1 — полный лог диалога, одна строка на реплику (и
    бота, и человека), отдельно от итоговой строки в «Отношения»."""
    row = db.get_relationship_session(session_id)
    session_num = row["session_num"] if row else ""
    dialogue_session_id = _dialogue_session_id(row) if row else ""
    await sheets_logger.append(
        "Диалоги",
        [
            db.now(), user.id, dialogue_session_id, user.username or "", "relationships",
            session_num, step, who, (text or "")[:2000], msg_type,
        ],
    )


async def _send(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, step: str, text: str,
    msg_type: str = "обычный", **kwargs,
):
    """reply_text + запись реплики бота в «Диалоги» одним вызовом."""
    target = update.callback_query.message if update.callback_query else update.effective_message
    result = await target.reply_text(text, **kwargs)
    await _log_turn(session_id, update.effective_user, step, "бот", text, msg_type)
    return result


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    db.upsert_user(user.id, user.username)

    # allow_reentry=True означает, что этот энтри-поинт проверяется на КАЖДОМ тапе
    # "Отношения", даже посреди уже идущего разбора. Быстрый повторный тап (двойной клик,
    # нетерпеливость) иначе плодит новые сессии впустую, сжирая лимит без единого ответа
    # пользователя — реальный случай: 3 сессии за 1 секунду от одного человека.
    existing_id = context.user_data.get("rel_session_id")
    if existing_id is not None:
        existing_row = db.get_relationship_session(existing_id)
        if existing_row is not None and existing_row["ended_at"] is None:
            await query.answer("Разбор уже идёт — отвечай в чате выше 👆")
            return None

    await query.answer()
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
    await _log_turn(session_id, user, "A", "бот", Q_A)
    return A_EVENT


async def _close_after_hostility(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, exit_step: str
) -> None:
    db.finish_relationship_session(session_id, exit_step=exit_step)
    await _log_session(update, context, session_id)
    await hostility.maybe_send_self_harm_note(update, context)


async def _bump_messages(session_id: int) -> int:
    return db.increment_relationship_messages(session_id)


DOZHIM_ATTEMPT1_TEXT = {
    "A": (
        "Я слышу, но не понимаю. Вопрос-то был: что тебя в нём бесит?\n"
        "Продолжи предложение: «Меня в нём бесит то, что он…» (или «она…»)."
    ),
    "B": (
        "Я слышу, но не понимаю. Вопрос-то был: как, по-твоему, он должен был бы себя вести?\n"
        "Продолжи: «Он должен был бы…» (или «она должна была бы…»)."
    ),
}
DOZHIM_ATTEMPT2_TEXT = {
    "A": (
        "Слышь, давай без этого, мозги не еби. Ответь конкретно: что тебя бесит, раздражает, "
        "триггерит, печалит, задевает в поведении другого?\n"
        "Продолжи: «Меня в нём бесит то, что он…»"
    ),
    "B": (
        "Слышь, давай без этого, мозги не еби. Ответь конкретно: как, по-твоему, он должен был бы "
        "себя вести?\n"
        "Продолжи: «Он должен был бы…»"
    ),
}
DOZHIM_EVASIVE_CLOSE_TEXT = (
    "Слышишь, что-то не то происходит. Где-то ты юлишь. Как будешь готов конкретно сказать, чем "
    "тебя раздражает другой, — начинай заново, и я тебе помогу.\n\n"
    f"Если считаешь, что это ошибка, напиши администратору {ADMIN_USERNAME} — он решит этот вопрос."
)
DOZHIM_SELF_CLOSE_TEXT = (
    SELF_REFUSAL_TEXT + "\n\n"
    f"Если считаешь, что это ошибка, напиши администратору {ADMIN_USERNAME} — он решит этот вопрос."
)


async def _check_dozhim(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, step: str, text: str
) -> object:
    """Дожим конкретного ответа на A и B (ТЗ-доп. №6, ч.2 — заменяет разделы 2-3 ТЗ-доп. №3
    целиком). Своя лестница из 3 попыток на A и своя на B. Возвращает None, если ответ принят
    (target=other И concrete_behavior=true) — вызывающий обработчик продолжает как обычно.
    Иначе уже отправила ответ пользователю и вернула состояние."""
    attempts_key = f"rel_dozhim_{step.lower()}_attempts"
    attempts = context.user_data.get(attempts_key, 0)

    classification = await llm.classify_concrete_answer(step, text)
    accepted = classification["target"] == "other" and classification["concrete_behavior"]

    if accepted:
        outcome = "принят с первого раза" if attempts == 0 else "переформулировал"
        db.update_relationship_session(
            session_id, **{f"attempts_{step.lower()}": attempts, "dozhim_outcome": outcome}
        )
        return None

    attempts += 1
    context.user_data[attempts_key] = attempts

    if attempts >= 3:
        is_self = classification["target"] == "self"
        outcome = "отказ_самообвинение" if is_self else "отказ_юлит"
        close_text = DOZHIM_SELF_CLOSE_TEXT if is_self else DOZHIM_EVASIVE_CLOSE_TEXT
        db.update_relationship_session(
            session_id, self_refused=1,
            **{f"attempts_{step.lower()}": attempts, "dozhim_outcome": outcome},
        )
        db.finish_relationship_session(session_id, exit_step=outcome)
        await _log_session(update, context, session_id)
        await _send(
            update, context, session_id, f"проверка_{step}", close_text, msg_type="отказ",
            reply_markup=menu_and_teeth_keyboard(),
        )
        await hostility.maybe_send_self_harm_note(update, context)
        context.user_data.pop("rel_session_id", None)
        context.user_data.pop("rel_dozhim_a_attempts", None)
        context.user_data.pop("rel_dozhim_b_attempts", None)
        return ConversationHandler.END

    prompt = DOZHIM_ATTEMPT1_TEXT[step] if attempts == 1 else DOZHIM_ATTEMPT2_TEXT[step]
    await _send(update, context, session_id, f"проверка_{step}", prompt, msg_type="уточнение")
    return A_EVENT if step == "A" else B_NARRATIVE


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
    """Самые содержательные ответы D1-D4/D6-D8 по длине (словам) — цитируем дословно,
    ничего не пересказываем и не интерпретируем (см. ТЗ-доп. №2, п.2.3). D5 — число, не
    описательный текст, в цитаты не годится (ТЗ-доп. №5)."""
    candidates = [
        row[f] for f in (
            "d1_logical", "d2_empirical", "d3_pragmatic", "d4_hedonistic",
            "d6_historical", "d7_double_standard", "d8_semantic",
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
        await _send(update, context, session_id, "E_reflection", quote_text)
    db.update_relationship_session(session_id, reflection_before_e=stored_quotes)
    await _send(update, context, session_id, "E_discomfort_after", DISCOMFORT_AFTER_Q)
    return E_DISCOMFORT_AFTER


async def _maybe_wrap_to_summary(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, count: int
) -> int | None:
    if count > LIMIT_RELATIONSHIP_MESSAGES:
        await _send(update, context, session_id, "wrap", WRAP_TEXT)
        return await _start_e_finale(update, context, session_id)
    return None


async def a_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_a", conv_handler=conv_handler, state=A_EVENT,
        process=lambda text: _process_a_event(update, context, text),
    )


async def _process_a_event(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "A", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="A", bot_question=Q_A, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return A_EVENT
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, a_event=text)

    dozhim_state = await _check_dozhim(update, context, session_id, "A", text)
    if dozhim_state is not None:
        return dozhim_state

    if ENABLE_PRIOR_EVENT_QUESTION:
        await _send(update, context, session_id, "A_prior", PRIOR_EVENT_QUESTION)
        return A_PRIOR_EVENT

    return await _classify_or_ask_other_person(update, context, session_id)


async def _classify_or_ask_other_person(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int
) -> int:
    row = db.get_relationship_session(session_id)
    other = await llm.classify_other_person(row["a_event"])
    if other:
        db.update_relationship_session(session_id, other_person=other)
        await _send(update, context, session_id, "B", Q_B)
        return B_NARRATIVE
    await _send(update, context, session_id, "A_other", OTHER_PERSON_QUESTION)
    return A_OTHER_PERSON


async def a_prior_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_a_prior", conv_handler=conv_handler, state=A_PRIOR_EVENT,
        process=lambda text: _process_a_prior_event(update, context, text),
    )


async def _process_a_prior_event(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "A_prior", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="A_prior",
        bot_question=PRIOR_EVENT_QUESTION, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return A_PRIOR_EVENT
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
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
    await _log_turn(session_id, update.effective_user, "A_other", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="A_other",
        bot_question=OTHER_PERSON_QUESTION, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return A_OTHER_PERSON
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    resolved = await llm.classify_other_person(text) or "другое"
    db.update_relationship_session(session_id, other_person=resolved)
    await _send(update, context, session_id, "B", Q_B)
    return B_NARRATIVE


async def b_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_b", conv_handler=conv_handler, state=B_NARRATIVE,
        process=lambda text: _process_b_narrative(update, context, text),
    )


async def _process_b_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "B", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="B", bot_question=Q_B, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return B_NARRATIVE
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, b_narrative_raw=text)
    context.user_data["rel_raw_narrative"] = text

    dozhim_state = await _check_dozhim(update, context, session_id, "B", text)
    if dozhim_state is not None:
        return dozhim_state

    row = db.get_relationship_session(session_id)
    reformulated = await llm.reformulate_narrative(row["a_event"], text)
    context.user_data["rel_pending_narrative"] = reformulated
    await _send(update, context, session_id, "B_confirm", reformulated)
    return B_CONFIRM


async def b_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_b_confirm", conv_handler=conv_handler, state=B_CONFIRM,
        process=lambda text: _process_b_confirm(update, context, text),
    )


async def _process_b_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    pending_question = context.user_data.get("rel_pending_narrative", "")
    await _log_turn(session_id, update.effective_user, "B_confirm", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="B_confirm",
        bot_question=pending_question, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return B_CONFIRM
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    count = await _bump_messages(session_id)

    verdict = await llm.classify_confirmation(text)
    if verdict == "correct":
        row = db.get_relationship_session(session_id)
        reformulated = await llm.reformulate_narrative(
            row["a_event"], context.user_data["rel_raw_narrative"], correction=text
        )
        context.user_data["rel_pending_narrative"] = reformulated
        await _send(update, context, session_id, "B_confirm", reformulated)
        return B_CONFIRM

    confirmed = context.user_data.pop("rel_pending_narrative")
    db.update_relationship_session(session_id, b_narrative_confirmed=confirmed)

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

    c_question = Q_C_TEMPLATE.format(narrative_short="то, как ты хочешь, ")
    await _send(update, context, session_id, "C", c_question)
    return C_CONSEQUENCE


async def c_consequence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_c", conv_handler=conv_handler, state=C_CONSEQUENCE,
        process=lambda text: _process_c_consequence(update, context, text),
    )


async def _process_c_consequence(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "C", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="C",
        bot_question=Q_C_TEMPLATE.format(narrative_short="то, как ты хочешь, "),
        text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return C_CONSEQUENCE
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    count = await _bump_messages(session_id)
    db.update_relationship_session(session_id, c_consequence=text)

    await _check_exit_intent(context, session_id, "C", text)

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

    feeling_question = await _ask_feeling_question(session_id)
    context.user_data["rel_feeling_question"] = feeling_question
    await _send(update, context, session_id, "C_feeling", feeling_question)
    return C_FEELING


async def c_feeling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_c_feeling", conv_handler=conv_handler, state=C_FEELING,
        process=lambda text: _process_c_feeling(update, context, text),
    )


async def _process_c_feeling(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    feeling_question = context.user_data.get("rel_feeling_question", "")
    await _log_turn(session_id, update.effective_user, "C_feeling", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="C_feeling",
        bot_question=feeling_question, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return C_FEELING
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    count = await _bump_messages(session_id)
    row = db.get_relationship_session(session_id)
    combined_consequence = f"{row['c_consequence']}\nЧувство: {text}"
    db.update_relationship_session(session_id, c_consequence=combined_consequence)

    await _check_exit_intent(context, session_id, "C_feeling", text)

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

    await _send(update, context, session_id, "C_discomfort", DISCOMFORT_BEFORE_Q)
    return C_DISCOMFORT


async def c_discomfort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_c_discomfort", conv_handler=conv_handler, state=C_DISCOMFORT,
        process=lambda text: _process_c_discomfort(update, context, text),
    )


async def _process_c_discomfort(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "C_discomfort", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="C_discomfort",
        bot_question=DISCOMFORT_BEFORE_Q, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return C_DISCOMFORT
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    value = _parse_discomfort(text)
    if value is None:
        await _send(update, context, session_id, "C_discomfort", DISCOMFORT_RETRY_TEXT)
        return C_DISCOMFORT

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, discomfort_before=value)

    row = db.get_relationship_session(session_id)
    advice = await llm.generate_i_would_advice(row["a_event"], row["b_narrative_confirmed"], row["c_consequence"])
    await _send(update, context, session_id, "D_confirm", advice)
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
    await _log_turn(session_id, update.effective_user, "D_confirm", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="D_confirm",
        bot_question=offer_text, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return D_CONFIRM
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)

    agreed = await llm.classify_yes_no(text)
    if not agreed:
        db.finish_relationship_session(session_id, exit_step="declined_D")
        await _log_session(update, context, session_id)
        await _send(update, context, session_id, "D_confirm", DECLINE_D_TEXT, reply_markup=back_to_menu_keyboard())
        await hostility.maybe_send_self_harm_note(update, context)
        context.user_data.pop("rel_session_id", None)
        return ConversationHandler.END

    row = db.get_relationship_session(session_id)
    question = await llm.adapt_dispute_question(1, row["b_narrative_confirmed"])
    context.user_data["rel_d_index"] = 1
    await _send(update, context, session_id, "D1", question)
    return D_QUESTION


async def d_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_d", conv_handler=conv_handler, state=D_QUESTION,
        process=lambda text: _process_d_question(update, context, text),
    )


async def _process_d_question(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    idx = context.user_data["rel_d_index"]
    await _log_turn(session_id, update.effective_user, f"D{idx}", "человек", text)

    # По умолчанию модуль панчлайнов на шаге D выключен: сопротивление вроде
    # «да это бред, он всё равно виноват» — материал самого разбора, не выпад
    # против бота (см. ТЗ-дополнение, п.4 исключений). Кризис проверяем всегда.
    status = await hostility.precheck(
        update, context, branch="relationships", step="D", skip_hostility=True, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END

    count = await _bump_messages(session_id)
    db.update_relationship_session(session_id, **{D_FIELDS[idx]: text})

    await _check_exit_intent(context, session_id, f"D{idx}", text)

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

    if idx < 8:
        idx += 1
        context.user_data["rel_d_index"] = idx
        if idx == 5:
            row = db.get_relationship_session(session_id)
            question = D5_QUESTION_TEMPLATE.format(situation=row["a_event"])
            await _send(update, context, session_id, "D5", question)
            return D5_QUESTION
        row = db.get_relationship_session(session_id)
        question = await llm.adapt_dispute_question(idx, row["b_narrative_confirmed"])
        await _send(update, context, session_id, f"D{idx}", question)
        return D_QUESTION

    return await _start_e_finale(update, context, session_id)


async def d5_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_d5", conv_handler=conv_handler, state=D5_QUESTION,
        process=lambda text: _process_d5_question(update, context, text),
    )


async def _process_d5_question(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "D5", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="D5", skip_hostility=True, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END

    value = _parse_scale_100(text)
    if value is None:
        await _send(update, context, session_id, "D5", SCALE_100_RETRY_TEXT)
        return D5_QUESTION

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, d5_catastrophe_scale=value, d5_comment=text)

    if value == 100:
        await _send(update, context, session_id, "D5_hundred", D5_HUNDRED_FOLLOWUP_Q)
        return D5_HUNDRED_FOLLOWUP

    return await _advance_past_d5(update, context, session_id)


async def _advance_past_d5(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> int:
    row = db.get_relationship_session(session_id)
    context.user_data["rel_d_index"] = 6
    question = await llm.adapt_dispute_question(6, row["b_narrative_confirmed"])
    await _send(update, context, session_id, "D6", question)
    return D_QUESTION


async def d5_hundred_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_d5_hundred", conv_handler=conv_handler, state=D5_HUNDRED_FOLLOWUP,
        process=lambda text: _process_d5_hundred_followup(update, context, text),
    )


async def _process_d5_hundred_followup(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "D5_hundred", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="D5_hundred", skip_hostility=True, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END

    await _bump_messages(session_id)

    # Принимаем ответ как есть, не спорим — но если человек сам называет новое число вместо
    # прежних 100, записываем ревизию, исходную оценку сохраняя отдельно (ТЗ-доп. №5, п.1).
    revised = await llm.extract_revised_scale(text)
    if revised is not None and revised != 100:
        db.update_relationship_session(session_id, d5_original=100, d5_catastrophe_scale=revised)

    return await _advance_past_d5(update, context, session_id)


async def e_discomfort_after(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_e_discomfort", conv_handler=conv_handler, state=E_DISCOMFORT_AFTER,
        process=lambda text: _process_e_discomfort_after(update, context, text),
    )


async def _process_e_discomfort_after(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "E_discomfort", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="E_discomfort",
        bot_question=DISCOMFORT_AFTER_Q, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return E_DISCOMFORT_AFTER
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    value = _parse_discomfort(text)
    if value is None:
        await _send(update, context, session_id, "E_discomfort", DISCOMFORT_RETRY_TEXT)
        return E_DISCOMFORT_AFTER

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, discomfort_after=value)
    return await _send_summary_confirm(update, context, session_id)


async def _finish_e(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, insertion: str | None) -> int:
    db.finish_relationship_session(session_id, exit_step="E")

    if insertion:
        closing_text = FINAL_INSIGHT_TEMPLATE.format(
            opener=random.choice(FINAL_INSIGHT_OPENERS), insertion=html.escape(insertion)
        )
        await _send(
            update, context, session_id, "E_finish", closing_text,
            reply_markup=back_to_menu_keyboard(), parse_mode=ParseMode.HTML,
        )
    else:
        await _send(update, context, session_id, "E_finish", E_NO_INSIGHT_TEXT, reply_markup=back_to_menu_keyboard())

    # «Избегание или интеграция» (ТЗ-доп. №5, раздел 2) — если хоть раз сработало на C-D8/E,
    # разбор не заканчивается тут же, а продолжается ещё одним обменом. Лог в Sheets поэтому
    # откладывается до полного разрешения (иначе пришлось бы потом патчить уже отправленную строку).
    if context.user_data.get("exit_intent_flagged"):
        row = db.get_relationship_session(session_id)
        behavior = await llm.extract_disowned_behavior(row["a_event"], row["b_narrative_confirmed"])
        context.user_data["exit_intent_behavior"] = behavior
        question = EXIT_INTENT_STEP1_TEMPLATE.format(behavior=behavior)
        await _send(update, context, session_id, "exit_intent_check", question)
        return EXIT_INTENT_CLARIFY

    await _log_session(update, context, session_id)
    await hostility.maybe_send_self_harm_note(update, context)
    context.user_data.pop("rel_session_id", None)
    context.user_data.pop("rel_e_first_answer", None)
    context.user_data.pop("rel_summary", None)
    return ConversationHandler.END


async def exit_intent_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_exit_intent", conv_handler=conv_handler, state=EXIT_INTENT_CLARIFY,
        process=lambda text: _process_exit_intent_clarify(update, context, text),
    )


async def _process_exit_intent_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "exit_intent_check", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="exit_intent_check", text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return EXIT_INTENT_CLARIFY
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    verdict = await llm.classify_avoidance_integration(text)
    behavior = context.user_data.get("exit_intent_behavior", "вести себя так же, как он")
    db.update_relationship_session(session_id, avoidance_or_integration=verdict, exit_intent_answer=text)

    parts = []
    if verdict in ("avoidance", "unclear"):
        parts.append(AVOIDANCE_TEXT)
    if verdict in ("integration", "unclear"):
        parts.append(INTEGRATION_TEXT.format(behavior=behavior))
    parts.append(EXIT_INTENT_CLOSING_TEXT)
    await _send(
        update, context, session_id, "exit_intent_check", "\n\n".join(parts),
        reply_markup=back_to_menu_keyboard(),
    )

    await _log_session(update, context, session_id)
    await hostility.maybe_send_self_harm_note(update, context)
    context.user_data.pop("rel_session_id", None)
    context.user_data.pop("exit_intent_flagged", None)
    context.user_data.pop("exit_intent_behavior", None)
    context.user_data.pop("rel_e_first_answer", None)
    context.user_data.pop("rel_summary", None)
    return ConversationHandler.END


async def _send_summary_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> int:
    row = db.get_relationship_session(session_id)
    d_answers = {i: row[D_FIELDS[i]] for i in range(1, 9)}
    summary = await llm.generate_session_summary(
        row["a_event"], row["b_narrative_confirmed"], row["c_consequence"], d_answers,
        discomfort_before=row["discomfort_before"], discomfort_after=row["discomfort_after"],
    )
    context.user_data["rel_summary"] = summary
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да", callback_data="e_confirm:yes")],
            [InlineKeyboardButton("Хочу кое-то добавить/подправить", callback_data="e_confirm:add")],
        ]
    )
    await _send(
        update, context, session_id, "E_summary_confirm", summary + SUMMARY_CONFIRM_SUFFIX,
        reply_markup=keyboard,
    )
    return E_SUMMARY_CONFIRM


async def e_summary_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session_id = context.user_data["rel_session_id"]
    await _log_turn(
        session_id, update.effective_user, "E_summary_confirm", "человек", query.data, msg_type="кнопка"
    )

    if query.data.endswith(":add"):
        await _send(update, context, session_id, "E_summary_correction", "Что добавить или поправить?")
        return E_SUMMARY_CORRECTION

    e_question = await _ask_e_question(context, session_id)
    await _send(update, context, session_id, "E", e_question)
    return E_SUMMARY


async def e_summary_correction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_e_correction", conv_handler=conv_handler, state=E_SUMMARY_CORRECTION,
        process=lambda text: _process_e_summary_correction(update, context, text),
    )


async def _process_e_summary_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "E_summary_correction", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="E_correction",
        bot_question="Что добавить или поправить?", text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return E_SUMMARY_CORRECTION
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    summary = context.user_data.get("rel_summary", "")

    edit_analysis = await llm.analyze_self_serving_edit(summary, text)
    if edit_analysis["self_serving"]:
        paranoid_text = PARANOID_BOT_TEMPLATE.format(
            hidden_need=edit_analysis["hidden_need"], punchline=edit_analysis["punchline"]
        )
        await _send(update, context, session_id, "E_summary_correction", paranoid_text)

    row = db.get_relationship_session(session_id)
    db.update_relationship_session(session_id, e_summary=f"Поправка к резюме: {text}")

    await _check_exit_intent(context, session_id, "E_correction", text)

    narrative_with_correction = f"{row['b_narrative_confirmed']}\n(поправка от пользователя: {text})"
    e_question = await llm.generate_e_question(row["a_event"], narrative_with_correction)
    context.user_data["rel_e_question"] = e_question
    await _send(update, context, session_id, "E", e_question)
    return E_SUMMARY


async def e_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_e", conv_handler=conv_handler, state=E_SUMMARY,
        process=lambda text: _process_e_summary(update, context, text),
    )


async def _process_e_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    e_question = context.user_data.get("rel_e_question", "")
    await _log_turn(session_id, update.effective_user, "E", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="E", bot_question=e_question, text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return E_SUMMARY
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, e_summary=text)

    await _check_exit_intent(context, session_id, "E", text)

    row = db.get_relationship_session(session_id)

    # Живой баг: E_QUESTION_SYSTEM раньше сразу утверждал, что мысль «теряет силу» — а человек мог
    # прямо ответить, что стало ХУЖЕ. Теперь вопрос честный (не предполагает результат), и если
    # ответ явно про «не изменилось/стало хуже» — не продолжаем выискивать инсайт, а закрываем
    # честно (тот же текст, что и при отсутствии инсайта, приглашение на диагностику).
    shift = await llm.classify_e_shift(text)
    if shift == "same_or_worse":
        return await _finish_e(update, context, session_id, None)

    analysis = await llm.analyze_e_insight(row["a_event"], row["b_narrative_confirmed"], "", text)
    if analysis["has_insight"]:
        return await _finish_e(update, context, session_id, analysis["reflection"])

    followup = await llm.ask_e_followup(row["b_narrative_confirmed"], text)
    context.user_data["rel_e_first_answer"] = text
    await _send(update, context, session_id, "E_followup", followup)
    return E_FOLLOWUP


async def e_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_e_followup", conv_handler=conv_handler, state=E_FOLLOWUP,
        process=lambda text: _process_e_followup(update, context, text),
    )


async def _process_e_followup(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    await _log_turn(session_id, update.effective_user, "E_followup", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="E_followup", text_override=text
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return E_FOLLOWUP
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    await _bump_messages(session_id)
    first_answer = context.user_data.get("rel_e_first_answer", "")
    db.update_relationship_session(session_id, e_summary=f"{first_answer}\n{text}")

    await _check_exit_intent(context, session_id, "E_followup", text)

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
        await _log_session(update, context, session_id)
    await show_menu(update, context)
    await hostility.maybe_send_self_harm_note(update, context)
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
        D5_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, d5_question)],
        D5_HUNDRED_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, d5_hundred_followup)],
        E_DISCOMFORT_AFTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_discomfort_after)],
        E_SUMMARY_CONFIRM: [CallbackQueryHandler(e_summary_confirm, pattern="^e_confirm:")],
        E_SUMMARY_CORRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_summary_correction)],
        E_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_summary)],
        E_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_followup)],
        EXIT_INTENT_CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, exit_intent_clarify)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CommandHandler("start", cancel),
        CallbackQueryHandler(cancel, pattern="^menu:back$"),
    ],
    name="relationships_conversation",
    persistent=True,
    allow_reentry=True,
)
