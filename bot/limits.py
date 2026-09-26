"""Эффективные лимиты тест-драйва: глобальный дефолт из config.py, но может быть
переопределён персонально через лист «Лимиты (ручные)» в Google-таблице логов
(см. bot/sheets.py::LimitOverridesStore) — админ вписывает user_id/ветку/лимит
руками, без правки кода."""
from bot import db
from bot.config import LIMIT_CONCEPT_MESSAGES, LIMIT_RELATIONSHIP_SESSIONS, LIMIT_TEETH_SESSIONS
from bot.sheets import limit_overrides

BRANCH_DEFAULTS = {
    "relationships": LIMIT_RELATIONSHIP_SESSIONS,
    "teeth": LIMIT_TEETH_SESSIONS,
    "concept": LIMIT_CONCEPT_MESSAGES,
}
BRANCH_LABELS = {
    "relationships": "Отношения",
    "teeth": "Зубы",
    "concept": "Концепция",
}
BRANCH_COUNTERS = {
    "relationships": db.count_relationship_sessions,
    "teeth": db.count_teeth_sessions,
    "concept": db.get_concept_message_count,
}


async def get_limit(user_id: int, branch: str) -> int:
    await limit_overrides.ensure_fresh()
    return limit_overrides.get(user_id, branch, BRANCH_DEFAULTS[branch])


async def remaining(user_id: int, branch: str) -> int:
    limit = await get_limit(user_id, branch)
    used = BRANCH_COUNTERS[branch](user_id)
    return max(0, limit - used)


async def summary(user_id: int) -> dict[str, dict]:
    """Для админ-команды /limits: полная раскладка по всем веткам."""
    result = {}
    for branch, label in BRANCH_LABELS.items():
        limit = await get_limit(user_id, branch)
        used = BRANCH_COUNTERS[branch](user_id)
        result[branch] = {"label": label, "limit": limit, "used": used, "left": max(0, limit - used)}
    return result
