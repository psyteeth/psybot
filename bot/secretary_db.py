"""SQLite-состояние «Секретаря созвонов»: слоты дня, материалы, черновики строк, вопросы Антону.

Живёт в той же bot.db (bot/db.py::get_conn). Имена участников из таблицы эфиров хранятся только
здесь, на сервере, и в сообщениях Антону — в логи не пишутся. Долговременный источник «уже
обработано» — лист «Журнал_секретаря» в таблице участников; база — рабочее состояние дня."""
import json
from typing import Optional

from bot.db import get_conn, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS sec_days (
    day TEXT PRIMARY KEY,
    list_msg_id INTEGER,
    reminded_3h INTEGER NOT NULL DEFAULT 0,
    reminded_morning INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sec_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    time_msk TEXT NOT NULL,
    name TEXT NOT NULL,
    trainer TEXT,
    part_row INTEGER,
    nick TEXT,
    match TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL DEFAULT 'pending',
    msg_id INTEGER,
    UNIQUE(day, time_msk, name)
);
CREATE TABLE IF NOT EXISTS sec_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    ref TEXT NOT NULL,
    text TEXT NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(slot_id, source, ref)
);
CREATE TABLE IF NOT EXISTS sec_chat_posts (
    chat_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    author_id INTEGER,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS sec_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    assigned_slot INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sec_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL,
    sheet TEXT NOT NULL,
    row_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    msg_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sec_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    msg_id INTEGER,
    answer TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sec_aliases (
    name_norm TEXT PRIMARY KEY,
    part_row INTEGER,
    nick TEXT
);
"""

FINAL_STATUSES = ("added", "declined", "no_call", "skipped")


MIGRATIONS = [
    "ALTER TABLE sec_slots ADD COLUMN report_msg_id INTEGER",  # сообщение-история по участнику
]


def init() -> None:
    import sqlite3
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # колонка уже есть


def slot_by_report_msg(msg_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_slots WHERE report_msg_id = ?", (msg_id,)).fetchone()


# --- дни ---

def ensure_day(day: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO sec_days (day, created_at) VALUES (?, ?)", (day, now()))


def get_day(day: str):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_days WHERE day = ?", (day,)).fetchone()


def update_day(day: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE sec_days SET {cols} WHERE day = ?", (*fields.values(), day))


def day_by_list_msg(msg_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_days WHERE list_msg_id = ?", (msg_id,)).fetchone()


# --- слоты ---

def upsert_slot(day: str, time_msk: str, name: str, trainer: str) -> int:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sec_slots (day, time_msk, name, trainer) VALUES (?, ?, ?, ?)",
            (day, time_msk, name, trainer),
        )
        return conn.execute(
            "SELECT id FROM sec_slots WHERE day = ? AND time_msk = ? AND name = ?", (day, time_msk, name)
        ).fetchone()["id"]


def get_slot(slot_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_slots WHERE id = ?", (slot_id,)).fetchone()


def slots_of_day(day: str) -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_slots WHERE day = ? ORDER BY time_msk, id", (day,)).fetchall()


def drop_stale_slots(day: str, keep: set[int]) -> int:
    """Удаляет слоты дня не из keep, если у них нет материала, черновиков и вопросов."""
    n = 0
    with get_conn() as conn:
        for r in conn.execute("SELECT id FROM sec_slots WHERE day = ? AND status = 'pending'", (day,)).fetchall():
            sid = r["id"]
            if sid in keep:
                continue
            busy = any(
                conn.execute(f"SELECT 1 FROM {t} WHERE slot_id = ? LIMIT 1", (sid,)).fetchone()
                for t in ("sec_materials", "sec_drafts", "sec_questions")
            )
            if not busy:
                conn.execute("DELETE FROM sec_slots WHERE id = ?", (sid,))
                n += 1
    return n


def update_slot(slot_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE sec_slots SET {cols} WHERE id = ?", (*fields.values(), slot_id))


def slot_by_msg(msg_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_slots WHERE msg_id = ?", (msg_id,)).fetchone()


# --- материалы ---

def add_material(slot_id: int, source: str, ref: str, text: str, meta: Optional[dict] = None) -> bool:
    """True — материал новый. Повторная загрузка той же расшифровки/поста не дублируется."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sec_materials (slot_id, source, ref, text, meta, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (slot_id, source, str(ref), text, json.dumps(meta or {}, ensure_ascii=False), now()),
        )
        return cur.rowcount > 0


def materials_of_slot(slot_id: int) -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_materials WHERE slot_id = ? ORDER BY id", (slot_id,)).fetchall()


# --- посты тренеров в чате ---

def add_chat_post(chat_id: int, msg_id: int, day: str, author_id: int, text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sec_chat_posts (chat_id, msg_id, day, author_id, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, msg_id, day, author_id, text, now()),
        )


def chat_posts_of_day(day: str) -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_chat_posts WHERE day = ? ORDER BY msg_id", (day,)).fetchall()


# --- присланное Антоном без адресата ---

def add_inbox(day: str, msg_id: int, text: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sec_inbox (day, msg_id, text, created_at) VALUES (?, ?, ?, ?)", (day, msg_id, text, now())
        )
        return cur.lastrowid


def get_inbox(inbox_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_inbox WHERE id = ?", (inbox_id,)).fetchone()


def assign_inbox(inbox_id: int, slot_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sec_inbox SET assigned_slot = ? WHERE id = ?", (slot_id, inbox_id))


# --- черновики строк ---

def replace_drafts(slot_id: int, drafts: list[tuple[str, dict]]) -> None:
    """Новый разбор слота заменяет его неотправленные черновики; записанные в таблицу не трогает."""
    with get_conn() as conn:
        conn.execute("DELETE FROM sec_drafts WHERE slot_id = ? AND status = 'pending'", (slot_id,))
        for sheet, row in drafts:
            conn.execute(
                "INSERT INTO sec_drafts (slot_id, sheet, row_json, created_at) VALUES (?, ?, ?, ?)",
                (slot_id, sheet, json.dumps(row, ensure_ascii=False), now()),
            )


def drafts_of_day(day: str, status: Optional[str] = "pending") -> list:
    q = "SELECT d.* FROM sec_drafts d JOIN sec_slots s ON s.id = d.slot_id WHERE s.day = ?"
    args: list = [day]
    if status:
        q += " AND d.status = ?"
        args.append(status)
    with get_conn() as conn:
        return conn.execute(q + " ORDER BY s.time_msk, d.id", args).fetchall()


def drafts_of_slot(slot_id: int, status: Optional[str] = "pending") -> list:
    q = "SELECT * FROM sec_drafts WHERE slot_id = ?"
    args: list = [slot_id]
    if status:
        q += " AND status = ?"
        args.append(status)
    with get_conn() as conn:
        return conn.execute(q + " ORDER BY id", args).fetchall()


def get_draft(draft_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_drafts WHERE id = ?", (draft_id,)).fetchone()


def draft_by_msg(msg_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_drafts WHERE msg_id = ?", (msg_id,)).fetchone()


def update_draft(draft_id: int, **fields) -> None:
    if "row" in fields:
        fields["row_json"] = json.dumps(fields.pop("row"), ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE sec_drafts SET {cols} WHERE id = ?", (*fields.values(), draft_id))


def draft_row(draft) -> dict:
    return json.loads(draft["row_json"])


# --- вопросы Антону ---

def replace_questions(slot_id: int, questions: list[str]) -> list[int]:
    with get_conn() as conn:
        conn.execute("DELETE FROM sec_questions WHERE slot_id = ? AND answer IS NULL", (slot_id,))
        answered = {r["text"] for r in conn.execute("SELECT text FROM sec_questions WHERE slot_id = ?", (slot_id,))}
        ids = []
        for q in questions:
            if q in answered:
                continue
            ids.append(conn.execute(
                "INSERT INTO sec_questions (slot_id, text, created_at) VALUES (?, ?, ?)", (slot_id, q, now())
            ).lastrowid)
        return ids


def add_answer(slot_id: int, question: str, answer: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sec_questions (slot_id, text, answer, created_at) VALUES (?, ?, ?, ?)",
            (slot_id, question, answer, now()),
        )


def questions_of_slot(slot_id: int, open_only: bool = False) -> list:
    q = "SELECT * FROM sec_questions WHERE slot_id = ?" + (" AND answer IS NULL" if open_only else "")
    with get_conn() as conn:
        return conn.execute(q + " ORDER BY id", (slot_id,)).fetchall()


def get_question(qid: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_questions WHERE id = ?", (qid,)).fetchone()


def question_by_msg(msg_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_questions WHERE msg_id = ?", (msg_id,)).fetchone()


def update_question(qid: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE sec_questions SET {cols} WHERE id = ?", (*fields.values(), qid))


# --- запомненные сопоставления «имя в эфирах → участник» ---

def get_alias(name_norm: str):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sec_aliases WHERE name_norm = ?", (name_norm,)).fetchone()


def set_alias(name_norm: str, part_row: Optional[int], nick: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sec_aliases (name_norm, part_row, nick) VALUES (?, ?, ?)", (name_norm, part_row, nick)
        )
