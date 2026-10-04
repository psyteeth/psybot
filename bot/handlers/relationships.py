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

from bot import analytics, db, debounce, dialogue_matrix, hostility, limits, llm
from bot.config import (
    ADMIN_USERNAME,
    AB_TESTING_ENABLED,
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

# Новые состояния (D5_QUESTION, D5_HUNDRED_FOLLOWUP, EXIT_INTENT_CLARIFY, D7_SELF, D7_FRIEND)
# дописаны В КОНЕЦ, а не вставлены по смыслу между старыми — persistent=True хранит состояние как
# целое число на диске (PicklePersistence), и перенумерация уже существующих состояний сломала бы
# разборы, начатые до деплоя этого изменения. POST_FLOW (ТЗ 30.09, «Стало легче?»/CTA-меню) убран
# из conv_handler.states по ТЗ 01.10.2026 (п.A1/A2), но имя оставлено занятым по той же причине —
# не сдвигать номера состояний после него.
(
    A_EVENT, A_PRIOR_EVENT, A_OTHER_PERSON,
    B_NARRATIVE, B_CONFIRM, C_CONSEQUENCE, C_FEELING, C_DISCOMFORT,
    D_CONFIRM, D_QUESTION,
    E_DISCOMFORT_AFTER, E_SUMMARY_CONFIRM, E_SUMMARY_CORRECTION, E_SUMMARY, E_FOLLOWUP,
    D5_QUESTION, D5_HUNDRED_FOLLOWUP, EXIT_INTENT_CLARIFY,
    INTRO_WAIT, POST_FLOW,
    D7_SELF, D7_FRIEND,
) = range(22)

Q_A = (
    "Опиши событие или поведение другого человека, от которого тебе дискомфортно: "
    "триггерит, бесит, раздражает, обламывает, достаёт."
)
Q_B = "Как бы тебе хотелось, чтобы было иначе — что другой человек должен был сделать по-другому?"
# ТЗ-доп. №7, п.2: C — только реакция (чувство спрашивает следующий шаг C_feeling).
Q_C = "Как ты реагируешь, когда так происходит?"
WRAP_TEXT = "Мы прошли уже много — давай подведём предварительный итог."
# ТЗ-доп. №7, п.1/п.3/п.7: финал — ОДНО сообщение: отражение (или «ответ не находится») + ссылки + CTA.
# «Ответ не находится» — только если на E/E_followup нет сдвига (llm.classify_e_has_shift).
E_NO_INSIGHT_TEXT = "Похоже, сейчас ответ не находится — и это тоже нормально, не обязательно сразу."
# B7 (ТЗ 01.10.2026): отдельный текст для исхода «хочу иначе, но не умею» — человек уже сам заметил
# желание реагировать по-другому (живой пример: «хочется по-другому, но я не умею», сессия 05:16:46).
E_WANTS_CHANGE_TEXT = (
    "Похоже, ты уже чувствуешь, что хочется реагировать иначе — а готовой формулировки для этого "
    "пока нет, и это тоже нормально, так и бывает."
)
DECLINE_D_TEXT = "Ок, как скажешь. Если захочешь вернуться — я здесь."

PRIOR_EVENT_QUESTION = "А что было до этого — может, чуть раньше что-то уже задело?"
OTHER_PERSON_QUESTION = "А кто это для тебя?"
DISCOMFORT_BEFORE_Q = "Насколько тебе сейчас дискомфортно от этой ситуации, от 0 до 10?"
DISCOMFORT_AFTER_Q = "И ещё раз, от 0 до 10: насколько тебе дискомфортно от этой ситуации сейчас?"
DISCOMFORT_RETRY_TEXT = "Напиши, пожалуйста, просто число от 0 до 10."
# B1 (ТЗ 01.10.2026): живой баг — discomfort_before=0 при явно непустом, беспокоящем событии почти
# всегда оказывался опечаткой/недопониманием вопроса, а не реальным «ноль». Один раз переспрашиваем.
DISCOMFORT_ZERO_CONFIRM_TEXT = "0 — совсем не беспокоит, так?"


def _parse_discomfort(text: str) -> int | None:
    # B1 (ТЗ 01.10.2026): раньше было re.fullmatch — принимало ТОЛЬКО голое число, а сообщение с
    # пояснением («стало дискомфортнее, 4») отклонялось целиком («напиши просто число»), и ровно
    # эти поясняющие слова (нужные для проверки противоречия в E_discomfort_after) терялись в
    # отклонённом сообщении. Теперь число ищем внутри текста — с пояснением оно тоже принимается, а
    # сырой текст с пояснением сохраняется (см. вызовы discomfort_*_raw).
    m = re.search(r"(?<!\d)(10|[0-9])(?!\d)", text)
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
# 04.10: прежний вопрос («ты начинаешь позволять себе вести себя так же, как он — Критиковать… .
# — по отношению к людям?») был непонятен без объяснения идеи (живой отзыв: «не понимаю второй
# вариант»), фраза поведения вставлялась сырой (заглавная, точка посреди), «он» был вписан жёстко.
EXIT_INTENT_STEP1_TEMPLATE = (
    "Стоп, это важный момент.\n\n"
    "Бывает два варианта:\n"
    "1) «Я лучше, {he} {bad} — и с такими мне не по пути». Тогда ты просто уходишь от таких людей.\n"
    "2) Тебя так задевает {his} поведение — {behavior}, — потому что ты себе такое строго "
    "запрещаешь. И, может, тебе пора разрешить себе немного того же: {permission}.\n\n"
    "Как думаешь, что из этого больше про тебя?"
)
EXIT_INTENT_EXPLAIN_TEMPLATE = (
    "Скажу проще.\n\n"
    "1) Ты считаешь, что {he} {bad}, и просто перестаёшь с такими общаться.\n"
    "2) То, что бесит в другом, часто то, что запрещаешь себе. {his_cap} поведение — {behavior} — "
    "цепляет так сильно, потому что тебе так нельзя. А в мягкой дозе это бывает полезно: "
    "{permission}.\n\n"
    "Что ближе — 1 или 2?"
)
AVOIDANCE_TEXT = (
    "Уйти — самый простой путь, но такие люди будут встречаться снова, и снова будет задевать. "
    "Стоит подумать, что именно в {him} так цепляет."
)
INTEGRATION_TEXT = (
    "То, что бесит в другом, часто то, что ты себе запрещаешь. Если начинаешь позволять себе "
    "немного этого же — это может быть началом чего-то нового."
)
# ответ номером варианта классификатору без вопроса не понять («2» он принимал за избегание)
_CHOICE_RE = re.compile(r"^\s*(?:(1|один|первое|первый|первый вариант)|(2|два|второе|второй|второй вариант))\s*[.!)]?\s*$", re.IGNORECASE)

_PRONOUNS = {
    "m": {"he": "он", "bad": "плохой", "his": "его", "him": "нём"},
    "f": {"he": "она", "bad": "плохая", "his": "её", "him": "ней"},
    "unknown": {"he": "этот человек", "bad": "плохой", "his": "его", "him": "этом человеке"},
}


def _pronouns(row) -> dict:
    forms = dict(_PRONOUNS.get(row["other_person_gender"] or "unknown", _PRONOUNS["unknown"]))
    forms["his_cap"] = forms["his"][:1].upper() + forms["his"][1:]
    return forms




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
SUMMARY_CONFIRM_PREFIX = (
    "Давай я теперь всё срезюмирую, как понял, а ты скажешь, правильно я тебя понял или подкорректируешь.\n\n"
)
PARANOID_BOT_TEMPLATE = (
    "Прости, я совсем забыл сказать, что я бот-параноик, и сейчас я ощущаю космический посыл "
    "передать тебе следующую информацию из космоса: попахивает тем, что тебе хочется {hidden_need}, "
    "голос передаёт тебе: {punchline}"
)
FINAL_SHIFT_TEMPLATE = "Похоже, у тебя появилось: {quote}."
FINAL_NEXT_TIME_TEMPLATE = "В следующий раз в подобной ситуации ты можешь {insertion}"
FINAL_LINKS_TEXT = (
    "Меняй своё мышление, а не других людей.\n"
    "Психостоматология №1\n\n"
    f'<a href="{ROADMAP_URL}">Зайти в работу</a>\n'
    "Пройти серию тестов и получить персональный портрет коммуникации - "
    f'<a href="{TESTS_URL}">здесь</a>'
)
# п.7: без «консультации» (такой кнопки нет) и без «он» про администратора
FINAL_CTA_TEXT = (
    "Если хочется разобраться глубже — приходи на эфир, поговорим. "
    f"В любом случае пиши {ADMIN_USERNAME}, администратор Мария сориентирует."
)

D_FIELDS = {
    1: "d1_logical", 2: "d2_empirical", 3: "d3_pragmatic", 4: "d4_hedonistic",
    5: "d5_catastrophe_scale", 6: "d6_historical", 8: "d8_semantic",
    # 7 (двойной стандарт) разбит на D7_SELF/D7_FRIEND — свои обработчики, свои поля
    # (d7_self/d7_friend), не через этот generic-словарь (ТЗ 01.10.2026, п.A3).
}


def _other(row) -> str:
    """Кто другой и в каком роде о нём говорить — для всех генераций (вопросы D, резюме, E)."""
    return llm.other_person_note(row["other_person_label"], row["other_person_gender"])


def _shift_value(row):
    before, after = row["discomfort_before"], row["discomfort_after"]
    if before is None or after is None:
        return ""
    if row["discomfort_doubt"]:
        # B1 (ТЗ 01.10.2026): слова в ответе противоречат направлению изменения числа — не
        # считаем сдвиг как достоверный, чтобы не портить статистику ошибкой ввода.
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
        _shift_value(row),
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
        # ТЗ 01.10.2026: новые поля дописаны в конец, старые позиции (d7_double_standard,
        # reflection_before_e выше) не трогаем — не сдвигаем уже выгруженные колонки в Sheets.
        row["d7_self"] or "",
        row["d7_friend"] or "",
        "да" if row["discomfort_doubt"] else "нет",
        row["discomfort_before_raw"] or "",
        row["discomfort_after_raw"] or "",
        row["e_outcome"] or "",
        row["other_person_label"] or "",
        row["other_person_gender"] or "",
        row["d3b_helps"] or "",
        row["d4b_cost"] or "",
        row["c_feeling"] or "",
        "" if row["e_has_shift"] is None else ("да" if row["e_has_shift"] else "нет"),
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


INTRO_SCREEN_TEXT = (
    "Разберём одну ситуацию, где тебя что-то бесит или задевает в поведении другого человека — "
    "по методу психостоматологии, шаг за шагом.\n\n"
    "Обычно это 5-10 минут и помогает увидеть, что стоит за раздражением на самом деле."
)


async def _start_session(
    update: Update, context: ContextTypes.DEFAULT_TYPE, via_query: bool, skip_ab: bool = False
) -> int:
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

    # Аналитика воронки рекламы (ТЗ 30.09): до создания сессии — считались бы «начатые» разборы
    # людьми, которые просто повторно тапнули кнопку.
    if not skip_ab and db.count_relationship_sessions(user.id) > 0:
        await analytics.log(context, user.id, "return_session")

    if AB_TESTING_ENABLED and not skip_ab:
        variant = db.get_or_assign_ab_variant(user.id)
        if variant == "intro":
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Разобрать", callback_data="rel_intro_start")]]
            )
            await send(INTRO_SCREEN_TEXT, reply_markup=keyboard)
            return INTRO_WAIT

    session_id = db.create_relationship_session(user.id, user.username)
    context.user_data["rel_session_id"] = session_id
    _reset_dozhim_state(context)
    hostility.reset_session(context)
    # Живой кейс: на вопросе A нет способа выйти кроме /start — несколько раз подряд человек
    # открывал разбор, не отвечал и уходил (см. SeliverstovaMarina, 3 пустых cancelled-сессии
    # подряд). Кнопка «В меню» даёт лёгкий путь назад без команды.
    await send(Q_A, reply_markup=back_to_menu_keyboard())
    await _log_turn(session_id, user, "A", "бот", Q_A)
    variant = db.get_or_assign_ab_variant(user.id) if AB_TESTING_ENABLED else "direct"
    await analytics.log(context, user.id, "flow_started", {"ab_variant": variant})
    return A_EVENT


async def rel_intro_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Тап по кнопке «Разобрать» на интро-экране A/B-варианта (ТЗ 30.09)."""
    query = update.callback_query
    await query.answer()
    return await _start_session(update, context, via_query=True, skip_ab=True)


async def rel_intro_text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Если на интро-экране человек написал текст вместо тапа по кнопке — не бросаем в тупик,
    просто стартуем разбор так же, как по кнопке."""
    return await _start_session(update, context, via_query=False, skip_ab=True)


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
    "Слышишь, что-то не то происходит. Где-то ты юлишь. Когда сможешь конкретно сказать, чем "
    "тебя раздражает другой, — начинай заново, и я тебе помогу.\n\n"
    f"Если считаешь, что это ошибка, напиши администратору {ADMIN_USERNAME} — она решит этот вопрос."
)
# 04.10: тому, кто честно пытается объяснить (llm.classify_dozhim_effort = trying), — мягкие тексты;
# жёсткие DOZHIM_ATTEMPT2_TEXT/DOZHIM_EVASIVE_CLOSE_TEXT остаются только для тех, кто уходит от ответа.
DOZHIM_ATTEMPT2_SOFT_TEXT = {
    "A": (
        "Давай ещё чуть конкретнее: что именно он делает или говорит такого, от чего тебя бесит?\n"
        "Продолжи: «Меня в нём бесит то, что он…» (или «она…»)."
    ),
    "B": (
        "Давай ещё чуть конкретнее: что именно он должен был бы сделать по-другому?\n"
        "Продолжи: «Он должен был бы…» (или «она должна была бы…»)."
    ),
}
DOZHIM_SOFT_CLOSE_TEXT = (
    "Похоже, пока сложно сформулировать, что именно задевает в поведении другого — так бывает. "
    "Вспомни конкретный недавний случай: что он или она сделал(а) или сказал(а) — и начни разбор "
    "заново, я помогу.\n\n"
    f"Если считаешь, что это ошибка, напиши администратору {ADMIN_USERNAME} — она решит этот вопрос."
)
DOZHIM_SELF_CLOSE_TEXT = (
    SELF_REFUSAL_TEXT + "\n\n"
    f"Если считаешь, что это ошибка, напиши администратору {ADMIN_USERNAME} — она решит этот вопрос."
)


def _reset_dozhim_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for step in ("a", "b"):
        for suffix in ("attempts", "answers", "prompt"):
            context.user_data.pop(f"rel_dozhim_{step}_{suffix}", None)


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

    answers_key = f"rel_dozhim_{step.lower()}_answers"
    prompt_key = f"rel_dozhim_{step.lower()}_prompt"
    if accepted:
        outcome = "принят с первого раза" if attempts == 0 else "переформулировал"
        db.update_relationship_session(
            session_id, **{f"attempts_{step.lower()}": attempts, "dozhim_outcome": outcome}
        )
        # раньше счётчик не сбрасывался — в следующем разборе первый же промах сразу получал 2-ю ступень
        for key in (attempts_key, answers_key, prompt_key):
            context.user_data.pop(key, None)
        return None

    previous = context.user_data.get(answers_key, [])
    effort = await llm.classify_dozhim_effort(step, previous, text) if attempts >= 1 else "trying"
    context.user_data[answers_key] = previous + [text]
    attempts += 1
    context.user_data[attempts_key] = attempts

    if attempts >= 3:
        is_self = classification["target"] == "self"
        if is_self:
            outcome, close_text = "отказ_самообвинение", DOZHIM_SELF_CLOSE_TEXT
        elif effort == "evasive":
            outcome, close_text = "отказ_юлит", DOZHIM_EVASIVE_CLOSE_TEXT
        else:
            outcome, close_text = "отказ_не_сформулировал", DOZHIM_SOFT_CLOSE_TEXT
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
        _reset_dozhim_state(context)
        return ConversationHandler.END

    if attempts == 1:
        prompt = DOZHIM_ATTEMPT1_TEXT[step]
    else:
        prompt = DOZHIM_ATTEMPT2_TEXT[step] if effort == "evasive" else DOZHIM_ATTEMPT2_SOFT_TEXT[step]
    context.user_data[prompt_key] = prompt
    await _send(update, context, session_id, f"проверка_{step}", prompt, msg_type="уточнение")
    return A_EVENT if step == "A" else B_NARRATIVE


async def _ask_e_question(context: ContextTypes.DEFAULT_TYPE, session_id: int) -> str:
    row = db.get_relationship_session(session_id)
    question = await llm.generate_e_question(row["a_event"], row["b_narrative_confirmed"], other=_other(row))
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


async def _start_e_finale(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> int:
    """Общая точка входа в финал разбора — что при нормальном прохождении всех D1-D8,
    что при форсированном сворачивании по лимиту 40 сообщений (см. _maybe_wrap_to_summary).
    Шаг E_reflection (цитаты «на одном вопросе ты сказал...») убран по ТЗ 01.10.2026, п.A4 —
    пара цитат выбиралась нестабильно и без рамки, иногда ухудшала состояние человека. Сразу
    переспрашивает дискомфорт (2.1), и только потом идёт Voss-резюме/подтверждение."""
    await analytics.log(context, update.effective_user.id, "step_D_done")
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
    # Баг из живой сессии: пока идёт лестница дожима (ТЗ-доп. №6), ответ пользователя на
    # реплику дожима возвращает то же состояние A_EVENT — и без этой проверки ответ логировался
    # бы под шагом "A" вместо "проверка_A", а bot_question для hostility.precheck оставался бы
    # исходным Q_A вместо реального последнего вопроса бота.
    attempts_a = context.user_data.get("rel_dozhim_a_attempts", 0)
    in_dozhim_retry = attempts_a > 0
    log_step = "проверка_A" if in_dozhim_retry else "A"
    bot_question = context.user_data.get("rel_dozhim_a_prompt", DOZHIM_ATTEMPT1_TEXT["A"]) if in_dozhim_retry else Q_A
    await _log_turn(session_id, update.effective_user, log_step, "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step=log_step, bot_question=bot_question, text_override=text
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

    await analytics.log(context, update.effective_user.id, "step_A_done", {"len": len(text)})

    if ENABLE_PRIOR_EVENT_QUESTION:
        await _send(update, context, session_id, "A_prior", PRIOR_EVENT_QUESTION)
        return A_PRIOR_EVENT

    return await _classify_or_ask_other_person(update, context, session_id)


async def _classify_or_ask_other_person(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int
) -> int:
    row = db.get_relationship_session(session_id)
    other = await llm.classify_other_person_full(row["a_event"])
    if other["category"]:
        db.update_relationship_session(
            session_id, other_person=other["category"],
            other_person_label=other["label"], other_person_gender=other["gender"],
        )
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
    other = await llm.classify_other_person_full(text)
    db.update_relationship_session(
        session_id, other_person=other["category"] or "другое",
        other_person_label=other["label"] or text.strip()[:60], other_person_gender=other["gender"],
    )
    await _send(update, context, session_id, "B", Q_B)
    return B_NARRATIVE


async def b_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_b", conv_handler=conv_handler, state=B_NARRATIVE,
        process=lambda text: _process_b_narrative(update, context, text),
    )


async def _process_b_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["rel_session_id"]
    # см. тот же фикс в _process_a_event — во время лестницы дожима это состояние (B_NARRATIVE)
    # тоже переиспользуется, ответ на дожим иначе логировался бы под "B", а не "проверка_B".
    attempts_b = context.user_data.get("rel_dozhim_b_attempts", 0)
    in_dozhim_retry = attempts_b > 0
    log_step = "проверка_B" if in_dozhim_retry else "B"
    bot_question = context.user_data.get("rel_dozhim_b_prompt", DOZHIM_ATTEMPT1_TEXT["B"]) if in_dozhim_retry else Q_B
    await _log_turn(session_id, update.effective_user, log_step, "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step=log_step, bot_question=bot_question, text_override=text
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
    reformulated = await llm.reformulate_narrative(row["a_event"], text, other=_other(row))
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
            row["a_event"], context.user_data["rel_raw_narrative"], correction=text, other=_other(row)
        )
        context.user_data["rel_pending_narrative"] = reformulated
        await _send(update, context, session_id, "B_confirm", reformulated)
        return B_CONFIRM

    confirmed = context.user_data.pop("rel_pending_narrative")
    db.update_relationship_session(session_id, b_narrative_confirmed=confirmed)
    await analytics.log(context, update.effective_user.id, "step_B_done", {"len": len(confirmed)})

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

    c_question = Q_C
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
        bot_question=Q_C,
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
    if await _maybe_meta_reask(update, context, session_id, "C", Q_C, text):
        return C_CONSEQUENCE
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
    if await _maybe_meta_reask(update, context, session_id, "C_feeling", feeling_question, text):
        return C_FEELING
    row = db.get_relationship_session(session_id)
    combined_consequence = f"{row['c_consequence']}\nЧувство: {text}"
    db.update_relationship_session(session_id, c_consequence=combined_consequence, c_feeling=text)

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
    zero_pending = context.user_data.get("rel_c_discomfort_zero_pending", False)
    bot_question = DISCOMFORT_ZERO_CONFIRM_TEXT if zero_pending else DISCOMFORT_BEFORE_Q
    await _log_turn(session_id, update.effective_user, "C_discomfort", "человек", text)

    status = await hostility.precheck(
        update, context, branch="relationships", step="C_discomfort",
        bot_question=bot_question, text_override=text,
    )
    if status == "crisis":
        await _close_after_hostility(update, context, session_id, "crisis")
        return ConversationHandler.END
    if status == "hostile":
        return C_DISCOMFORT
    if status == "closed":
        await _close_after_hostility(update, context, session_id, "hostility_closed")
        return ConversationHandler.END

    if zero_pending:
        # B1: это ответ на «0 — совсем не беспокоит, так?», не новое число — исходный «0» уже
        # сохранён в raw_text ниже.
        context.user_data.pop("rel_c_discomfort_zero_pending", None)
        raw_text = context.user_data.pop("rel_c_discomfort_zero_raw", text)
        confirmed_zero = await llm.classify_yes_no(text)
        if not confirmed_zero:
            await _send(update, context, session_id, "C_discomfort", DISCOMFORT_BEFORE_Q)
            return C_DISCOMFORT
        value = 0
    else:
        value = _parse_discomfort(text)
        if value is None:
            await _send(update, context, session_id, "C_discomfort", DISCOMFORT_RETRY_TEXT)
            return C_DISCOMFORT
        if value == 0:
            context.user_data["rel_c_discomfort_zero_pending"] = True
            context.user_data["rel_c_discomfort_zero_raw"] = text
            await _send(update, context, session_id, "C_discomfort", DISCOMFORT_ZERO_CONFIRM_TEXT)
            return C_DISCOMFORT
        raw_text = text

    await _bump_messages(session_id)
    db.update_relationship_session(session_id, discomfort_before=value, discomfort_before_raw=raw_text)
    await analytics.log(context, update.effective_user.id, "step_C_done")

    row = db.get_relationship_session(session_id)
    advice = await llm.generate_i_would_advice(
        row["a_event"], row["b_narrative_confirmed"], row["c_consequence"], other=_other(row)
    )
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

    # Живой баг: раньше тут был общий classify_yes_no — любой неявный ответ (вопрос, рассуждение)
    # читался как отказ и рвал весь разбор. Теперь разбор прерывается только на явном «не хочу
    # продолжать»/«останови»/«начни заново» (см. llm.CONTINUE_CONSENT_SYSTEM).
    agreed = await llm.classify_continue_consent(text)
    if not agreed:
        db.finish_relationship_session(session_id, exit_step="declined_D")
        await _log_session(update, context, session_id)
        await _send(update, context, session_id, "D_confirm", DECLINE_D_TEXT, reply_markup=back_to_menu_keyboard())
        await hostility.maybe_send_self_harm_note(update, context)
        context.user_data.pop("rel_session_id", None)
        return ConversationHandler.END

    for key in ("rel_d_step", "rel_d_cur_q", "rel_d_prev_q", "rel_d_prev_step", "rel_d_reasked", "rel_d8_reasked"):
        context.user_data.pop(key, None)
    return await _ask_d(update, context, session_id, "1")


# B5 (ТЗ 01.10.2026): D8 — семантическая переформулировка «я бы предпочёл, чтобы...» — должна быть
# про самого клиента. Живой баг (сессия 18:49:17): ответ «я бы сказала ей поговорить с партнёром»
# был про третье лицо (подругу из D7), а не про себя. Переспрашиваем один раз, не больше.
D8_THIRD_PERSON_RETRY_TEXT = (
    "Уточню: а что меняется в твоих собственных ощущениях, когда говоришь «мне бы хотелось» вместо "
    "«должен(на)»?"
)
FIRST_PERSON_RE = re.compile(r"\b(я|мне|меня|мной|мною)\b", re.IGNORECASE)


async def _maybe_meta_reask(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, step: str, question: str, text: str
) -> bool:
    """ТЗ-доп. №7, п.2: «задавай по одному» / «не поняла вопрос» — не выпад и не ответ. Коротко
    соглашаемся и задаём текущий вопрос заново, проще. True — переспросили, шаг не двигаем."""
    if not question:
        return False
    meta = await llm.classify_meta_request(text, question)
    if meta == "none":
        return False
    ack = D_ONE_AT_A_TIME_ACK if meta == "one_at_a_time" else D_NOT_UNDERSTOOD_ACK
    simpler = await llm.simplify_question(question)
    await _send(update, context, session_id, step, f"{ack} {simpler}", msg_type="уточнение")
    return True


# --- Шаги D (ТЗ-доп. №7, п.2/п.4) ---
# Один вопрос в сообщении: D2 → (2b — доказательства, только если ответ «да»), D3 → 3b, D4 → 4b.
# D7 себе/другу теперь тоже идут через D_QUESTION (состояния D7_SELF/D7_FRIEND оставлены только для
# разборов, начатых до деплоя). Ключ шага хранится в user_data["rel_d_step"].
D_STEP_FIELDS = {
    "1": "d1_logical", "2": "d2_empirical", "2b": "d2_empirical", "3": "d3_pragmatic", "3b": "d3b_helps",
    "4": "d4_hedonistic", "4b": "d4b_cost", "6": "d6_historical", "7a": "d7_self", "7b": "d7_friend",
    "8": "d8_semantic",
}
D_STEP_LOG = {
    "1": "D1", "2": "D2", "2b": "D2b", "3": "D3", "3b": "D3b", "4": "D4", "4b": "D4b",
    "6": "D6", "7a": "D7_self", "7b": "D7_friend", "8": "D8",
}
D_NEXT = {"1": "2", "2b": "3", "3": "3b", "3b": "4", "4": "4b", "4b": "5", "6": "7a", "7a": "7b", "7b": "8"}
D_ONE_AT_A_TIME_ACK = "Понял, по одному."
D_NOT_UNDERSTOOD_ACK = "Скажу проще."
D_PREVIOUS_ANSWER_ACK = "Похоже, это к прошлому вопросу — записал 👌 А теперь:"


def _d_base_key(step: str) -> int | str:
    return int(step) if step.isdigit() else step


async def _ask_d(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, step: str) -> int:
    """Задать D-шаг step (кроме 5 — у него свой фиксированный текст и состояние)."""
    row = db.get_relationship_session(session_id)
    if step == "5":
        context.user_data["rel_d_index"] = 5
        question = D5_QUESTION_TEMPLATE.format(situation=row["a_event"])
        await _send(update, context, session_id, "D5", question)
        return D5_QUESTION
    question = await llm.adapt_dispute_question(_d_base_key(step), row["b_narrative_confirmed"], other=_other(row))
    context.user_data["rel_d_prev_q"] = context.user_data.get("rel_d_cur_q", "")
    context.user_data["rel_d_prev_step"] = context.user_data.get("rel_d_step")
    context.user_data["rel_d_step"] = step
    context.user_data["rel_d_cur_q"] = question
    context.user_data["rel_d_index"] = int(step[0])
    context.user_data.pop("rel_d_reasked", None)
    await _send(update, context, session_id, D_STEP_LOG[step], question)
    return D_QUESTION


def _store_d_answer(session_id: int, step: str, text: str, append: bool = False) -> None:
    field = D_STEP_FIELDS[step]
    current = db.get_relationship_session(session_id)[field] or ""
    if step == "2b":
        value = f"{current} / доказательства: {text}" if current else text
    elif append and current:
        value = f"{current} / {text}"
    else:
        value = text
    db.update_relationship_session(session_id, **{field: value})


async def d_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_d", conv_handler=conv_handler, state=D_QUESTION,
        process=lambda text: _process_d_answer(update, context, text),
    )


async def _process_d_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, forced_step: str | None = None
) -> int:
    session_id = context.user_data["rel_session_id"]
    # разборы, начатые до деплоя ТЗ-доп. №7, знают только rel_d_index
    step = forced_step or context.user_data.get("rel_d_step") or str(context.user_data.get("rel_d_index", 1))
    if step not in D_STEP_FIELDS:
        step = "8" if step.startswith("8") else step[0]
    await _log_turn(session_id, update.effective_user, D_STEP_LOG[step], "человек", text)

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
    current_q = context.user_data.get("rel_d_cur_q", "")
    previous_q = context.user_data.get("rel_d_prev_q", "")
    previous_step = context.user_data.get("rel_d_prev_step")
    reasked = context.user_data.get("rel_d_reasked", False)

    if current_q and not (step == "8" and context.user_data.get("rel_d8_reasked")):
        check = await llm.check_d_answer(previous_q, current_q, text)
        if check["meta"] != "none":
            # «задавай по одному» / «не поняла вопрос» — не выпад и не ответ: повторить проще, шаг не пропускать
            ack = D_ONE_AT_A_TIME_ACK if check["meta"] == "one_at_a_time" else D_NOT_UNDERSTOOD_ACK
            simpler = await llm.simplify_question(current_q)
            await _send(update, context, session_id, D_STEP_LOG[step], f"{ack} {simpler}", msg_type="уточнение")
            return D_QUESTION
        if not check["answers_current"] and check["answers_previous"] and previous_step in D_STEP_FIELDS:
            _store_d_answer(session_id, previous_step, text, append=True)
            await _send(
                update, context, session_id, D_STEP_LOG[step], f"{D_PREVIOUS_ANSWER_ACK} {current_q}",
                msg_type="уточнение",
            )
            return D_QUESTION
        if not check["answers_current"] and not reasked:
            context.user_data["rel_d_reasked"] = True
            simpler = await llm.simplify_question(current_q)
            await _send(update, context, session_id, D_STEP_LOG[step], simpler, msg_type="уточнение")
            return D_QUESTION

    if step == "8" and context.user_data.get("rel_d8_reasked"):
        # ответ на уточнение D8 не затирает первый ответ — в резюме идут оба
        first = db.get_relationship_session(session_id)[D_FIELDS[8]] or ""
        db.update_relationship_session(session_id, **{D_FIELDS[8]: f"{first} / на уточнение: {text}"})
    else:
        _store_d_answer(session_id, step, text, append=reasked)

    await _check_exit_intent(context, session_id, D_STEP_LOG[step], text)

    wrap_state = await _maybe_wrap_to_summary(update, context, session_id, count)
    if wrap_state is not None:
        return wrap_state

    if step == "8":
        if (
            not context.user_data.get("rel_d8_reasked")
            and not FIRST_PERSON_RE.search(text)
            and await llm.is_d8_answer_about_other(text)
        ):
            context.user_data["rel_d8_reasked"] = True
            await _send(update, context, session_id, "D8_проверка", D8_THIRD_PERSON_RETRY_TEXT)
            return D_QUESTION
        context.user_data.pop("rel_d8_reasked", None)
        return await _start_e_finale(update, context, session_id)

    if step == "2":
        next_step = "2b" if await llm.classify_yes_no(text) else "3"
    else:
        next_step = D_NEXT[step]
    return await _ask_d(update, context, session_id, next_step)


# Состояния D7_SELF/D7_FRIEND — только для разборов, начатых до ТЗ-доп. №7.
async def d7_self(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_d7_self", conv_handler=conv_handler, state=D7_SELF,
        process=lambda text: _process_d_answer(update, context, text, forced_step="7a"),
    )


async def d7_friend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="rel_d7_friend", conv_handler=conv_handler, state=D7_FRIEND,
        process=lambda text: _process_d_answer(update, context, text, forced_step="7b"),
    )


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

    # B2 (ТЗ 01.10.2026): живой баг — повторное/гоночное сообщение в этот же шаг (клиент успел
    # отправить D5 дважды до того, как бот обработал первый ответ) приводило к повторной отправке
    # D6. Если D5 уже отвечен — это не новый ответ, а правка прежнего: обновляем значение (храня
    # исходное в d5_original) и НЕ переспрашиваем/не продвигаем разбор второй раз.
    existing = db.get_relationship_session(session_id)
    if existing["d5_catastrophe_scale"] is not None:
        update_fields = {"d5_catastrophe_scale": value, "d5_comment": text}
        if existing["d5_original"] is None and int(existing["d5_catastrophe_scale"]) != value:
            update_fields["d5_original"] = int(existing["d5_catastrophe_scale"])
        db.update_relationship_session(session_id, **update_fields)
        return D_QUESTION

    db.update_relationship_session(session_id, d5_catastrophe_scale=value, d5_comment=text)

    if value == 100:
        await _send(update, context, session_id, "D5_hundred", D5_HUNDRED_FOLLOWUP_Q)
        return D5_HUNDRED_FOLLOWUP

    return await _advance_past_d5(update, context, session_id)


async def _advance_past_d5(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> int:
    return await _ask_d(update, context, session_id, "6")


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
    row = db.get_relationship_session(session_id)
    # B1 (ТЗ 01.10.2026): слова ответа могут противоречить направлению изменения числа
    # («стало дискомфортнее, 4» при падении с 8) — живой случай, ошибка ввода. Флаг «сомнение»
    # исключает такой сдвиг из статистики (см. _shift_value), сам ответ всё равно сохраняется.
    contradicts = await llm.classify_discomfort_contradiction(row["discomfort_before"], value, text)
    db.update_relationship_session(
        session_id, discomfort_after=value, discomfort_after_raw=text,
        discomfort_doubt=1 if contradicts else 0,
    )
    return await _send_summary_confirm(update, context, session_id)




async def _complete_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> int:
    """Общая точка истинного завершения разбора — вызывается и из немедленного конца _finish_e, и
    из конца _process_exit_intent_clarify (когда «избегание или интеграция» продлевает разбор на
    один обмен). Логирует E/flow_completed, затем текстовое приглашение написать админу (ТЗ
    01.10.2026, п.A1/A2 — «Стало легче?» и кнопки «Консультация»/«Интенсив»/«Канал» убраны, они не
    работали или путали; кнопки здесь больше нет вообще — «В меню» уже есть на самом сообщении
    E_finish прямо перед этим, дублировать её тут не нужно)."""
    user = update.effective_user
    await analytics.log(context, user.id, "step_E_done")
    await analytics.log(context, user.id, "flow_completed")
    await _log_session(update, context, session_id)
    await hostility.maybe_send_self_harm_note(update, context)

    context.user_data.pop("rel_session_id", None)
    context.user_data.pop("rel_pending_final", None)
    context.user_data.pop("rel_e_first_answer", None)
    context.user_data.pop("rel_summary", None)
    return ConversationHandler.END


def _build_final_text(outcome: str, quote: str | None, insertion: str | None) -> str:
    if outcome == "insight":
        parts = [FINAL_SHIFT_TEMPLATE.format(quote=html.escape(quote or ""))]
        if insertion:
            parts.append(FINAL_NEXT_TIME_TEMPLATE.format(insertion=html.escape(insertion)))
    elif outcome == "wants_change":
        parts = [html.escape(E_WANTS_CHANGE_TEXT)]
    else:
        parts = [html.escape(E_NO_INSIGHT_TEXT)]
    return "\n\n".join(parts + [FINAL_LINKS_TEXT, html.escape(FINAL_CTA_TEXT)])


async def _send_final(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, text: str) -> None:
    await _send(
        update, context, session_id, "E_finish", text,
        reply_markup=back_to_menu_keyboard(), parse_mode=ParseMode.HTML,
    )


async def _finish_e(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, outcome: str,
    quote: str | None = None, insertion: str | None = None,
) -> int:
    """Исходы (B7, ТЗ 01.10.2026 + ТЗ-доп. №7, п.1): insight — есть сдвиг (отражаем его словами
    человека, плюс новое поведение на будущее, если оно названо); wants_change — хочется иначе, но не
    сформулировано; no_change — пусто по смыслу. Порядок (п.3): проверка «избегание/интеграция»,
    если сработала, — ДО финала, а финал — одно сообщение со ссылками и CTA."""
    db.finish_relationship_session(session_id, exit_step="E")
    db.update_relationship_session(session_id, e_outcome=outcome, e_has_shift=1 if outcome == "insight" else 0)
    final_text = _build_final_text(outcome, quote, insertion)

    # «Избегание или интеграция» (ТЗ-доп. №5, раздел 2) — если хоть раз сработало на C-D8/E,
    # сначала ещё один обмен, финал уходит вместе с ответом на него. Лог в Sheets откладывается до
    # полного разрешения (иначе пришлось бы потом патчить уже отправленную строку).
    if context.user_data.get("exit_intent_flagged"):
        context.user_data["rel_pending_final"] = final_text
        row = db.get_relationship_session(session_id)
        behavior = await llm.extract_disowned_behavior(row["a_event"], row["b_narrative_confirmed"])
        context.user_data["exit_intent_behavior"] = behavior
        question = EXIT_INTENT_STEP1_TEMPLATE.format(**behavior, **_pronouns(row))
        await _send(update, context, session_id, "exit_intent_check", question)
        return EXIT_INTENT_CLARIFY

    await _send_final(update, context, session_id, final_text)
    return await _complete_flow(update, context, session_id)


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
    choice = _CHOICE_RE.match(text)
    digits = set(re.findall(r"(?<!\d)[12](?!\d)", text))
    if choice:
        verdict = "avoidance" if choice.group(1) else "integration"
    elif len(digits) == 1:  # «скорее 2», «наверное 1»
        verdict = "avoidance" if digits == {"1"} else "integration"
    else:
        verdict = await llm.classify_avoidance_integration(text)
    row = db.get_relationship_session(session_id)
    forms = _pronouns(row)
    behavior = context.user_data.get("exit_intent_behavior")
    if not isinstance(behavior, dict):  # разборы, начатые до 04.10, хранили строку
        behavior = {"behavior": behavior or "вести себя так же", "permission": "иногда позволять себе то же самое"}

    if verdict == "confused" and not context.user_data.get("exit_intent_explained"):
        # человек не понял вопрос — объясняем проще один раз и ждём ответа, а не закрываем разбор
        context.user_data["exit_intent_explained"] = True
        await _send(
            update, context, session_id, "exit_intent_check",
            EXIT_INTENT_EXPLAIN_TEMPLATE.format(**behavior, **forms), msg_type="уточнение",
        )
        return EXIT_INTENT_CLARIFY
    if verdict == "confused":
        verdict = "unclear"
    context.user_data.pop("exit_intent_explained", None)
    db.update_relationship_session(session_id, avoidance_or_integration=verdict, exit_intent_answer=text)

    parts = []
    if verdict in ("avoidance", "unclear"):
        parts.append(html.escape(AVOIDANCE_TEXT.format(**forms)))
    if verdict in ("integration", "unclear"):
        parts.append(html.escape(INTEGRATION_TEXT))
    final_text = context.user_data.pop("rel_pending_final", None) or _build_final_text("no_change", None, None)
    await _send_final(update, context, session_id, "\n\n".join(parts + [final_text]))

    context.user_data.pop("exit_intent_flagged", None)
    context.user_data.pop("exit_intent_behavior", None)
    return await _complete_flow(update, context, session_id)


async def _send_summary_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> int:
    row = db.get_relationship_session(session_id)
    d_answers = {f"D{i}": row[D_FIELDS[i]] for i in D_FIELDS}
    d_answers["D3b"] = row["d3b_helps"]
    d_answers["D4b"] = row["d4b_cost"]
    d_answers["D7_себе"] = row["d7_self"]
    d_answers["D7_другу"] = row["d7_friend"]
    summary = await llm.generate_session_summary(
        row["a_event"], row["b_narrative_confirmed"], row["c_consequence"], d_answers,
        discomfort_before=row["discomfort_before"], discomfort_after=row["discomfort_after"],
        other=_other(row),
    )
    context.user_data["rel_summary"] = summary
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да", callback_data="e_confirm:yes")],
            [InlineKeyboardButton("Хочу кое-то добавить/подправить", callback_data="e_confirm:add")],
        ]
    )
    await _send(
        update, context, session_id, "E_summary_confirm", SUMMARY_CONFIRM_PREFIX + summary + SUMMARY_CONFIRM_SUFFIX,
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
    e_question = await llm.generate_e_question(row["a_event"], narrative_with_correction, other=_other(row))
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
    if await _maybe_meta_reask(update, context, session_id, "E", e_question, text):
        return E_SUMMARY
    db.update_relationship_session(session_id, e_summary=text)

    await _check_exit_intent(context, session_id, "E", text)

    # ТЗ-доп. №7, п.1: «ответ не находится» — только если ответ пустой по смыслу. Есть сдвиг —
    # отражаем его словами человека (и новое поведение на будущее, если оно названо).
    shift = await llm.classify_e_has_shift(text)
    if shift["has_shift"]:
        return await _finish_e_with_shift(update, context, session_id, "", text)

    followup = await llm.ask_e_followup(db.get_relationship_session(session_id)["b_narrative_confirmed"], text)
    context.user_data["rel_e_first_answer"] = text
    context.user_data["rel_e_followup_q"] = followup
    await _send(update, context, session_id, "E_followup", followup)
    return E_FOLLOWUP


async def _finish_e_with_shift(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, first_answer: str, latest: str
) -> int:
    row = db.get_relationship_session(session_id)
    analysis = await llm.analyze_e_insight(row["a_event"], row["b_narrative_confirmed"], first_answer, latest)
    if first_answer:
        quote = await llm.quote_e_shift(first_answer, latest)
    else:
        quote = await llm.quote_e_shift(latest)
    insertion = analysis["reflection"] if analysis["has_insight"] else None
    return await _finish_e(update, context, session_id, "insight", quote=quote, insertion=insertion)


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
    if await _maybe_meta_reask(update, context, session_id, "E_followup", context.user_data.get("rel_e_followup_q", ""), text):
        return E_FOLLOWUP
    first_answer = context.user_data.get("rel_e_first_answer", "")
    db.update_relationship_session(session_id, e_summary=f"{first_answer}\n{text}")

    await _check_exit_intent(context, session_id, "E_followup", text)

    # ответы на E и на уточнение оцениваются вместе (ТЗ-доп. №7, п.1)
    shift = await llm.classify_e_has_shift(first_answer, text)
    if shift["has_shift"]:
        return await _finish_e_with_shift(update, context, session_id, first_answer, text)

    row = db.get_relationship_session(session_id)
    analysis = await llm.analyze_e_insight(row["a_event"], row["b_narrative_confirmed"], first_answer, text)
    outcome = "wants_change" if analysis.get("wants_change") else "no_change"
    return await _finish_e(update, context, session_id, outcome)


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
        D7_SELF: [MessageHandler(filters.TEXT & ~filters.COMMAND, d7_self)],
        D7_FRIEND: [MessageHandler(filters.TEXT & ~filters.COMMAND, d7_friend)],
        E_DISCOMFORT_AFTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_discomfort_after)],
        E_SUMMARY_CONFIRM: [CallbackQueryHandler(e_summary_confirm, pattern="^e_confirm:")],
        E_SUMMARY_CORRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_summary_correction)],
        E_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_summary)],
        E_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, e_followup)],
        EXIT_INTENT_CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, exit_intent_clarify)],
        INTRO_WAIT: [
            CallbackQueryHandler(rel_intro_start, pattern="^rel_intro_start$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, rel_intro_text_fallback),
        ],
        # POST_FLOW («Стало легче?»/CTA-меню, ТЗ 30.09) больше не используется — убран по
        # ТЗ 01.10.2026, п.A1/A2 (_complete_flow теперь сразу шлёт «Интенсив» и завершает разговор).
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
