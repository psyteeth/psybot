import logging
import random
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import db, debounce, hostility, limits, llm
from bot.handlers import teeth
from bot.author_answers import ENTRIES as AUTHOR_ANSWERS
from bot.config import (
    ADMIN_CHAT_ID,
    ADMIN_USERNAME,
    CONCEPT_CHAT_USERNAME,
    CONCEPT_ROUTER_CONFIDENCE_THRESHOLD,
    CONCEPT_ROUTER_MAX_CLARIFICATIONS,
    CONCEPT_ROUTER_RELEVANCE_THRESHOLD,
    SECRETS_MAX_PER_SESSION,
    SECRETS_MIN_GAP_ANSWERS,
    ROADMAP_URL,
    SECRETS_PAUSE_AFTER_IGNORED,
    limit_exhausted_text,
)
from bot.keyboards import back_to_menu_keyboard
from bot.secrets_index import secrets_index
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
TEETH_RESUME_TEXT = (
    "Похоже, «{text}» — это ответ на вопрос из разбора зуба, который мы не закончили 🙂 "
    "Закончить тот разбор с этим ответом?"
)
# Личный запрос («хочу разобрать свою ситуацию», «откуда у меня…») — не здесь: «Концепция» объясняет
# метод, а разбор жизни уводит в бесконечную псевдотерапию (живой кейс 502643542, 02.10).
PERSONAL_REQUEST_TEXT = (
    "Похоже, тебе хочется разобраться в своей ситуации, а не в теории 🙌 Здесь, в «Концепции», я "
    "только объясняю сам метод.\n\n"
    "Свою ситуацию с конкретным человеком можно разобрать по шагам в ветке «Отношения» — кнопка ниже.\n\n"
    "А полный разбор, с чем связаны именно твои зубы, — на диагностике в Психостоматологии №1. "
    f"Подробности по ссылке: {ROADMAP_URL}\n\n"
    f"По всем вопросам — пиши администратору Марии {ADMIN_USERNAME}."
)


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


# --- «Секреты из таблицы» (мини-ТЗ 29.09) ---

def _reset_secrets_session_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["concept_secrets_shown_count"] = 0
    context.user_data["concept_secrets_answer_count"] = 0
    context.user_data["concept_secrets_last_shown_at_answer"] = -999
    context.user_data["concept_secrets_ignore_streak"] = 0
    context.user_data["concept_secrets_paused"] = False
    context.user_data.pop("concept_pending_secret_chunk_id", None)
    context.user_data.pop("concept_pending_secret_text", None)


async def _sweep_pending_secret_as_no_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chunk_id = context.user_data.pop("concept_pending_secret_chunk_id", None)
    context.user_data.pop("concept_pending_secret_text", None)
    if not chunk_id:
        return
    await sheets_logger.update_secret_reaction(chunk_id, update.effective_user.id, "нет ответа")


async def _handle_pending_secret_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int | None:
    """Если недавно был показан секрет — разбирает реакцию на НЕГО в первую очередь.
    Возвращает состояние, если сообщение целиком про секрет («расскажи подробнее» — раскрыли и
    вернулись в ASKING), иначе None — вызывающий обрабатывает text как обычно (в т.ч. возражение
    или новую тему, которые тоже считаются реакцией, но не «съедают» сообщение)."""
    chunk_id = context.user_data.get("concept_pending_secret_chunk_id")
    if not chunk_id:
        return None
    user = update.effective_user
    secret_text = context.user_data.get("concept_pending_secret_text", "")

    verdict = await llm.classify_secret_reaction(secret_text, text)
    reaction_label = {"more": "уточнил", "dispute": "возразил", "other": "проигнорировал"}[verdict]
    await sheets_logger.update_secret_reaction(chunk_id, user.id, reaction_label)
    context.user_data.pop("concept_pending_secret_chunk_id", None)
    context.user_data.pop("concept_pending_secret_text", None)

    if verdict == "more":
        context.user_data["concept_secrets_ignore_streak"] = 0
        await _log_turn(context, user, "secret_more", "человек", text)
        chunk = secrets_index.get(chunk_id)
        if chunk:
            neighbors = secrets_index.neighbors(chunk_id, limit=2)
            detail_source = "\n\n".join([chunk.text] + [n.text for n in neighbors])
            detail_text = await llm.generate_secret_reveal(detail_source, chunk.status, secret_text, full=True)
        else:
            detail_text = ""
        if not detail_text:
            detail_text = "Хм, под рукой сейчас нет деталей по этому — но идея всё ещё интересная, можем вернуться к ней позже."
        await _send(update, context, "secret_detail", detail_text, msg_type="секрет")
        return ASKING

    if verdict == "dispute":
        context.user_data["concept_secrets_ignore_streak"] = 0
        return None  # пусть обычный поток обработает это как содержательный ответ/спор

    streak = context.user_data.get("concept_secrets_ignore_streak", 0) + 1
    context.user_data["concept_secrets_ignore_streak"] = streak
    if streak >= SECRETS_PAUSE_AFTER_IGNORED:
        context.user_data["concept_secrets_paused"] = True
    return None


async def _maybe_share_secret(
    update: Update, context: ContextTypes.DEFAULT_TYPE, question: str, resolved_question: str
) -> None:
    user = update.effective_user
    answer_count = context.user_data.get("concept_secrets_answer_count", 0) + 1
    context.user_data["concept_secrets_answer_count"] = answer_count

    if context.user_data.get("concept_secrets_paused"):
        return
    if context.user_data.get("concept_secrets_shown_count", 0) >= SECRETS_MAX_PER_SESSION:
        return
    last_shown_at = context.user_data.get("concept_secrets_last_shown_at_answer", -999)
    if (answer_count - last_shown_at) < SECRETS_MIN_GAP_ANSWERS:
        return

    try:
        await secrets_index.ensure_fresh()
        if not secrets_index.is_loaded():
            return
        seen_ids = db.get_seen_secret_ids(user.id)
        candidates = secrets_index.search(f"{question} {resolved_question}", top_k=5, exclude_ids=seen_ids)
        if not candidates:
            return

        gate = await llm.classify_secret_gate(
            resolved_question,
            f"Исходный вопрос: {question}\nУточнённый: {resolved_question}",
            [{"chunk_id": c.chunk_id, "status": c.status, "text": c.text} for c in candidates],
        )
        if not gate["share"] or not gate["chunk_id"]:
            return
        chunk = secrets_index.get(gate["chunk_id"])
        if not chunk:
            return

        reveal_text = await llm.generate_secret_reveal(chunk.text, chunk.status, resolved_question)
        if not reveal_text:
            return

        await _send(update, context, "secret", reveal_text, msg_type="секрет")
        await sheets_logger.append(
            "Секреты",
            [db.now(), user.id, user.username or "", "concept", chunk.chunk_id, chunk.sheet, reveal_text, ""],
        )
        db.record_secret_shown(user.id, chunk.chunk_id)
        context.user_data["concept_secrets_shown_count"] = context.user_data.get("concept_secrets_shown_count", 0) + 1
        context.user_data["concept_secrets_last_shown_at_answer"] = answer_count
        context.user_data["concept_pending_secret_chunk_id"] = chunk.chunk_id
        context.user_data["concept_pending_secret_text"] = reveal_text
    except Exception:  # noqa: BLE001
        logger.exception("_maybe_share_secret упал, отвечаем без секрета")


# --- Режим ответа: простой / уточнение / углубление (доп. ТЗ 30.09) ---

def _reset_concept_topic_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("concept_session_id", None)
    context.user_data.pop("concept_raw_question", None)
    context.user_data.pop("concept_combined_question", None)
    context.user_data.pop("concept_clarify_count", None)
    context.user_data.pop("concept_irritated", None)
    context.user_data.pop("concept_last_readings", None)
    context.user_data.pop("concept_last_bot_question", None)
    context.user_data.pop("concept_reading_options", None)


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="concept_ask", conv_handler=conv_handler, state=ASKING,
        process=lambda text: _process_message(update, context, text),
    )


async def clarify_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await debounce.collect(
        update, context, step_id="concept_clarify", conv_handler=conv_handler, state=CLARIFY,
        process=lambda text: _process_message(update, context, text),
    )


async def reading_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Тап по кнопке-варианту прочтения в режиме УТОЧНЕНИЕ — эквивалент того, что человек
    напечатал бы этот вариант текстом."""
    query = update.callback_query
    await query.answer()
    try:
        idx = int(query.data.split(":", 1)[1])
        options = context.user_data.get("concept_reading_options", [])
        chosen_text = options[idx]
    except (ValueError, IndexError):
        return CLARIFY
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    return await _process_message(update, context, chosen_text)


async def _process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    user = update.effective_user

    if await _check_limit(update, context, user, via_query=False):
        return ConversationHandler.END

    secret_reaction_state = await _handle_pending_secret_reaction(update, context, text)
    if secret_reaction_state is not None:
        return secret_reaction_state

    is_first = not context.user_data.get("concept_raw_question")
    bot_question = None if is_first else context.user_data.get("concept_last_bot_question", "")
    prior_topic = context.user_data.get("concept_last_topic") or context.user_data.get("concept_raw_question")
    sources_request = await llm.is_sources_request(text, prior_topic)

    status = await hostility.precheck(
        update, context, branch="concept", step="ask" if is_first else "clarify",
        bot_question=bot_question, text_override=text, skip_hostility=sources_request,
    )
    if status == "crisis":
        return ConversationHandler.END
    if status == "closed":
        db.increment_concept_messages(user.id)
        await hostility.maybe_send_self_harm_note(update, context)
        return ConversationHandler.END
    if status == "hostile":
        return ASKING if is_first else CLARIFY

    db.increment_concept_messages(user.id)

    if sources_request:
        return await _respond_sources(update, context, text, prior_topic)

    if is_first and await _offer_teeth_resume(update, context, text):
        return ASKING

    return await _process_concept_text(update, context, text, is_first)


async def _offer_teeth_resume(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Первое сообщение в «Концепции» после брошенного на «Каким ты себя чувствуешь?» разбора зуба —
    если это похоже на ответ на тот вопрос, предлагаем закончить разбор (см. teeth.UNFINISHED_KEY)."""
    unfinished = context.user_data.get(teeth.UNFINISHED_KEY)
    if not unfinished:
        return False
    if time.time() - unfinished.get("ts", 0) > teeth.UNFINISHED_TTL_SECONDS:
        context.user_data.pop(teeth.UNFINISHED_KEY, None)
        return False
    # предлагаем только на первое сообщение после ухода из «Зубов» — дальше не переспрашиваем
    context.user_data.pop(teeth.UNFINISHED_KEY, None)
    if len(text.split()) > 5 or not await llm.is_feeling_answer(text):
        return False
    context.user_data[teeth.UNFINISHED_KEY] = unfinished
    context.user_data["concept_teeth_candidate"] = text
    await _log_turn(context, update.effective_user, "ask", "человек", text)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да, закончить разбор зуба", callback_data="concept_teeth:yes")],
            [InlineKeyboardButton("Нет, это вопрос про концепцию", callback_data="concept_teeth:no")],
        ]
    )
    await _send(update, context, "teeth_resume_offer", TEETH_RESUME_TEXT.format(text=text), reply_markup=keyboard)
    return True


async def teeth_resume_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    text = context.user_data.pop("concept_teeth_candidate", None)
    unfinished = context.user_data.pop(teeth.UNFINISHED_KEY, None)
    if not text:
        return ASKING
    await _log_turn(context, update.effective_user, "teeth_resume_offer", "человек", query.data, msg_type="кнопка")
    if query.data == "concept_teeth:yes" and unfinished:
        _reset_concept_topic_state(context)
        await teeth.finish_with_feeling(update, context, unfinished["session_id"], text)
        return ConversationHandler.END
    return await _process_concept_text(update, context, text, is_first=True, logged=True)


async def _process_concept_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, is_first: bool, logged: bool = False
) -> int:
    user = update.effective_user

    if await llm.is_personal_request(text, context.user_data.get("concept_last_answer")):
        return await _redirect_personal_request(update, context, text, is_first)

    if is_first:
        pending_marker = context.user_data.pop("concept_pending_reaction_marker", None)
        context.user_data["concept_session_id"] = f"{user.id}_{db.now()}"
        if not logged:
            await _log_turn(context, user, "ask", "человек", text)

        prior_topic = context.user_data.get("concept_last_topic")
        last_answer = context.user_data.pop("concept_last_answer", None)
        if last_answer and len(text) <= 60 and await llm.is_hook_acceptance(last_answer, text):
            # согласие на крючок прошлого ответа — продолжаем ту же тему, а не новый вопрос
            if pending_marker:
                await sheets_logger.update_concept_reaction(pending_marker, user.id, "углубился")
            followup = (
                f"{prior_topic}\n(бот ответил: {last_answer})\n(клиент ответил «{text}» — согласен на "
                "предложение в конце ответа бота; выполни именно это предложение, не повторяя уже сказанное)"
            )
            context.user_data["concept_raw_question"] = text
            context.user_data["concept_combined_question"] = followup
            context.user_data["concept_clarify_count"] = 0
            context.user_data["concept_irritated"] = False
            context.user_data["concept_hook_followup"] = True
            return await _route_and_respond(update, context)

        await concept_store.ensure_fresh()
        await secrets_index.ensure_fresh()
        best = secrets_index.search_scored(text, top_k=1) if secrets_index.is_loaded() else []
        relevance_threshold = secrets_index.param("relevance_threshold", CONCEPT_ROUTER_RELEVANCE_THRESHOLD)
        matches_table = bool(best) and best[0][1] >= relevance_threshold
        if not matches_table and await llm.is_offtopic_concept(
            text, prior_topic=prior_topic, topics=concept_store.titles()
        ):
            if pending_marker:
                await sheets_logger.update_concept_reaction(pending_marker, user.id, "ушёл")
            await _send(update, context, "offtopic", OFFTOPIC_TEXT, msg_type="отказ", reply_markup=back_to_menu_keyboard())
            context.user_data.pop("concept_session_id", None)
            return ASKING

        await _maybe_rant_opener(update, text)
        context.user_data["concept_raw_question"] = text
        context.user_data["concept_combined_question"] = text
        context.user_data["concept_clarify_count"] = 0
        context.user_data["concept_irritated"] = False
        context.user_data["_concept_pending_marker_to_resolve"] = pending_marker
    else:
        await _log_turn(context, user, "clarify_reply", "человек", text)
        await _maybe_rant_opener(update, text)
        combined = context.user_data.get("concept_combined_question", "")
        context.user_data["concept_combined_question"] = f"{combined}\n(уточнение клиента: {text})"

    return await _route_and_respond(update, context)


async def _redirect_personal_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, is_first: bool
) -> int:
    user = update.effective_user
    if is_first:
        context.user_data["concept_session_id"] = f"{user.id}_{db.now()}"
    await _log_turn(context, user, "ask" if is_first else "clarify_reply", "человек", text)
    pending_marker = context.user_data.pop("concept_pending_reaction_marker", None)
    if pending_marker:
        await sheets_logger.update_concept_reaction(pending_marker, user.id, "личный запрос")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Отношения", callback_data="menu:relationships")],
            [InlineKeyboardButton("В меню", callback_data="menu:back")],
        ]
    )
    await _send(update, context, "personal_redirect", PERSONAL_REQUEST_TEXT, reply_markup=keyboard)
    if ADMIN_CHAT_ID:
        try:
            relay = f"❓ {user.username or user.id}: {text}\n(личный запрос → Отношения/диагностика)"
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=relay[:4000])
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить релей личного запроса админу")
    _reset_concept_topic_state(context)
    context.user_data.pop("concept_last_topic", None)
    context.user_data.pop("concept_last_answer", None)
    return ConversationHandler.END


async def _route_and_respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    question = context.user_data.get("concept_combined_question", "")
    raw_question = context.user_data.get("concept_raw_question", question)
    clarify_count = context.user_data.get("concept_clarify_count", 0)
    irritated = context.user_data.get("concept_irritated", False)

    await secrets_index.ensure_fresh()
    scored = secrets_index.search_scored(question, top_k=5) if secrets_index.is_loaded() else []
    candidates_for_router = [{"score": s, "text": c.text} for c, s in scored]

    route = await llm.route_concept_question(question, question, candidates_for_router, clarify_count)
    context.user_data["concept_last_readings"] = route.get("readings", [])
    if route.get("irritation"):
        context.user_data["concept_irritated"] = True
        irritated = True

    relevance_threshold = secrets_index.param("relevance_threshold", CONCEPT_ROUTER_RELEVANCE_THRESHOLD)
    confidence_threshold = secrets_index.param("confidence_threshold", CONCEPT_ROUTER_CONFIDENCE_THRESHOLD)
    max_clarifications = int(secrets_index.param("max_clarifications", CONCEPT_ROUTER_MAX_CLARIFICATIONS))
    best_score = scored[0][1] if scored else 0.0

    mode = route["mode"]
    capped = False
    # согласие на крючок — выполняем обещанное одним развёрнутым ответом. Не «простым»: лимит в 3
    # предложения обрезал пример на середине (живой кейс 05.10: показана только версия стабилизатора,
    # катализатора — нет), а после простого ответа ещё и подмешивался «секрет» вторым сообщением со
    # своим вопросом. «Углубление» теперь строит ответ по всей таблице, а не по случайному куску.
    if context.user_data.pop("concept_hook_followup", False):
        mode = "deepen"
    if mode == "clarify":
        if best_score >= relevance_threshold and route["confidence"] >= confidence_threshold:
            mode = "simple"
        elif irritated:
            mode = "simple"
            capped = True
        elif clarify_count >= max_clarifications:
            mode = "simple"
            capped = True

    pending_marker = context.user_data.pop("_concept_pending_marker_to_resolve", None)
    if pending_marker:
        reaction = "углубился" if mode == "deepen" else "ушёл"
        await sheets_logger.update_concept_reaction(pending_marker, update.effective_user.id, reaction)

    if mode == "clarify":
        return await _respond_clarify(update, context, question, route)
    if mode == "deepen":
        return await _respond_deepen(update, context, question, scored, route)
    return await _respond_simple(update, context, raw_question, question, route, capped)


async def _respond_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str, route: dict) -> int:
    user = update.effective_user
    clarify_count = context.user_data.get("concept_clarify_count", 0) + 1
    context.user_data["concept_clarify_count"] = clarify_count
    readings = route.get("readings", [])

    clarify_text = await llm.generate_concept_clarify_question(
        question, readings, route.get("missing", ""), short=route.get("confusion", False)
    )

    keyboard = None
    if readings and 2 <= len(readings) <= 3:
        context.user_data["concept_reading_options"] = readings
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(r, callback_data=f"concept_reading:{i}")] for i, r in enumerate(readings)]
        )

    context.user_data["concept_last_bot_question"] = clarify_text
    await _send(update, context, "clarify", clarify_text, msg_type="уточнение", reply_markup=keyboard)

    await sheets_logger.append(
        "Концепция",
        [
            db.now(), user.id, user.username or "", context.user_data.get("concept_raw_question", question),
            clarify_text, "нет", "нет", question,
            "clarify", route.get("confidence", 0.0), clarify_count,
            "да" if route.get("irritation") else "нет", "да" if route.get("confusion") else "нет", "",
        ],
    )
    return CLARIFY


async def _respond_deepen(
    update: Update, context: ContextTypes.DEFAULT_TYPE, question: str, scored: list, route: dict
) -> int:
    user = update.effective_user
    raw_question = context.user_data.get("concept_raw_question", question)

    # 05.10: «подробно» — полноценный разбор по материалам всей таблицы, а не «секрет» из 1-3 кусков
    context_text = await _build_concept_context(question)
    similar_nums = await llm.pick_similar_author_answers(question, AUTHOR_ANSWERS)
    author_examples = [e for e in AUTHOR_ANSWERS if e["num"] in similar_nums]
    answer = await llm.answer_concept_question(question, context_text, author_examples, simple=False)
    if not answer:
        return await _respond_simple(update, context, raw_question, question, route, capped=False)

    await _send(update, context, "deepen", answer, reply_markup=back_to_menu_keyboard())
    context.user_data["concept_last_topic"] = question
    context.user_data["concept_last_answer"] = answer

    dispute = await llm.is_dispute(raw_question)
    await sheets_logger.append(
        "Концепция",
        [
            db.now(), user.id, user.username or "", raw_question, answer,
            "да" if dispute else "нет", "нет", question,
            "deepen", route.get("confidence", 0.0), context.user_data.get("concept_clarify_count", 0),
            "да" if route.get("irritation") else "нет", "да" if route.get("confusion") else "нет", "",
        ],
    )

    if ADMIN_CHAT_ID:
        try:
            relay = f"❓ {user.username or user.id}: {raw_question}\n(углубление)\n\n💬 {answer}"
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=relay[:4000])
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить релей углубления админу")

    _reset_concept_topic_state(context)
    await hostility.maybe_send_self_harm_note(update, context)
    return ASKING


SOURCES_UNKNOWN_TEXT = (
    "Точных научных источников по этому в моих материалах нет — и выдумывать не буду. Я передал "
    "твой вопрос автору, вернусь к тебе с ответом."
)
TELEGRAM_CHUNK = 3900


def _split_for_telegram(text: str, limit: int = TELEGRAM_CHUNK) -> list[str]:
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit and buf:
            parts.append(buf.rstrip())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        parts.append(buf.rstrip())
    return parts


def _select_citations(topic: str, max_chars: int = 25000) -> str:
    """Строки таблицы с исследованиями, относящиеся к теме: по совпадению слов темы (основы слов),
    самые релевантные — первыми."""
    stems = {w[:5] for w in re.findall(r"\w+", topic.lower()) if len(w) > 3}
    scored = []
    for sheet, cell in concept_store.citation_lines():
        low = f"{sheet} {cell}".lower()
        score = sum(1 for st in stems if st in low)
        if score:
            scored.append((score, sheet, cell))
    scored.sort(key=lambda x: -x[0])
    out, total = [], 0
    for _score, sheet, cell in scored:
        line = f"[{sheet}] {cell}"
        if total + len(line) > max_chars:
            break
        out.append(line)
        total += len(line)
    return "\n".join(out)


async def _respond_sources(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, prior_topic: str | None) -> int:
    """05.10, решение автора: на запрос источников — либо ВСЁ, что есть в таблице, либо честное «не
    знаю, спрошу автора» (вопрос уходит админу, автор отвечает через /send). Без уговоров."""
    user = update.effective_user
    if not context.user_data.get("concept_session_id"):
        context.user_data["concept_session_id"] = f"{user.id}_{db.now()}"
    await _log_turn(context, user, "sources", "человек", text)
    topic = f"{prior_topic}\n{text}" if prior_topic else text
    await concept_store.ensure_fresh()
    citations = _select_citations(topic)
    answer = await llm.answer_sources(topic, citations) if citations else None
    if answer:
        for part in _split_for_telegram(answer):
            await _send(update, context, "sources", part, reply_markup=back_to_menu_keyboard())
    else:
        await _send(update, context, "sources", SOURCES_UNKNOWN_TEXT, reply_markup=back_to_menu_keyboard())
        if ADMIN_CHAT_ID:
            try:
                relay = (
                    f"📚 Нужны источники — в таблице не нашлось.\nОт: @{user.username or '—'} (id {user.id})\n"
                    f"Тема: {prior_topic or '—'}\nВопрос: {text}\n\nОтветить: /send {user.id} <текст>"
                )
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=relay[:4000])
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось отправить админу запрос источников")
    await sheets_logger.append(
        "Концепция",
        [
            db.now(), user.id, user.username or "", text, answer or SOURCES_UNKNOWN_TEXT,
            "нет", "да" if (not answer and ADMIN_CHAT_ID) else "нет", topic,
            "sources", 1.0, 0, "нет", "нет", "",
        ],
    )
    context.user_data["concept_last_topic"] = prior_topic or text
    _reset_concept_topic_state(context)
    return ASKING


async def _build_concept_context(question: str) -> str:
    """Нужный лист целиком + лучшие куски со всех листов + начало нарратива (в этом порядке —
    обрезка CONCEPT_CONTEXT_MAX_CHARS режет хвост, а не главное)."""
    await concept_store.ensure_fresh()
    await secrets_index.ensure_fresh()
    parts = []
    relevant_title = await llm.pick_relevant_sheet(question, concept_store.titles())
    if relevant_title:
        parts.append(f"[Раздел: {relevant_title}]\n{concept_store.get(relevant_title)[:30000]}")
    if secrets_index.is_loaded():
        chunks = [c for c, _s in secrets_index.search_scored(question, top_k=10) if c.sheet != relevant_title]
        if chunks:
            parts.append("\n\n".join(f"[Раздел: {c.sheet}] {c.text}" for c in chunks))
    narrative = concept_store.main_narrative()
    if narrative and relevant_title and "нарратив" not in relevant_title.lower():
        parts.append(f"[Раздел: основной нарратив]\n{narrative[:15000]}")
    elif not relevant_title:
        parts.append(narrative)
    return "\n\n".join(parts)


async def _respond_simple(
    update: Update, context: ContextTypes.DEFAULT_TYPE, raw_question: str, question: str, route: dict, capped: bool
) -> int:
    user = update.effective_user

    context_text = await _build_concept_context(question)

    dispute = await llm.is_dispute(raw_question)
    similar_nums = await llm.pick_similar_author_answers(question, AUTHOR_ANSWERS)
    author_examples = [e for e in AUTHOR_ANSWERS if e["num"] in similar_nums]
    answer = await llm.answer_concept_question(question, context_text, author_examples, simple=True)

    if capped:
        readings = context.user_data.get("concept_last_readings", [])
        assumption = readings[0] if readings else route.get("missing") or question
        answer = f"Я понял так: {assumption}.\n\n{answer}\n\nЕсли про другое, напиши одним предложением."

    streak = context.user_data.get("concept_dispute_streak", 0)
    streak = streak + 1 if dispute else 0
    context.user_data["concept_dispute_streak"] = streak
    escalated = streak >= DISPUTE_STREAK_THRESHOLD
    if escalated:
        answer = f"{answer}\n\n{NOT_CONVERGED_TEXT}"

    await _send(update, context, "answer", answer, reply_markup=back_to_menu_keyboard())
    context.user_data["concept_last_topic"] = question
    context.user_data["concept_last_answer"] = answer

    if not escalated:
        await _maybe_share_secret(update, context, raw_question, question)

    row_timestamp = db.now()
    await sheets_logger.append(
        "Концепция",
        [
            row_timestamp, user.id, user.username or "", raw_question, answer,
            "да" if dispute else "нет", "да" if (escalated and ADMIN_CHAT_ID) else "нет", question,
            "simple", route.get("confidence", 0.0), context.user_data.get("concept_clarify_count", 0),
            "да" if route.get("irritation") else "нет", "да" if route.get("confusion") else "нет", "",
        ],
    )
    if not escalated:
        context.user_data["concept_pending_reaction_marker"] = row_timestamp

    if ADMIN_CHAT_ID:
        try:
            relay = f"❓ {user.username or user.id}: {raw_question}\n(режим: simple)\n\n💬 {answer}"
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

    _reset_concept_topic_state(context)
    await hostility.maybe_send_self_harm_note(update, context)
    return ASKING


async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    db.upsert_user(user.id, user.username)

    if await _check_limit(update, context, user, via_query=True):
        return ConversationHandler.END

    context.user_data["concept_dispute_streak"] = 0
    context.user_data.pop("concept_last_topic", None)
    context.user_data.pop("concept_last_answer", None)
    context.user_data.pop("concept_pending_reaction_marker", None)
    _reset_concept_topic_state(context)
    _reset_secrets_session_state(context)
    hostility.reset_session(context)
    await query.edit_message_text(INTRO_TEXT, reply_markup=back_to_menu_keyboard())
    await _log_turn(context, user, "intro", "бот", INTRO_TEXT)
    return ASKING


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot.handlers.menu import show_menu

    await _sweep_pending_secret_as_no_reply(update, context)
    pending_marker = context.user_data.pop("concept_pending_reaction_marker", None)
    if pending_marker:
        await sheets_logger.update_concept_reaction(pending_marker, update.effective_user.id, "ушёл")
    await show_menu(update, context)
    await hostility.maybe_send_self_harm_note(update, context)
    return ConversationHandler.END


conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(entry, pattern="^menu:concept$")],
    states={
        ASKING: [
            CallbackQueryHandler(teeth_resume_choice, pattern="^concept_teeth:"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask),
        ],
        CLARIFY: [
            CallbackQueryHandler(reading_choice, pattern="^concept_reading:"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, clarify_reply),
        ],
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
