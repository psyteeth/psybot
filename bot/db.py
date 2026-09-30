"""SQLite: состояние сессий и счётчики лимитов. Простые синхронные вызовы —
файл на локальном диске VDS, объём маленький, блокировка event loop незаметна."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from bot.config import COUNT_CRISIS_TOWARD_LIMIT, COUNT_SELF_REFUSED_TOWARD_LIMIT, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT
);

CREATE TABLE IF NOT EXISTS relationship_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    session_num INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    step TEXT NOT NULL DEFAULT 'A',
    message_count INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    a_event TEXT,
    b_narrative_raw TEXT,
    b_narrative_confirmed TEXT,
    c_consequence TEXT,
    d1_logical TEXT,
    d2_empirical TEXT,
    d3_pragmatic TEXT,
    d4_hedonistic TEXT,
    d5_catastrophe_scale TEXT,
    d6_historical TEXT,
    d7_double_standard TEXT,
    d8_semantic TEXT,
    e_summary TEXT,
    exit_step TEXT,
    event_before TEXT,
    other_person TEXT,
    discomfort_before INTEGER,
    discomfort_after INTEGER,
    reflection_before_e TEXT,
    self_request INTEGER NOT NULL DEFAULT 0,
    self_check_step TEXT,
    self_original_answer TEXT,
    self_reformulated INTEGER NOT NULL DEFAULT 0,
    self_refused INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    d5_comment TEXT,
    d5_original INTEGER,
    exit_intent INTEGER NOT NULL DEFAULT 0,
    exit_intent_step TEXT,
    avoidance_or_integration TEXT,
    exit_intent_answer TEXT,
    attempts_a INTEGER NOT NULL DEFAULT 0,
    attempts_b INTEGER NOT NULL DEFAULT 0,
    dozhim_outcome TEXT
);

CREATE TABLE IF NOT EXISTS teeth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    session_num INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    tooth_number INTEGER,
    scary_thing TEXT,
    feeling_word TEXT,
    acute_symptoms INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS concept_usage (
    user_id INTEGER PRIMARY KEY,
    message_count INTEGER NOT NULL DEFAULT 0,
    period TEXT
);

CREATE TABLE IF NOT EXISTS limit_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    branch TEXT NOT NULL,
    ts TEXT NOT NULL
);

-- «Секреты из таблицы» (мини-ТЗ 29.09) — какие chunk_id уже показаны какому пользователю,
-- чтобы никогда не повторять один и тот же секрет одному человеку (пожизненно, не только
-- в рамках сессии).
CREATE TABLE IF NOT EXISTS shown_secrets (
    user_id INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    shown_at TEXT NOT NULL,
    PRIMARY KEY (user_id, chunk_id)
);

-- Аналитика воронки рекламы (ТЗ 30.09) — лог событий по всей ветке «Отношения», от /start с
-- меткой источника до клика по CTA. Текст пользователя сюда никогда не пишется, только факт
-- события + длина ответа в meta.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    event TEXT NOT NULL,
    meta TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _month_start_iso() -> str:
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat()


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


MIGRATIONS = [
    "ALTER TABLE concept_usage ADD COLUMN period TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN event_before TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN other_person TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN discomfort_before INTEGER",
    "ALTER TABLE relationship_sessions ADD COLUMN discomfort_after INTEGER",
    "ALTER TABLE relationship_sessions ADD COLUMN reflection_before_e TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN self_request INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE relationship_sessions ADD COLUMN self_check_step TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN self_original_answer TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN self_reformulated INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE relationship_sessions ADD COLUMN self_refused INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE relationship_sessions ADD COLUMN note TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN d5_comment TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN d5_original INTEGER",
    "ALTER TABLE relationship_sessions ADD COLUMN exit_intent INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE relationship_sessions ADD COLUMN exit_intent_step TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN avoidance_or_integration TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN exit_intent_answer TEXT",
    "ALTER TABLE relationship_sessions ADD COLUMN attempts_a INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE relationship_sessions ADD COLUMN attempts_b INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE relationship_sessions ADD COLUMN dozhim_outcome TEXT",
    "ALTER TABLE users ADD COLUMN first_source TEXT",
    "ALTER TABLE users ADD COLUMN last_source TEXT",
    "ALTER TABLE users ADD COLUMN ab_variant TEXT",
]


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # колонка уже есть (миграция на уже существующей базе)


# --- Пользователи ---

def upsert_user(user_id: int, username: Optional[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username),
        )


# --- Аналитика воронки рекламы (ТЗ 30.09) ---

def log_event(user_id: int, source: str, event: str, meta: Optional[str]) -> None:
    """meta — уже сериализованная JSON-строка (или None); сериализацию делает bot/analytics.py,
    здесь только запись."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO events (ts, user_id, source, event, meta) VALUES (?, ?, ?, ?, ?)",
            (now(), user_id, source, event, meta),
        )


def set_source(user_id: int, source: str) -> None:
    """last_source всегда обновляется; first_source пишется только один раз (COALESCE)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET first_source = COALESCE(first_source, ?), last_source = ? "
            "WHERE user_id = ?",
            (source, source, user_id),
        )


def get_last_source(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT last_source FROM users WHERE user_id=?", (user_id,)).fetchone()
    return (row["last_source"] if row and row["last_source"] else "organic")


def get_or_assign_ab_variant(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT ab_variant FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row and row["ab_variant"]:
            return row["ab_variant"]
        variant = "direct" if hash(user_id) % 2 == 0 else "intro"
        conn.execute("UPDATE users SET ab_variant=? WHERE user_id=?", (variant, user_id))
    return variant


# --- Ветка «Отношения» ---

def count_relationship_sessions(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM relationship_sessions WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row["c"]


def count_relationship_sessions_this_month(user_id: int) -> int:
    query = "SELECT COUNT(*) AS c FROM relationship_sessions WHERE user_id=? AND started_at>=?"
    params: list = [user_id, _month_start_iso()]
    if not COUNT_SELF_REFUSED_TOWARD_LIMIT:
        # разбор, закрытый отказом («запрос на себя», ТЗ-доп. №3) по сути не начался —
        # не должен списываться из месячного лимита.
        query += " AND self_refused=0"
    if not COUNT_CRISIS_TOWARD_LIMIT:
        # разбор, закрытый по проверке суицид/самоповреждение (ТЗ-доп. №4) — та же логика.
        query += " AND (exit_step IS NULL OR exit_step != 'crisis')"
    with get_conn() as conn:
        row = conn.execute(query, params).fetchone()
        return row["c"]


def create_relationship_session(user_id: int, username: Optional[str]) -> int:
    session_num = count_relationship_sessions(user_id) + 1
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO relationship_sessions (user_id, username, session_num, started_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, username, session_num, now()),
        )
        return cur.lastrowid


def update_relationship_session(session_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE relationship_sessions SET {cols} WHERE id=?",
            (*fields.values(), session_id),
        )


def increment_relationship_messages(session_id: int) -> int:
    with get_conn() as conn:
        conn.execute(
            "UPDATE relationship_sessions SET message_count = message_count + 1 "
            "WHERE id=?",
            (session_id,),
        )
        row = conn.execute(
            "SELECT message_count FROM relationship_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        return row["message_count"]


def get_relationship_session(session_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM relationship_sessions WHERE id=?", (session_id,)
        ).fetchone()


RELATIONSHIP_REQUIRED_FIELDS = [
    "a_event", "b_narrative_confirmed", "c_consequence",
    "d1_logical", "d2_empirical", "d3_pragmatic", "d4_hedonistic",
    "d5_catastrophe_scale", "d6_historical", "d7_double_standard", "d8_semantic",
    "e_summary",
]


def finish_relationship_session(session_id: int, exit_step: str) -> None:
    """completed вычисляется ЗДЕСЬ из реального содержимого полей, а не принимается
    аргументом — раньше вызывающий код мог передать completed=True, даже если A-D
    не были заполнены (например, разбор форсированно свернули после 40 сообщений)."""
    row = get_relationship_session(session_id)
    completed = all(row[f] for f in RELATIONSHIP_REQUIRED_FIELDS) if row else False
    update_relationship_session(
        session_id,
        ended_at=now(),
        exit_step=exit_step,
        completed=1 if completed else 0,
    )


# --- Ветка «Зубы» ---

def count_teeth_sessions(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM teeth_sessions WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["c"]


def count_teeth_sessions_this_month(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM teeth_sessions WHERE user_id=? AND started_at>=?",
            (user_id, _month_start_iso()),
        ).fetchone()
        return row["c"]


def create_teeth_session(user_id: int, username: Optional[str]) -> int:
    session_num = count_teeth_sessions(user_id) + 1
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO teeth_sessions (user_id, username, session_num, started_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, username, session_num, now()),
        )
        return cur.lastrowid


def get_teeth_session(session_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM teeth_sessions WHERE id=?", (session_id,)).fetchone()


def update_teeth_session(session_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE teeth_sessions SET {cols} WHERE id=?",
            (*fields.values(), session_id),
        )


def finish_teeth_session(session_id: int, completed: bool) -> None:
    update_teeth_session(session_id, ended_at=now(), completed=1 if completed else 0)


# --- Ветка «Концепция» ---

def get_concept_message_count(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT message_count, period FROM concept_usage WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row or row["period"] != _current_period():
        return 0
    return row["message_count"]


def increment_concept_messages(user_id: int) -> int:
    period = _current_period()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT message_count, period FROM concept_usage WHERE user_id=?", (user_id,)
        ).fetchone()
        if row and row["period"] == period:
            conn.execute(
                "UPDATE concept_usage SET message_count = message_count + 1 WHERE user_id=?",
                (user_id,),
            )
            return row["message_count"] + 1
        conn.execute(
            "INSERT INTO concept_usage (user_id, message_count, period) VALUES (?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET message_count=1, period=excluded.period",
            (user_id, period),
        )
        return 1


# --- Лимиты ---

def log_limit_hit(user_id: int, branch: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO limit_hits (user_id, branch, ts) VALUES (?, ?, ?)",
            (user_id, branch, now()),
        )


# --- «Секреты из таблицы» (мини-ТЗ 29.09) ---

def get_seen_secret_ids(user_id: int) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM shown_secrets WHERE user_id=?", (user_id,)
        ).fetchall()
    return {r["chunk_id"] for r in rows}


def record_secret_shown(user_id: int, chunk_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO shown_secrets (user_id, chunk_id, shown_at) VALUES (?, ?, ?)",
            (user_id, chunk_id, now()),
        )
