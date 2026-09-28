"""Конфиг бота. Все секреты — только из переменных окружения."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
LOG_SPREADSHEET_ID = os.environ.get("LOG_SPREADSHEET_ID", "")
CONCEPT_SPREADSHEET_ID = os.environ.get(
    "CONCEPT_SPREADSHEET_ID", "1rKTcsHrcMygqBqwT89uPzSIJ5TPOYpVvuNSf9gc2Xlc"
)
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = str(DATA_DIR / "bot.db")
PERSISTENCE_PATH = str(DATA_DIR / "bot_persistence.pickle")

ASSETS_DIR = BASE_DIR / "assets"
TEETH_CHART_PATH = str(ASSETS_DIR / "teeth_chart.jpg")

# --- Модели ---
MODEL_SONNET = "claude-sonnet-5"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# --- Лимит сообщений внутри ОДНОГО разбора «Отношения» (не помесячный) ---
LIMIT_RELATIONSHIP_MESSAGES = 40

# --- Ссылки и тексты ---
DIAGNOSTICS_POST_URL = "https://t.me/psy_teeth_official/439"
ROADMAP_URL = "https://psyteeth.github.io/hi/"
TESTS_URL = "https://t.me/psy_teeth_official/701"
ADMIN_USERNAME = "@psyteeth"
CONCEPT_CHAT_USERNAME = "@psy_teeth"

# --- Доступ по чатам: тариф определяется членством в Telegram-чатах, лимиты помесячные ---
# «Чат исцеления отношений» — рабочий проект автора, участники получают безлимит.
# Приватный чат без публичного @username — id узнать через /chatid (см. menu.py),
# вписать в .env после того, как бота добавят туда участником.
RELATIONSHIPS_CHAT_ID = os.environ.get("RELATIONSHIPS_CHAT_ID", "")
# «Чат психостоматологии» — публичный чат, тот же, куда бот шлёт офф-топик редиректы.
PSYTEETH_CHAT_ID = os.environ.get("PSYTEETH_CHAT_ID", "") or CONCEPT_CHAT_USERNAME
MEMBERSHIP_CACHE_SECONDS = 10 * 60  # не дёргать getChatMember на каждое сообщение

UNLIMITED = float("inf")

TIER_DEFAULTS = {
    # участник «чата исцеления отношений» — безлимит везде
    "chat_unlimited": {"relationships": UNLIMITED, "teeth": UNLIMITED, "concept": UNLIMITED},
    # родственник участника — ручной список в листе «Родственники»
    "relative": {"relationships": 10, "teeth": 10, "concept": 30},
    # участник публичного чата психостоматологии
    "chat_member": {"relationships": 10, "teeth": 30, "concept": 50},
    # ни в одном чате и не в списке родственников — бот не работает
    "none": {"relationships": 0, "teeth": 0, "concept": 0},
}

NO_ACCESS_TEXT = (
    "Бот сейчас доступен только участникам чата психостоматологии. Чтобы получить доступ, напиши "
    f"{ADMIN_USERNAME} — добавят в чат {CONCEPT_CHAT_USERNAME}, и бот заработает."
)

VALID_TEETH_NUMBERS = {
    n
    for quadrant in (range(11, 19), range(21, 29), range(31, 39), range(41, 49))
    for n in quadrant
}

CONCEPT_REFRESH_SECONDS = 60 * 60  # раз в час
LIMIT_OVERRIDES_REFRESH_SECONDS = 5 * 60  # раз в 5 минут — ручные лимиты должны подхватываться быстро

# --- Модуль «Скрытая потребность + SET-панчлайн» ---
HOSTILITY_CONFIDENCE_THRESHOLD = 0.7
HOSTILITY_MAX_PUNCHLINES = 3

# --- Объединение подряд идущих сообщений перед обработкой шага ---
DEBOUNCE_SECONDS = 2.5

# --- Тестовые user_id: помечаются "тест=да" в логах, /stats их не считает ---
TEST_USER_IDS = {8001, 9001, 1234, 333, 444, 555, 0}

# --- Ветка «Отношения»: новые поля (ТЗ-доп. №2) ---
# Выключатель по умолчанию — вопрос "что было до этого?" после шага A.
ENABLE_PRIOR_EVENT_QUESTION = False

OTHER_PERSON_OPTIONS = [
    "муж", "жена", "партнёр", "мама", "папа", "ребёнок",
    "родственник", "начальник", "коллега", "друг", "другое",
]

# --- Ветка «Отношения»: запрос «проблема во мне» (ТЗ-доп. №3) ---
# unclear считается self, если confidence классификатора ниже порога.
SELF_TARGET_CONFIDENCE_THRESHOLD = 0.7
# Сколько раз даём переформулировать через другого человека, прежде чем отказать.
SELF_TARGET_MAX_ATTEMPTS = 1
# Разбор, закрытый отказом (человек настоял, что проблема в нём) — по сути не начался,
# по умолчанию НЕ считается в месячный лимит.
COUNT_SELF_REFUSED_TOWARD_LIMIT = False

# --- /stats: часовой пояс автосводки для админа (понедельник 10:00 Бангкок = 03:00 UTC) ---
STATS_WEEKLY_WEEKDAY = 0  # 0 = понедельник (Monday в датах JobQueue)
STATS_WEEKLY_HOUR_UTC = 3
STATS_WEEKLY_MINUTE_UTC = 0


def limit_exhausted_text(branch_label: str) -> str:
    return (
        f"Лимит по «{branch_label}» на этот месяц закончился. Обновится в начале следующего "
        f"месяца. Если нужно больше — пиши {ADMIN_USERNAME}."
    )


RELATIONSHIP_LIMIT_TEXT = limit_exhausted_text("Отношения")
