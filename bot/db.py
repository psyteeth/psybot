"""SQLite: состояние сессий и счётчики лимитов. Простые синхронные вызовы —
файл на локальном диске VDS, объём маленький, блокировка event loop незаметна."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from bot.config import DB_PATH

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
    exit_step TEXT
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


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        try:
            conn.execute("ALTER TABLE concept_usage ADD COLUMN period TEXT")
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


# --- Ветка «Отношения» ---

def count_relationship_sessions(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM relationship_sessions WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row["c"]


def count_relationship_sessions_this_month(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM relationship_sessions WHERE user_id=? AND started_at>=?",
            (user_id, _month_start_iso()),
        ).fetchone()
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


def finish_relationship_session(session_id: int, exit_step: str, completed: bool) -> None:
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
