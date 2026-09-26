"""Сквозной модуль «Скрытая потребность + SET-панчлайн».

Срабатывает во всех трёх ветках, до обычной логики шага, когда пользователь
нападает на бота/концепцию/формат. Жёсткие исключения (кризис, острая боль
в «Зубах», злость на партнёра, шаг D в «Отношениях») см. в docstring `precheck`.
"""
import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from bot import db, llm
from bot.config import (
    ADMIN_CHAT_ID,
    ADMIN_USERNAME,
    HOSTILITY_CONFIDENCE_THRESHOLD,
    HOSTILITY_MAX_PUNCHLINES,
    LOG_SPREADSHEET_ID,
)
from bot.keyboards import back_to_menu_keyboard
from bot.sheets import sheets_logger

logger = logging.getLogger(__name__)

SELF_HARM_TEXT = (
    "Стоп, это важнее, чем весь остальной разговор. Похоже, тебе сейчас очень тяжело. Пожалуйста, не "
    "оставайся с этим один — прямо сейчас можно позвонить на бесплатную анонимную линию психологической "
    "помощи 8-800-2000-122 (круглосуточно, по России) или написать напрямую специалисту: "
    f"{ADMIN_USERNAME}. Возвращайся сюда, когда будешь готов(а) — никуда не тороплю."
)

FINAL_MESSAGE = (
    "Похоже, спорить тебе интереснее, чем разбираться. Хочешь поспорить с живым человеком — пиши "
    f"{ADMIN_USERNAME}."
)

PROFANITY_RE = re.compile(
    r"\b(?:ху[йяеёю]\w*|хер\w*|пизд\w*|еб[а-яё]{1,6}|бля\w*|мудо\w*|мудак\w*|долбо[её]б\w*|гандон\w*)",
    re.IGNORECASE,
)


def _has_profanity(text: str) -> bool:
    return bool(PROFANITY_RE.search(text))


def _admin_log_link() -> str:
    return f"https://docs.google.com/spreadsheets/d/{LOG_SPREADSHEET_ID}/edit"


async def _flush_pending_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Если предыдущим сообщением был панчлайн — теперь известна реакция пользователя
    (это текущее сообщение), дописываем отложенную строку в лог «Выпады»."""
    pending = context.user_data.pop("hostility_pending_row", None)
    if not pending:
        return
    reaction = (update.effective_message.text or "")[:500]
    row = pending["prefix"] + [reaction, pending["streak"], "да" if pending["closed"] else "нет"]
    await sheets_logger.append("Выпады", row)


def _track_profanity(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if _has_profanity(text):
        context.user_data["hostility_profanity_seen"] = True


def reset_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вызывать при входе в ветку/начале новой сессии — сбрасывает счётчик выпадов."""
    context.user_data["hostility_streak"] = 0
    context.user_data["hostility_last_category"] = None
    context.user_data["hostility_profanity_seen"] = False
    context.user_data.pop("hostility_pending_row", None)


async def precheck(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    branch: str,
    step: str,
    skip_hostility: bool = False,
    acute_check=None,
    bot_question: str | None = None,
    text_override: str | None = None,
) -> str:
    """Возвращает:
    - "none"    — не перехвачено, обрабатывай сообщение как обычно;
    - "crisis"  — суицидальный кризис, ответ уже отправлен, обработчик должен
                  завершить сессию/разговор (ConversationHandler.END);
    - "acute"   — сработал переданный acute_check (например острая боль в
                  «Зубах»), он уже сам отправил ответ, обработчик завершает разговор;
    - "hostile" — панчлайн отправлен, обработчик должен остаться на том же шаге,
                  ничего больше с этим сообщением не делать;
    - "closed"  — лимит панчлайнов исчерпан, финальное сообщение отправлено,
                  обработчик должен завершить сессию/разговор.

    Порядок проверок (жёсткие исключения по ТЗ): кризис → acute_check → (если
    skip_hostility — стоп) → классификация враждебности.
    """
    await _flush_pending_reaction(update, context)
    text = text_override if text_override is not None else (update.effective_message.text or "")
    _track_profanity(context, text)

    if await llm.detect_self_harm(text):
        await update.effective_message.reply_text(SELF_HARM_TEXT, reply_markup=back_to_menu_keyboard())
        return "crisis"

    if acute_check is not None and await acute_check():
        return "acute"

    if skip_hostility:
        return "none"

    verdict = await llm.classify_hostility(text, bot_question)
    if not (
        verdict["hostile"]
        and verdict["confidence"] >= HOSTILITY_CONFIDENCE_THRESHOLD
        and verdict["target"] != "other"
    ):
        return "none"

    return await _respond_hostile(update, context, branch, step, text, verdict)


async def _respond_hostile(update, context, branch: str, step: str, text: str, verdict: dict) -> str:
    user = update.effective_user
    streak = context.user_data.get("hostility_streak", 0) + 1
    context.user_data["hostility_streak"] = streak
    closed = streak > HOSTILITY_MAX_PUNCHLINES

    if closed:
        reply_text = FINAL_MESSAGE
        category = ""
        hidden_need = ""
        await update.effective_message.reply_text(reply_text, reply_markup=back_to_menu_keyboard())
    else:
        avoid_category = context.user_data.get("hostility_last_category")
        profanity_allowed = context.user_data.get("hostility_profanity_seen", False)
        result = await llm.generate_punchline(text, branch, avoid_category, profanity_allowed)
        reply_text = result["reply"] or FINAL_MESSAGE
        category = result["category"]
        hidden_need = result["hidden_need"]
        context.user_data["hostility_last_category"] = category
        await update.effective_message.reply_text(reply_text)

        if streak == HOSTILITY_MAX_PUNCHLINES and ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"⚠️ @{user.username or user.id} — третий выпад в ветке «{branch}». "
                        f"Ещё один — и сессия закроется. Лог: {_admin_log_link()}"
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось отправить уведомление о выпадах админу")

    context.user_data["hostility_pending_row"] = {
        "prefix": [
            db.now(), user.id, user.username or "", branch, step, text,
            verdict["target"], round(verdict["confidence"], 2), hidden_need, category, reply_text,
        ],
        "streak": streak,
        "closed": closed,
    }

    return "closed" if closed else "hostile"
