"""ТЗ-доп. №6, часть 1: сводная «матричная» таблица — один столбец на разбор, одна
строка на фиксированный шаг. Строится ИЗ листа «Диалоги» (единый источник правды) в
момент закрытия разбора, отдельно для «Отношения» и «Зубы» (у «Концепция» нет
фиксированных шагов — только линейный лог).

Сбой Sheets API здесь не должен ронять бота — как и SheetsLogger, всё ловим и логируем.
"""
import asyncio
import logging
import time

import gspread

from bot.config import LOG_SPREADSHEET_ID
from bot.sheets import _build_client

logger = logging.getLogger(__name__)

RELATIONSHIP_MATRIX_TAB = "Диалоги (матрица)"
TEETH_MATRIX_TAB = "Диалоги (матрица) Зубы"

RELATIONSHIP_ROW_LABELS = [
    "username", "дата",
    "A — бот", "A — человек",
    "проверка на себя A — бот", "проверка на себя A — человек",
    "B — бот", "B — человек",
    "B сверка — бот", "B сверка — человек",
    "C — бот", "C — человек",
    "ответ «я бы…» — бот",
    "D1 — бот", "D1 — человек",
    "D2 — бот", "D2 — человек",
    "D3 — бот", "D3 — человек",
    "D4 — бот", "D4 — человек",
    "D5 — бот", "D5 — человек",
    "D6 — бот", "D6 — человек",
    "D7 — бот", "D7 — человек",
    "D8 — бот", "D8 — человек",
    "отражение перед E — бот",
    "проверка на себя B — бот", "проверка на себя B — человек",
    "E — бот", "E — человек",
    "избегание/интеграция — бот", "избегание/интеграция — человек",
    "финал — бот",
    "прочее",
]

TEETH_ROW_LABELS = [
    "username", "дата",
    "номер зуба", "самое страшное", "как себя чувствует",
    "финальный ответ", "острые симптомы",
    "прочее",
]

# шаг (как передан в _log_turn/_send) -> базовая метка строки без "— бот"/"— человек".
# Шаг, которого нет в этом словаре, или у которого нет соответствующей строки для данного
# "кто" (например, человек не отвечает под шагом "финал"), уходит в "прочее" — так и задумано.
RELATIONSHIP_STEP_TO_LABEL = {
    "A": "A",
    "проверка_A": "проверка на себя A",
    "B": "B",
    "проверка_B": "проверка на себя B",
    "B_confirm": "B сверка",
    "C": "C",
    "D_confirm": "ответ «я бы…»",
    "D1": "D1", "D2": "D2", "D3": "D3", "D4": "D4",
    "D5": "D5", "D6": "D6", "D7": "D7", "D8": "D8",
    "E_reflection": "отражение перед E",
    "E": "E",
    "exit_intent_check": "избегание/интеграция",
    "E_followup": "финал",
}

TEETH_STEP_TO_LABEL = {
    "ask_tooth": "номер зуба",
    "ask_scary": "самое страшное",
    "ask_feeling": "как себя чувствует",
    "final": "финальный ответ",
    "acute": "острые симптомы",
}


def _label_key(step: str, who: str, step_to_label: dict, row_labels: list) -> str:
    base = step_to_label.get(step)
    if not base:
        return "прочее"
    split_candidate = f"{base} — {who}"
    if split_candidate in row_labels:
        return split_candidate
    # «Зубы»: строки не разбиты на бот/человек — одна строка на тему (например «номер зуба»).
    if base in row_labels:
        return base
    return "прочее"


def aggregate_cells(dialogue_rows: list[dict], step_to_label: dict, row_labels: list) -> dict[str, str]:
    """dialogue_rows — записи листа «Диалоги» (get_all_records), уже отфильтрованные по
    одному session_id. Несколько реплик на одном шаге склеиваются переносом строки."""
    cells: dict[str, list[str]] = {}
    for r in dialogue_rows:
        step = str(r.get("шаг", "")).strip()
        who = str(r.get("кто", "")).strip()
        text = str(r.get("текст", "")).strip()
        if not text:
            continue
        key = _label_key(step, who, step_to_label, row_labels)
        # в «прочее» без шага реплика теряет происхождение (несколько разных шагов сваливаются
        # в одну ячейку) — помечаем [шаг] для трассируемости; в подписанных строках это и так
        # видно из самой метки строки, там не дублируем.
        entry = f"[{step}] {who}: {text}" if key == "прочее" else f"{who}: {text}"
        cells.setdefault(key, []).append(entry)
    return {k: "\n".join(v) for k, v in cells.items()}


def _ensure_matrix_tab_sync(client, tab: str, row_labels: list) -> "gspread.Worksheet":
    sh = client.open_by_key(LOG_SPREADSHEET_ID)
    try:
        ws = sh.worksheet(tab)
    except Exception:  # noqa: BLE001
        ws = sh.add_worksheet(title=tab, rows=len(row_labels) + 2, cols=26)
        ws.update([["разбор"], *[[label] for label in row_labels]], "A1")
        try:
            ws.freeze(cols=1)
            ws.format(f"A1:A{len(row_labels) + 1}", {"wrapStrategy": "WRAP"})
            ws.columns_auto_resize(0, 1)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось применить форматирование к листу матрицы %s", tab)
    return ws


def _append_session_column_sync(
    client, tab: str, row_labels: list, session_id: str, username: str, date: str, cells: dict[str, str]
) -> None:
    if not client or not LOG_SPREADSHEET_ID:
        logger.warning("Sheets не настроен, столбец матрицы потерян: tab=%s session_id=%s", tab, session_id)
        return
    last_err = None
    for attempt in range(3):
        try:
            ws = _ensure_matrix_tab_sync(client, tab, row_labels)
            header_row = ws.row_values(1)
            next_col = len(header_row) + 1  # колонка A — метки, значит первая сессия в B
            values = [[session_id], [username], [date]]
            for label in row_labels[2:]:  # первые два — username/дата, уже заполнены выше
                values.append([cells.get(label, "")])
            col_letter = gspread.utils.rowcol_to_a1(1, next_col).rstrip("123456789")
            ws.update(
                values, f"{col_letter}1:{col_letter}{len(row_labels) + 1}",
                value_input_option="USER_ENTERED",
            )
            try:
                ws.format(f"{col_letter}1:{col_letter}{len(row_labels) + 1}", {"wrapStrategy": "WRAP"})
                ws.columns_auto_resize(next_col - 1, next_col)
            except Exception:  # noqa: BLE001
                pass
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    logger.exception("Не удалось записать столбец матрицы (tab=%s, session_id=%s): %s", tab, session_id, last_err)


async def append_session_column(
    tab: str, row_labels: list, session_id: str, username: str, date: str, cells: dict[str, str]
) -> None:
    client = _build_client()
    try:
        await asyncio.to_thread(
            _append_session_column_sync, client, tab, row_labels, session_id, username, date, cells
        )
    except Exception:  # noqa: BLE001
        logger.exception("append_session_column() упал, матрица не обновлена (session_id=%s)", session_id)


async def build_relationship_matrix_column(session_id: str, username: str, date: str) -> None:
    """Читает все строки «Диалоги» для данного session_id и дописывает новый столбец
    в «Диалоги (матрица)»."""
    dialogue_rows = await _fetch_dialogue_rows(session_id)
    cells = aggregate_cells(dialogue_rows, RELATIONSHIP_STEP_TO_LABEL, RELATIONSHIP_ROW_LABELS)
    await append_session_column(RELATIONSHIP_MATRIX_TAB, RELATIONSHIP_ROW_LABELS, session_id, username, date, cells)


async def build_teeth_matrix_column(session_id: str, username: str, date: str) -> None:
    dialogue_rows = await _fetch_dialogue_rows(session_id)
    cells = aggregate_cells(dialogue_rows, TEETH_STEP_TO_LABEL, TEETH_ROW_LABELS)
    await append_session_column(TEETH_MATRIX_TAB, TEETH_ROW_LABELS, session_id, username, date, cells)


def _fetch_dialogue_rows_sync(session_id: str) -> list[dict]:
    client = _build_client()
    if not client or not LOG_SPREADSHEET_ID:
        return []
    try:
        sh = client.open_by_key(LOG_SPREADSHEET_ID)
        ws = sh.worksheet("Диалоги")
        records = ws.get_all_records()
        return [r for r in records if str(r.get("session_id", "")) == str(session_id)]
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось прочитать «Диалоги» для session_id=%s", session_id)
        return []


async def _fetch_dialogue_rows(session_id: str) -> list[dict]:
    return await asyncio.to_thread(_fetch_dialogue_rows_sync, session_id)
