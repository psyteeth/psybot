import html
import logging
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
    DIAGNOSTICS_POST_URL,
    ROADMAP_URL,
    TEETH_CHART_PATH,
    VALID_TEETH_NUMBERS,
    limit_exhausted_text,
)
from bot.keyboards import back_to_menu_keyboard
from bot.sheets import sheets_logger

logger = logging.getLogger(__name__)

ASK_TOOTH, CONFIRM_TOOTH, ASK_SCARY, ASK_FEELING = range(4)

ACUTE_PATTERNS = [
    r"сильн\w*\s+бол", r"остр\w*\s+бол", r"отёк", r"отек", r"температур",
    r"кровотеч", r"кровоточ", r"опухол",
]
ACUTE_RE = re.compile("|".join(ACUTE_PATTERNS), re.IGNORECASE)

ACUTE_TEXT = (
    "Во-первых, иди к стоматологу. А если хочешь разобраться, что за этим стоит, приходи в "
    f"психостоматологию на диагностику: пиши {ADMIN_USERNAME}."
)

# Живой кейс: человек проходит диагностику не за себя, а за другого (мать — за дочь). Бот должен
# отнестись к этому с подозрением, а не просто продолжать как ни в чём не бывало.
OTHER_PERSON_MINOR_TEXT = (
    "Смотрю, это про другого человека, а не про тебя — и, кажется, самому диагностику пока рано "
    "проходить. В таком случае это лучше делать через расстановку. Если понадобится помощь — "
    f"приходи на диагностику, пиши {ADMIN_USERNAME}."
)
OTHER_PERSON_ADULT_QUESTION = (
    "Смотрю, ты спрашиваешь за другого взрослого человека, а не за себя — почему сам(а) он/она не "
    "проходит диагностику?"
)
OTHER_PERSON_ANXIOUS_TEXT = (
    "Может не понравится это слышать, но когда за взрослого человека диагностику проходят вместо "
    "него, это попахивает провалом сепарации — не самое лучшее, что стоит вносить в отношения с "
    "близким человеком.\n\n"
    "По опыту специалистов психостоматологии: те, кто спрашивает так — из тревоги, вины или "
    "беспокойства за другого, а не из любопытства — чаще сами не живут свою жизнь и мешают жить "
    "другому."
)


# Живой кейс: люди отвечают на «Каким ты тогда себя чувствуешь?» ярлыком/фактом («не целый», «не
# справившийся»), а не прожитой эмоцией — бот должен докопаться до настоящего чувства, а не
# принимать первый попавшийся ярлык.
FEELING_DIG_MAX_ATTEMPTS = 2
FEELING_DIG_TEXT = (
    "Диагностика — штука не самая комфортная, и всё же это самый важный шаг на пути к тому, чтобы "
    'понять, откуда идёт проблема с зубами. Так что скажи честно: ты сказал(а) «{label}» — а что в '
    "этом самое страшное?"
)


def _other_person_note_text(subject_age: str, motivation: str) -> str:
    if subject_age == "minor":
        return OTHER_PERSON_MINOR_TEXT
    if motivation == "anxious":
        return f"{OTHER_PERSON_ADULT_QUESTION}\n\n{OTHER_PERSON_ANXIOUS_TEXT}"
    return OTHER_PERSON_ADULT_QUESTION

Q_TOOTH = "Какой зуб тебя беспокоит? Напиши номер по схеме."
Q_SCARY = (
    "В контексте того, что уже произошло с твоим зубом, что самое страшное для тебя может "
    "случиться с ним в итоге?"
)
Q_FEELING = "Каким ты тогда себя чувствуешь?"

QUADRANT_NAMES = {
    1: "верхний правый",
    2: "верхний левый",
    3: "нижний левый",
    4: "нижний правый",
}
POSITION_NAMES = {
    1: "центральный резец",
    2: "боковой резец",
    3: "клык",
    4: "первый премоляр",
    5: "второй премоляр",
    6: "первый моляр",
    7: "второй моляр",
    8: "зуб мудрости (третий моляр)",
}


def describe_tooth(number: int) -> str:
    quadrant, position = divmod(number, 10)
    return f"{QUADRANT_NAMES[quadrant]} {POSITION_NAMES[position]}"

FINAL_TEMPLATE = (
    "🦷 <b>Зуб даю,</b> что у тебя это ощущение возникает с кем-то в коммуникации.\n\n"
    "🔍 <b>Ищи с кем и где ты чувствуешь себя</b> как {insert}, и начинай работать над этой "
    "коммуникацией и отношениями. <b>Оно само не пройдёт.</b>\n\n"
    "Ну или как и большинство людей с проблемами по зубам, избегай всех ситуаций и коммуникаций, "
    "где кто-то может подсветить тебе то, что ты: {insert}.\n\n"
    "💩 Так обычно все и делают. И чем меньше своих ровных здоровых зубов, тем больше избегания и "
    "границ в коммуникации с жизнью и классными людьми.\n\n"
    "🔥 Если же <b>ты не хочешь быть «как все»</b> и тебе нужна помощь в том, чтобы разобраться, о "
    f"чём говорят твои зубы — можешь начать с просмотра записей диагностик по зубам других людей: {DIAGNOSTICS_POST_URL}\n\n"
    "Или <b>приходи в диагностику самостоятельно</b>, а оттуда решишь, как дальше вести себя с "
    f"другими людьми, чтобы не разрушать свои зубы. Подробности по ссылке: {ROADMAP_URL}\n\n"
    f"По всем вопросам — пиши администратору Марии {ADMIN_USERNAME}."
)


def _dialogue_session_id(row) -> str:
    """session_id для склейки «Диалоги»/«Диалоги (матрица)» с «Зубы» (ТЗ-доп. №6) —
    считается на лету из user_id+started_at, отдельная колонка в БД не нужна."""
    return f"{row['user_id']}_{row['started_at']}"


async def _log_turn(session_id: int, user, step: str, who: str, text: str, msg_type: str = "обычный") -> None:
    """ТЗ-доп. №6, ч.1 — полный лог диалога ветки «Зубы», одна строка на реплику."""
    row = db.get_teeth_session(session_id)
    session_num = row["session_num"] if row else ""
    dialogue_session_id = _dialogue_session_id(row) if row else ""
    await sheets_logger.append(
        "Диалоги",
        [
            db.now(), user.id, dialogue_session_id, user.username or "", "teeth",
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


async def _log_and_build_matrix(session_id: int) -> None:
    row = db.get_teeth_session(session_id)
    try:
        await dialogue_matrix.build_teeth_matrix_column(
            _dialogue_session_id(row), row["username"] or "", row["started_at"]
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось построить столбец матрицы Зубы для сессии %s", session_id)


async def _check_acute(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, session_id: int) -> bool:
    if ACUTE_RE.search(text):
        db.update_teeth_session(session_id, acute_symptoms=1)
        db.finish_teeth_session(session_id, completed=False)
        await _send(update, context, session_id, "acute", ACUTE_TEXT, msg_type="отказ", reply_markup=back_to_menu_keyboard())
        session = _fetch_teeth_row(session_id)
        await sheets_logger.append("Зубы", session)
        await _log_and_build_matrix(session_id)
        return True
    return False


def _fetch_teeth_row(session_id: int) -> list:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM teeth_sessions WHERE id=?", (session_id,)).fetchone()
    return [
        row["started_at"], row["user_id"], row["username"] or "", row["session_num"],
        row["tooth_number"], row["scary_thing"], row["feeling_word"],
        "да" if row["acute_symptoms"] else "нет",
        "да" if row["completed"] else "нет",
        _dialogue_session_id(row),
    ]


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    db.upsert_user(user.id, user.username)

    # см. relationships.entry() — тот же двойной-тап-плодит-сессии баг, тот же фикс.
    existing_id = context.user_data.get("teeth_session_id")
    if existing_id is not None:
        existing_row = db.get_teeth_session(existing_id)
        if existing_row is not None and existing_row["ended_at"] is None:
            await query.answer("Сессия уже идёт — отвечай в чате выше 👆")
            return None

    await query.answer()

    if db.count_teeth_sessions_this_month(user.id) >= await limits.get_limit(context.bot, user.id, "teeth"):
        db.log_limit_hit(user.id, "teeth")
        await sheets_logger.append(
            "Лимиты", [db.now(), user.id, user.username or "", "Зубы", "упёрся в лимит"]
        )
        await query.edit_message_text(
            limit_exhausted_text("Зубы"),
            reply_markup=back_to_menu_keyboard(),
        )
        return ConversationHandler.END

    session_id = db.create_teeth_session(user.id, user.username)
    context.user_data["teeth_session_id"] = session_id
    context.user_data.pop("teeth_feeling_attempts", None)
    hostility.reset_session(context)

    with open(TEETH_CHART_PATH, "rb") as photo:
        await query.message.reply_photo(photo)
    await _send(update, context, session_id, "ask_tooth", Q_TOOTH)
    return ASK_TOOTH


async def ask_tooth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="teeth_tooth", conv_handler=conv_handler, state=ASK_TOOTH,
        process=lambda text: _process_ask_tooth(update, context, text),
    )


async def _process_ask_tooth(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["teeth_session_id"]
    text = text.strip()

    status = await hostility.precheck(
        update, context, branch="teeth", step="ask_tooth",
        acute_check=lambda: _check_acute(update, context, text, session_id),
        bot_question=Q_TOOTH, text_override=text,
    )
    if status in ("crisis", "acute", "closed"):
        if status != "acute":
            db.finish_teeth_session(session_id, completed=False)
        await hostility.maybe_send_self_harm_note(update, context)
        return ConversationHandler.END
    if status == "hostile":
        return ASK_TOOTH

    await _log_turn(session_id, update.effective_user, "ask_tooth", "человек", text)

    digits = re.sub(r"\D", "", text)
    if digits and digits.isdigit() and text.strip() == digits:
        number = int(digits)
        if number in VALID_TEETH_NUMBERS:
            return await _ask_confirmation(update, context, number)
        await _send(
            update, context, session_id, "ask_tooth",
            "Такого номера нет на схеме. Глянь ещё раз картинку и напиши номер (11-18, 21-28, 31-38, 41-48).",
            msg_type="уточнение",
        )
        return ASK_TOOTH

    candidate = await llm.parse_tooth_number(text)
    if candidate and candidate in VALID_TEETH_NUMBERS:
        return await _ask_confirmation(update, context, candidate)

    await _send(
        update, context, session_id, "ask_tooth",
        "Не понял, какой это зуб. Посмотри на схему и напиши номер цифрами.",
        msg_type="уточнение",
    )
    return ASK_TOOTH


async def _ask_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, number: int) -> int:
    session_id = context.user_data["teeth_session_id"]
    context.user_data["teeth_candidate"] = number
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да", callback_data="tooth_confirm:yes"),
                InlineKeyboardButton("Нет", callback_data="tooth_confirm:no"),
            ]
        ]
    )
    await _send(
        update, context, session_id, "confirm_tooth",
        f"Правильно понимаю: {describe_tooth(number)}, зуб {number}?", reply_markup=keyboard,
    )
    return CONFIRM_TOOTH


async def confirm_tooth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    session_id = context.user_data["teeth_session_id"]
    await query.answer()
    await _log_turn(session_id, update.effective_user, "confirm_tooth", "человек", query.data, msg_type="кнопка")
    if query.data.endswith(":no"):
        context.user_data.pop("teeth_candidate", None)
        await query.edit_message_text("Хорошо, напиши номер по схеме ещё раз.")
        await _log_turn(session_id, update.effective_user, "ask_tooth", "бот", "Хорошо, напиши номер по схеме ещё раз.", msg_type="уточнение")
        return ASK_TOOTH
    number = context.user_data.pop("teeth_candidate")
    confirm_text = f"Принято — {describe_tooth(number)} (зуб {number})."
    await query.edit_message_text(confirm_text)
    await _log_turn(session_id, update.effective_user, "confirm_tooth", "бот", confirm_text)
    return await _accept_tooth(update, context, number, via_query=True)


async def _accept_tooth(update, context, number: int, via_query: bool = False) -> int:
    session_id = context.user_data["teeth_session_id"]
    db.update_teeth_session(session_id, tooth_number=number)
    await _send(update, context, session_id, "ask_scary", Q_SCARY)
    return ASK_SCARY


async def ask_scary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="teeth_scary", conv_handler=conv_handler, state=ASK_SCARY,
        process=lambda text: _process_ask_scary(update, context, text),
    )


async def _process_ask_scary(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["teeth_session_id"]
    text = text.strip()

    status = await hostility.precheck(
        update, context, branch="teeth", step="ask_scary",
        acute_check=lambda: _check_acute(update, context, text, session_id),
        bot_question=Q_SCARY, text_override=text,
    )
    if status in ("crisis", "acute", "closed"):
        if status != "acute":
            db.finish_teeth_session(session_id, completed=False)
        await hostility.maybe_send_self_harm_note(update, context)
        return ConversationHandler.END
    if status == "hostile":
        return ASK_SCARY

    await _log_turn(session_id, update.effective_user, "ask_scary", "человек", text)
    db.update_teeth_session(session_id, scary_thing=text)

    subject = await llm.classify_diagnosis_subject(text)
    if subject["subject"] == "other":
        note_text = _other_person_note_text(subject["subject_age"], subject["motivation"])
        await _send(update, context, session_id, "ask_scary", note_text, msg_type="уточнение")

    await _send(update, context, session_id, "ask_feeling", Q_FEELING)
    return ASK_FEELING


async def ask_feeling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="teeth_feeling", conv_handler=conv_handler, state=ASK_FEELING,
        process=lambda text: _process_ask_feeling(update, context, text),
    )


async def _process_ask_feeling(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    session_id = context.user_data["teeth_session_id"]
    text = text.strip()

    status = await hostility.precheck(
        update, context, branch="teeth", step="ask_feeling",
        acute_check=lambda: _check_acute(update, context, text, session_id),
        bot_question=Q_FEELING, text_override=text,
    )
    if status in ("crisis", "acute", "closed"):
        if status != "acute":
            db.finish_teeth_session(session_id, completed=False)
        await hostility.maybe_send_self_harm_note(update, context)
        return ConversationHandler.END
    if status == "hostile":
        return ASK_FEELING

    await _log_turn(session_id, update.effective_user, "ask_feeling", "человек", text)

    attempts = context.user_data.get("teeth_feeling_attempts", 0)
    if attempts < FEELING_DIG_MAX_ATTEMPTS:
        depth = await llm.classify_feeling_depth(text)
        if not depth["is_deep"]:
            context.user_data["teeth_feeling_attempts"] = attempts + 1
            dig_text = FEELING_DIG_TEXT.format(label=depth["label"])
            await _send(update, context, session_id, "ask_feeling", dig_text, msg_type="уточнение")
            return ASK_FEELING

    context.user_data.pop("teeth_feeling_attempts", None)
    db.update_teeth_session(session_id, feeling_word=text)
    db.finish_teeth_session(session_id, completed=True)
    await sheets_logger.append("Зубы", _fetch_teeth_row(session_id))
    insert = html.escape(await llm.normalize_feeling_insert(text))
    final_text = FINAL_TEMPLATE.format(insert=insert)
    await _send(
        update, context, session_id, "final", final_text,
        reply_markup=back_to_menu_keyboard(), parse_mode=ParseMode.HTML,
    )
    await _log_and_build_matrix(session_id)
    await hostility.maybe_send_self_harm_note(update, context)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot.handlers.menu import show_menu

    session_id = context.user_data.pop("teeth_session_id", None)
    if session_id:
        db.finish_teeth_session(session_id, completed=False)
    await show_menu(update, context)
    await hostility.maybe_send_self_harm_note(update, context)
    return ConversationHandler.END


conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(entry, pattern="^menu:teeth$")],
    states={
        ASK_TOOTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_tooth)],
        CONFIRM_TOOTH: [CallbackQueryHandler(confirm_tooth, pattern="^tooth_confirm:")],
        ASK_SCARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_scary)],
        ASK_FEELING: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_feeling)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        CommandHandler("start", cancel),
        CallbackQueryHandler(cancel, pattern="^menu:back$"),
    ],
    name="teeth_conversation",
    persistent=True,
)
