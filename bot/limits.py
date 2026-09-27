"""Эффективные лимиты (помесячные): тариф по членству в Telegram-чатах
(bot/membership.py, см. config.TIER_DEFAULTS) — переопределяется персонально через лист
«Лимиты (ручные)» (bot/sheets.py::LimitOverridesStore), включая значение «безлимит»."""
from telegram import Bot

from bot import db, membership
from bot.config import TIER_DEFAULTS
from bot.sheets import limit_overrides

BRANCH_LABELS = {
    "relationships": "Отношения",
    "teeth": "Зубы",
    "concept": "Концепция",
}
BRANCH_COUNTERS = {
    "relationships": db.count_relationship_sessions_this_month,
    "teeth": db.count_teeth_sessions_this_month,
    "concept": db.get_concept_message_count,
}


async def get_tier(bot: Bot, user_id: int) -> str:
    return await membership.get_tier(bot, user_id)


async def get_limit(bot: Bot, user_id: int, branch: str) -> float:
    await limit_overrides.ensure_fresh()
    override = limit_overrides.get(user_id, branch, None)
    if override is not None:
        return override
    tier = await membership.get_tier(bot, user_id)
    return TIER_DEFAULTS[tier][branch]


async def remaining(bot: Bot, user_id: int, branch: str) -> float:
    limit = await get_limit(bot, user_id, branch)
    used = BRANCH_COUNTERS[branch](user_id)
    return max(0, limit - used)


async def has_access(bot: Bot, user_id: int) -> bool:
    """False только если тариф «none» И нет ни одного ручного переопределения лимита —
    значит человек не в чатах, не родственник и админ явно не дал доступ вручную."""
    tier = await membership.get_tier(bot, user_id)
    if tier != "none":
        return True
    await limit_overrides.ensure_fresh()
    return limit_overrides.has_any(user_id)


async def summary(bot: Bot, user_id: int) -> dict[str, dict]:
    """Для админ-команды /limits: полная раскладка по всем веткам."""
    result = {}
    for branch, label in BRANCH_LABELS.items():
        limit = await get_limit(bot, user_id, branch)
        used = BRANCH_COUNTERS[branch](user_id)
        result[branch] = {"label": label, "limit": limit, "used": used, "left": max(0, limit - used)}
    return result
