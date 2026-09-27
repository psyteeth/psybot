"""Тариф пользователя по членству в Telegram-чатах. Без доступа ни к одному чату и не в
списке родственников — бот не работает (см. config.NO_ACCESS_TEXT, TIER_DEFAULTS)."""
import logging
import time

from telegram import Bot
from telegram.error import TelegramError

from bot.config import MEMBERSHIP_CACHE_SECONDS, PSYTEETH_CHAT_ID, RELATIONSHIPS_CHAT_ID
from bot.sheets import relatives

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {"member", "administrator", "creator", "restricted"}

_cache: dict[int, tuple[str, float]] = {}


async def _is_member(bot: Bot, chat_id: str, user_id: int) -> bool:
    if not chat_id:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ACTIVE_STATUSES
    except TelegramError:
        return False
    except Exception:  # noqa: BLE001
        logger.exception("Проверка членства в чате упала (chat_id=%s)", chat_id)
        return False


async def get_tier(bot: Bot, user_id: int) -> str:
    """Возвращает один из: chat_unlimited | chat_member | relative | none."""
    cached = _cache.get(user_id)
    if cached and (time.time() - cached[1]) < MEMBERSHIP_CACHE_SECONDS:
        return cached[0]

    if await _is_member(bot, RELATIONSHIPS_CHAT_ID, user_id):
        tier = "chat_unlimited"
    elif await _is_member(bot, PSYTEETH_CHAT_ID, user_id):
        tier = "chat_member"
    else:
        await relatives.ensure_fresh()
        tier = "relative" if relatives.contains(user_id) else "none"

    _cache[user_id] = (tier, time.time())
    return tier
