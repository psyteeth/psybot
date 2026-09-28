"""Сводка для /stats и еженедельной рассылки админу: агрегация по листам
таблицы логов, учитываются только строки с тест != "да"."""
import asyncio
import logging
import statistics

from bot.config import LOG_SPREADSHEET_ID
from bot.sheets import _build_client

logger = logging.getLogger(__name__)

D_COLUMNS = [
    ("D1_логическое", "D1"), ("D2_эмпирическое", "D2"), ("D3_прагматическое", "D3"),
    ("D4_гедонистический", "D4"), ("D6_историческое", "D6"),
    ("D7_двойной_стандарт", "D7"), ("D8_семантическое", "D8"),
    # D5 (шкала катастроф) — теперь число, средняя длина ответа в словах для него
    # бессмысленна (ТЗ-доп. №5).
]


def _real_rows(records: list[dict]) -> list[dict]:
    return [r for r in records if str(r.get("тест", "")).strip().lower() != "да"]


def _numbers(rows: list[dict], col: str) -> list[int]:
    out = []
    for r in rows:
        v = str(r.get(col, "")).strip()
        if v.lstrip("-").isdigit():
            out.append(int(v))
    return out


def _distribution(rows: list[dict], col: str) -> dict[str, int]:
    """r.get(col) может прийти int/float — gspread типизирует ячейки по содержимому,
    а не только по заголовку, поэтому всегда приводим к str перед .strip()."""
    dist: dict[str, int] = {}
    for r in rows:
        v = str(r.get(col) or "").strip()
        if v:
            dist[v] = dist.get(v, 0) + 1
    return dist


def _fmt_dist(dist: dict[str, int]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(dist.items(), key=lambda x: -x[1]))


def _compute_sync() -> str:
    client = _build_client()
    if not client or not LOG_SPREADSHEET_ID:
        return "Sheets не настроен — сводка недоступна."
    sh = client.open_by_key(LOG_SPREADSHEET_ID)

    rel = _real_rows(sh.worksheet("Отношения").get_all_records())
    limit_rows = _real_rows(sh.worksheet("Лимиты").get_all_records())
    hostility_rows = _real_rows(sh.worksheet("Выпады").get_all_records())

    lines = []

    total = len(rel)
    completed_rows = [r for r in rel if str(r.get("завершён", "")).strip().lower() == "да"]
    completed = len(completed_rows)
    pct = round(100 * completed / total) if total else 0
    lines.append(f"Отношения: начато {total}, завершено {completed} ({pct}%)")

    exit_dist = _distribution(
        [r for r in rel if str(r.get("завершён", "")).strip().lower() != "да"], "шаг_выхода"
    )
    if exit_dist:
        lines.append(f"Уходят на шаге: {_fmt_dist(exit_dist)}")

    before = _numbers(rel, "дискомфорт_до")
    after = _numbers(rel, "дискомфорт_после")
    shifts = _numbers(rel, "сдвиг")
    if before:
        lines.append(f"Дискомфорт до: {round(statistics.mean(before), 1)}")
    if after:
        lines.append(f"Дискомфорт после: {round(statistics.mean(after), 1)}")
    if shifts:
        share_2 = round(100 * sum(1 for s in shifts if s >= 2) / len(shifts))
        lines.append(f"Средний сдвиг: {round(statistics.mean(shifts), 1)}, сдвиг ≥2: {share_2}%")

    other_dist = _distribution(rel, "кто_другой")
    if other_dist:
        lines.append(f"Кто другой: {_fmt_dist(other_dist)}")

    d_avgs = []
    for col, label in D_COLUMNS:
        word_lens = [len(str(r.get(col, "")).split()) for r in rel if r.get(col)]
        if word_lens:
            d_avgs.append(f"{label}: {round(statistics.mean(word_lens), 1)} сл.")
    if d_avgs:
        lines.append("Длина ответов: " + ", ".join(d_avgs))

    if limit_rows:
        lines.append(f"Упёрлись в лимит: {len(limit_rows)}")

    if hostility_rows:
        cat_dist = _distribution(hostility_rows, "категория_панчлайна")
        suffix = f" ({_fmt_dist(cat_dist)})" if cat_dist else ""
        lines.append(f"Выпадов: {len(hostility_rows)}{suffix}")

    # ТЗ-доп. №6, ч.2 — дожим конкретного ответа на A/B (заменяет старый self_target-блок,
    # который больше ничего не пишет: колонки запрос_на_себя/переформулирован остались
    # только в старых строках).
    dozhim_dist = _distribution(rel, "исход_проверки")
    if dozhim_dist:
        lines.append(f"Дожим A/B (исход): {_fmt_dist(dozhim_dist)}")
    dozhim_attempts = [a for a in (_numbers(rel, "попытки_A") + _numbers(rel, "попытки_B")) if a > 0]
    if dozhim_attempts:
        lines.append(
            f"Дожим: потребовалась доп. попытка в {len(dozhim_attempts)} шагах A/B, среднее "
            f"число попыток при этом {round(statistics.mean(dozhim_attempts), 1)}"
        )

    exit_intent_dist = _distribution(rel, "избегание_или_интеграция")
    if exit_intent_dist:
        exit_intent_count = sum(1 for r in rel if str(r.get("выход_из_контакта", "")).strip().lower() == "да")
        lines.append(f"Выход из контакта: сработал {exit_intent_count} раз ({_fmt_dist(exit_intent_dist)})")

    return "\n".join(lines) if lines else "Данных пока нет (или все строки помечены как тест)."


async def compute_summary() -> str:
    try:
        return await asyncio.to_thread(_compute_sync)
    except Exception:  # noqa: BLE001
        logger.exception("compute_summary упал")
        return "Не удалось построить сводку — см. логи бота."


def _render_dialog_sync(query_arg: str) -> str:
    """ТЗ-доп. №6, ч.1 — /dialog <session_id или username>: полный текст сессии из «Диалоги».
    Если query_arg совпадает с session_id — берём её. Иначе ищем последнюю сессию с таким
    username (без учёта регистра)."""
    client = _build_client()
    if not client or not LOG_SPREADSHEET_ID:
        return ""
    sh = client.open_by_key(LOG_SPREADSHEET_ID)
    records = sh.worksheet("Диалоги").get_all_records()
    if not records:
        return ""

    session_ids = {str(r.get("session_id", "")) for r in records}
    if query_arg in session_ids:
        target_session_id = query_arg
    else:
        matching = [
            r for r in records
            if str(r.get("username", "")).strip().lower() == query_arg.strip().lower()
        ]
        if not matching:
            return ""
        matching.sort(key=lambda r: str(r.get("timestamp", "")))
        target_session_id = str(matching[-1].get("session_id", ""))
        if not target_session_id:
            return ""

    session_rows = [r for r in records if str(r.get("session_id", "")) == target_session_id]
    if not session_rows:
        return ""
    session_rows.sort(key=lambda r: str(r.get("timestamp", "")))

    lines = [f"session_id: {target_session_id}"]
    for r in session_rows:
        who = str(r.get("кто", ""))
        step = str(r.get("шаг", ""))
        text = str(r.get("текст", ""))
        lines.append(f"[{step}] {who}: {text}")
    return "\n".join(lines)


async def render_dialog(query_arg: str) -> str:
    try:
        return await asyncio.to_thread(_render_dialog_sync, query_arg)
    except Exception:  # noqa: BLE001
        logger.exception("render_dialog упал")
        return ""
