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

ASSETS_DIR = BASE_DIR / "assets"
TEETH_CHART_PATH = str(ASSETS_DIR / "teeth_chart.jpg")

# --- Модели ---
MODEL_SONNET = "claude-sonnet-5"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# --- Лимиты тест-драйва (константы, чтобы менять без переписывания кода) ---
LIMIT_RELATIONSHIP_SESSIONS = 3
LIMIT_RELATIONSHIP_MESSAGES = 40
LIMIT_TEETH_SESSIONS = 10
LIMIT_CONCEPT_MESSAGES = 30

# --- Ссылки и тексты ---
DIAGNOSTICS_POST_URL = "https://t.me/psy_teeth_official/439"
ROADMAP_URL = "https://psyteeth.github.io/hi/"
ADMIN_USERNAME = "@psyteeth"

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

RELATIONSHIP_LIMIT_TEXT = (
    "Три разбора в тест-драйве закончились. Хочешь продолжить — приходи на "
    f"бесплатную диагностику. Пиши {ADMIN_USERNAME}."
)
