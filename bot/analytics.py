"""Аналитика воронки рекламы (ТЗ 30.09) — метки источника из /start, лог событий по ветке
«Отношения», агрегированная сводка и CSV-экспорт. Никаких внешних сервисов — всё в SQLite
(bot/db.py::events), никогда не роняет сценарий (тот же принцип, что и у sheets_logger.append)."""
import csv
import json
import logging
import re
import statistics
from collections import defaultdict
from pathlib import Path

from telegram.ext import ContextTypes

from bot import db
from bot.config import AB_TESTING_ENABLED, DATA_DIR

logger = logging.getLogger(__name__)

SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Порядок шагов воронки для /stats_funnel — от старта до клика по CTA.
FUNNEL_STEPS = [
    "start", "flow_started", "step_A_done", "step_B_done", "step_C_done",
    "step_D_done", "step_E_done", "cta_click",
]
FUNNEL_STEP_LABELS = {
    "start": "start", "flow_started": "flow_started", "step_A_done": "A", "step_B_done": "B",
    "step_C_done": "C", "step_D_done": "D", "step_E_done": "E", "cta_click": "cta_click",
}


def parse_source(args: list[str] | None) -> str:
    if not args:
        return "organic"
    payload = args[0]
    return payload if SOURCE_RE.match(payload) else "invalid"


async def log(context: ContextTypes.DEFAULT_TYPE, user_id: int, event: str, meta: dict | None = None) -> None:
    """Никогда не поднимает исключение наверх — сбой лога не должен рвать сценарий (прямое
    требование ТЗ, тот же принцип, что и sheets_logger.append)."""
    try:
        source = db.get_last_source(user_id)
        meta = dict(meta or {})
        if AB_TESTING_ENABLED and "ab_variant" not in meta:
            meta["ab_variant"] = db.get_or_assign_ab_variant(user_id)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        db.log_event(user_id, source, event, meta_json)
    except Exception:  # noqa: BLE001
        logger.exception("analytics.log упал (event=%s, user_id=%s), сценарий продолжается", event, user_id)


def _fetch_events(days: int) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, user_id, source, event, meta FROM events "
            "WHERE ts >= datetime('now', ?) ORDER BY ts",
            (f"-{days} days",),
        ).fetchall()
    out = []
    for r in rows:
        meta = {}
        if r["meta"]:
            try:
                meta = json.loads(r["meta"])
            except (ValueError, TypeError):
                meta = {}
        out.append({"ts": r["ts"], "user_id": r["user_id"], "source": r["source"], "event": r["event"], "meta": meta})
    return out


def _funnel_table(rows: list[dict], group_key) -> str:
    """group_key(row) -> str — группирует по источнику или по ab_variant."""
    groups: dict[str, dict[str, set]] = defaultdict(lambda: {step: set() for step in FUNNEL_STEPS})
    for r in rows:
        if r["event"] not in FUNNEL_STEPS:
            continue
        key = group_key(r)
        if key is None:
            continue
        groups[key][r["event"]].add(r["user_id"])

    lines = []
    for key in sorted(groups):
        counts = groups[key]
        start_n = len(counts["start"]) or 1
        parts = []
        for step in FUNNEL_STEPS:
            n = len(counts[step])
            pct = round(100 * n / start_n) if counts["start"] else 0
            parts.append(f"{FUNNEL_STEP_LABELS[step]}={n} ({pct}%)")
        lines.append(f"{key}: " + ", ".join(parts))
    return "\n".join(lines) if lines else "(нет данных)"


def _median_time_to_d(rows: list[dict]) -> str:
    starts: dict[int, str] = {}
    d_done: dict[int, str] = {}
    for r in rows:
        if r["event"] == "start" and r["user_id"] not in starts:
            starts[r["user_id"]] = r["ts"]
        if r["event"] == "step_D_done" and r["user_id"] not in d_done:
            d_done[r["user_id"]] = r["ts"]

    from datetime import datetime

    deltas = []
    for uid, d_ts in d_done.items():
        if uid not in starts:
            continue
        try:
            t0 = datetime.fromisoformat(starts[uid])
            t1 = datetime.fromisoformat(d_ts)
            deltas.append((t1 - t0).total_seconds())
        except ValueError:
            continue
    if not deltas:
        return "нет завершивших D за период"
    return f"{round(statistics.median(deltas) / 60, 1)} мин (n={len(deltas)})"


def _feedback_distribution(rows: list[dict]) -> str:
    dist: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["event"] == "feedback":
            dist[str(r["meta"].get("value", "?"))] += 1
    if not dist:
        return "нет ответов"
    return ", ".join(f"{k}: {v}" for k, v in sorted(dist.items(), key=lambda x: -x[1]))


async def compute_funnel_stats(days: int) -> str:
    try:
        rows = _fetch_events(days)
    except Exception:  # noqa: BLE001
        logger.exception("compute_funnel_stats упал")
        return "Не удалось построить сводку — см. логи бота."

    lines = [f"Воронка за последние {days} дн.\n"]
    lines.append("По источнику:")
    lines.append(_funnel_table(rows, lambda r: r["source"]))
    lines.append("\nПо A/B-варианту:")
    lines.append(_funnel_table(rows, lambda r: r["meta"].get("ab_variant")))
    lines.append(f"\nМедианное время start → D: {_median_time_to_d(rows)}")
    lines.append(f"Отклики «стало легче»: {_feedback_distribution(rows)}")
    return "\n".join(lines)


def export_csv(days: int | None = None) -> str:
    rows = _fetch_events(days) if days else _fetch_events(36500)  # без ограничения по факту
    path = Path(DATA_DIR) / "events_export.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "user_id", "source", "event", "meta"])
        for r in rows:
            writer.writerow([r["ts"], r["user_id"], r["source"], r["event"], json.dumps(r["meta"], ensure_ascii=False)])
    return str(path)
