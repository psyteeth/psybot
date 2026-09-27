"""Google Sheets: асинхронное логирование разговоров (запись, с повтором при
ошибке) и кэшированное чтение таблицы концепции (только чтение).

Сбой Google API не должен ронять бота — все методы логируют исключение и
возвращают, ничего не поднимая наверх.
"""
import asyncio
import json
import logging
import time
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from bot.config import (
    CONCEPT_REFRESH_SECONDS,
    CONCEPT_SPREADSHEET_ID,
    GOOGLE_SERVICE_ACCOUNT_JSON,
    LIMIT_OVERRIDES_REFRESH_SECONDS,
    LOG_SPREADSHEET_ID,
    TEST_USER_IDS,
    UNLIMITED,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

RELATIONSHIP_HEADER = [
    "timestamp_start", "user_id", "username", "номер_разбора",
    "A_событие", "B_нарратив_исходный", "B_нарратив_подтверждённый",
    "C_следствие", "D1_логическое", "D2_эмпирическое", "D3_прагматическое",
    "D4_гедонистический", "D5_шкала_катастроф", "D6_историческое",
    "D7_двойной_стандарт", "D8_семантическое", "E_итог", "шаг_выхода",
    "завершён", "число_сообщений", "timestamp_end",
    # ТЗ-доп. №2, раздел 2 — добавлены в конец, чтобы не сломать старые строки
    "событие_до", "кто_другой", "дискомфорт_до", "дискомфорт_после", "сдвиг",
    "отражение_перед_E",
    "тест",
]
TEETH_HEADER = [
    "timestamp", "user_id", "username", "номер_сессии", "номер_зуба",
    "самое_страшное", "как_себя_чувствует", "острые_симптомы", "завершена",
    "тест",
]
CONCEPT_HEADER = [
    "timestamp", "user_id", "username", "вопрос", "ответ", "спор", "передано_админу",
    "вопрос_уточнённый",
    "тест",
]
LIMITS_HEADER = ["timestamp", "user_id", "username", "ветка", "событие", "тест"]
HOSTILITY_HEADER = [
    "timestamp", "user_id", "username", "ветка", "шаг", "текст_выпада", "target",
    "confidence", "скрытая_потребность", "категория_панчлайна", "текст_ответа",
    "реакция_пользователя", "номер_выпада_в_сессии", "сессия_закрыта",
    "тест",
]

LIMIT_OVERRIDES_HEADER = ["user_id", "ветка", "лимит", "комментарий"]
LIMIT_OVERRIDES_TAB = "Лимиты (ручные)"

RELATIVES_HEADER = ["user_id", "комментарий"]
RELATIVES_TAB = "Родственники"

SHEET_TABS = {
    "Отношения": RELATIONSHIP_HEADER,
    "Зубы": TEETH_HEADER,
    "Концепция": CONCEPT_HEADER,
    "Лимиты": LIMITS_HEADER,
    "Выпады": HOSTILITY_HEADER,
    LIMIT_OVERRIDES_TAB: LIMIT_OVERRIDES_HEADER,
    RELATIVES_TAB: RELATIVES_HEADER,
}


def _build_client() -> Optional[gspread.Client]:
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON не задан — логирование в Sheets отключено")
        return None
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception:
        logger.exception("Не удалось инициализировать клиент Google Sheets")
        return None


def _test_flag(row: list) -> str:
    """user_id — вторая колонка (индекс 1) во всех пяти лог-листах."""
    try:
        return "да" if int(row[1]) in TEST_USER_IDS else "нет"
    except (IndexError, ValueError, TypeError):
        return "нет"


class SheetsLogger:
    def __init__(self) -> None:
        self._client = _build_client()
        self._ready = False

    def _ensure_tabs(self) -> None:
        if self._ready or not self._client or not LOG_SPREADSHEET_ID:
            return
        sh = self._client.open_by_key(LOG_SPREADSHEET_ID)
        existing = {ws.title for ws in sh.worksheets()}
        for title, header in SHEET_TABS.items():
            if title not in existing:
                ws = sh.add_worksheet(title=title, rows=1000, cols=len(header) + 2)
                ws.append_row(header)
        self._ready = True

    def _append_sync(self, tab: str, row: list) -> None:
        if not self._client or not LOG_SPREADSHEET_ID:
            logger.warning("Sheets не настроен, строка потеряна: tab=%s", tab)
            return
        last_err = None
        for attempt in range(3):
            try:
                self._ensure_tabs()
                sh = self._client.open_by_key(LOG_SPREADSHEET_ID)
                ws = sh.worksheet(tab)
                ws.append_row(row, value_input_option="USER_ENTERED")
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        logger.exception("Не удалось записать строку в Sheets (tab=%s): %s", tab, last_err)

    async def append(self, tab: str, row: list) -> None:
        try:
            await asyncio.to_thread(self._append_sync, tab, [*row, _test_flag(row)])
        except Exception:  # noqa: BLE001
            logger.exception("append() к Sheets упал, разговор продолжается без лога")


class ConceptStore:
    """Кэш таблицы концепции в памяти, обновление раз в час."""

    def __init__(self) -> None:
        self._client = _build_client()
        self._sheets: dict[str, str] = {}
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()

    def _refresh_sync(self) -> dict:
        if not self._client:
            return {}
        sh = self._client.open_by_key(CONCEPT_SPREADSHEET_ID)
        result = {}
        for ws in sh.worksheets():
            rows = ws.get_all_values()
            text = "\n".join(" | ".join(cell for cell in row if cell) for row in rows if any(row))
            result[ws.title] = text
        return result

    async def ensure_fresh(self) -> None:
        stale = (time.time() - self._last_refresh) > CONCEPT_REFRESH_SECONDS
        if self._sheets and not stale:
            return
        async with self._lock:
            stale = (time.time() - self._last_refresh) > CONCEPT_REFRESH_SECONDS
            if self._sheets and not stale:
                return
            try:
                data = await asyncio.to_thread(self._refresh_sync)
                if data:
                    self._sheets = data
                    self._last_refresh = time.time()
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось обновить таблицу концепции, используем старый кэш")

    def titles(self) -> list[str]:
        return list(self._sheets.keys())

    def main_narrative(self) -> str:
        for title in self._sheets:
            if "нарратив" in title.lower():
                return self._sheets[title]
        return next(iter(self._sheets.values()), "")

    def get(self, title: str) -> str:
        return self._sheets.get(title, "")

    def is_loaded(self) -> bool:
        return bool(self._sheets)


BRANCH_LABEL_TO_KEY = {
    "отношения": "relationships",
    "зубы": "teeth",
    "концепция": "concept",
}


class LimitOverridesStore:
    """Персональные лимиты из листа «Лимиты (ручные)» в LOG_SPREADSHEET_ID.

    Формат листа: user_id | ветка (Отношения/Зубы/Концепция) | лимит | комментарий.
    Админ правит таблицу руками, бот подхватывает изменения раз в
    LIMIT_OVERRIDES_REFRESH_SECONDS. Пустой/некорректный лист = нет переопределений,
    это НЕ ошибка (в отличие от ConceptStore, где пусто = отдельный сигнал сбоя)."""

    def __init__(self) -> None:
        self._client = _build_client()
        self._overrides: dict[tuple[int, str], int] = {}
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()

    def _refresh_sync(self) -> dict[tuple[int, str], int]:
        if not self._client or not LOG_SPREADSHEET_ID:
            return {}
        sh = self._client.open_by_key(LOG_SPREADSHEET_ID)
        try:
            ws = sh.worksheet(LIMIT_OVERRIDES_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(
                title=LIMIT_OVERRIDES_TAB, rows=500, cols=len(LIMIT_OVERRIDES_HEADER) + 1
            )
            ws.append_row(LIMIT_OVERRIDES_HEADER)
            return {}

        result: dict[tuple[int, str], float] = {}
        for record in ws.get_all_records():
            try:
                user_id = int(record.get("user_id"))
                branch_key = BRANCH_LABEL_TO_KEY.get(str(record.get("ветка", "")).strip().lower())
            except (TypeError, ValueError):
                continue
            if branch_key is None:
                continue
            raw_limit = str(record.get("лимит", "")).strip().lower()
            if raw_limit in ("безлимит", "unlimited", "инф", "inf", "∞"):
                limit = UNLIMITED
            else:
                try:
                    limit = int(raw_limit)
                except ValueError:
                    continue
            result[(user_id, branch_key)] = limit
        return result

    async def ensure_fresh(self) -> None:
        if (time.time() - self._last_refresh) <= LIMIT_OVERRIDES_REFRESH_SECONDS and self._last_refresh:
            return
        async with self._lock:
            if (time.time() - self._last_refresh) <= LIMIT_OVERRIDES_REFRESH_SECONDS and self._last_refresh:
                return
            try:
                self._overrides = await asyncio.to_thread(self._refresh_sync)
                self._last_refresh = time.time()
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось обновить лист ручных лимитов, используем старый кэш")

    def get(self, user_id: int, branch: str, default=None):
        return self._overrides.get((user_id, branch), default)

    def has_any(self, user_id: int) -> bool:
        return any(uid == user_id for uid, _branch in self._overrides)


class RelativesStore:
    """Список user_id «родственников» из листа «Родственники» — тариф не определить
    автоматически (нет членства в чате), поэтому это ручной список, админ вписывает id.
    Формат листа: user_id | комментарий."""

    def __init__(self) -> None:
        self._client = _build_client()
        self._ids: set[int] = set()
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()

    def _refresh_sync(self) -> set[int]:
        if not self._client or not LOG_SPREADSHEET_ID:
            return set()
        sh = self._client.open_by_key(LOG_SPREADSHEET_ID)
        try:
            ws = sh.worksheet(RELATIVES_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=RELATIVES_TAB, rows=500, cols=len(RELATIVES_HEADER) + 1)
            ws.append_row(RELATIVES_HEADER)
            return set()

        result: set[int] = set()
        for record in ws.get_all_records():
            try:
                result.add(int(record.get("user_id")))
            except (TypeError, ValueError):
                continue
        return result

    async def ensure_fresh(self) -> None:
        if (time.time() - self._last_refresh) <= LIMIT_OVERRIDES_REFRESH_SECONDS and self._last_refresh:
            return
        async with self._lock:
            if (time.time() - self._last_refresh) <= LIMIT_OVERRIDES_REFRESH_SECONDS and self._last_refresh:
                return
            try:
                self._ids = await asyncio.to_thread(self._refresh_sync)
                self._last_refresh = time.time()
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось обновить лист «Родственники», используем старый кэш")

    def contains(self, user_id: int) -> bool:
        return user_id in self._ids


sheets_logger = SheetsLogger()
concept_store = ConceptStore()
limit_overrides = LimitOverridesStore()
relatives = RelativesStore()
