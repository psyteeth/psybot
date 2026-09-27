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
    ("D4_гедонистический", "D4"), ("D5_шкала_катастроф", "D5"), ("D6_историческое", "D6"),
    ("D7_двойной_стандарт", "D7"), ("D8_семантическое", "D8"),
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

    return "\n".join(lines) if lines else "Данных пока нет (или все строки помечены как тест)."


async def compute_summary() -> str:
    try:
        return await asyncio.to_thread(_compute_sync)
    except Exception:  # noqa: BLE001
        logger.exception("compute_summary упал")
        return "Не удалось построить сводку — см. логи бота."
