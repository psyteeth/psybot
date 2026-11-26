"""«Секретарь созвонов» в боте: ежедневная задача, сообщения Антону, кнопки, запись в таблицу.

Поток дня (ТЗ_секретарь_созвонов.md, раздел 3.2):
  1. 16:00 по Таиланду — список созвонов дня из таблицы эфиров (только блоки SECRETARY_TRAINER),
     сопоставление с листом «Участники» (неоднозначное — кнопками Антону);
  2. материал: расшифровки (таблица «Расшифровки созвонов»), посты тренеров в «Исцелении
     отношений», присланное Антоном (ответом на сообщение участника, пересылкой или блоком
     «Строки для таблицы»);
  3. Антону — список ✅/❓, по каждому ❓ отдельное сообщение с кнопками «Не было созвона» /
     «Пропустить сегодня»; через 3 часа одно напоминание, наутро — ещё одно;
  4. разбор через Claude (готовый блок разбирается без Claude), вопросы — Антону, не наугад;
  5. сводка с кнопками «Добавить всё / По одному / Не добавлять», запись строк с третьей строки;
  6. «Журнал_секретаря» — чтобы один и тот же созвон не обрабатывался дважды.

В логи не пишутся имена участников: только id слотов и счётчики."""
import asyncio
import datetime as dt
import json
import logging
import re

import gspread
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import llm
from bot import secretary as S
from bot import secretary_db as sdb
from bot.config import (
    ADMIN_CHAT_ID,
    BASE_DIR,
    EFIRY_SPREADSHEET_ID,
    MODEL_SONNET,
    PARTICIPANTS_SPREADSHEET_ID,
    RELATIONSHIPS_CHAT_ID,
    SECRETARY_ENABLED,
    SECRETARY_HOUR_UTC,
    SECRETARY_MINUTE_UTC,
    SECRETARY_MORNING_HOUR_UTC,
    SECRETARY_REMIND_HOURS,
    SECRETARY_TRAINER,
    SECRETARY_TRAINER_IDS,
    TRANSCRIPTS_SPREADSHEET_ID,
)
from bot.sheets import _build_client

logger = logging.getLogger(__name__)

TZ_DOC_PATH = BASE_DIR / "docs" / "ТЗ_таблица_участников.md"
_lock = asyncio.Lock()


# ================================================================ Google

class SecretaryGoogle:
    """Синхронные вызовы Google (через asyncio.to_thread). Колонки — по заголовку листа."""

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self) -> gspread.Client:
        if self._client is None:
            self._client = _build_client()
            if self._client is None:
                raise RuntimeError("Google не настроен (GOOGLE_SERVICE_ACCOUNT_JSON)")
        return self._client

    def _participants_book(self):
        return self.client.open_by_key(PARTICIPANTS_SPREADSHEET_ID)

    def efiry(self, day: dt.date) -> list[S.Slot]:
        sh = self.client.open_by_key(EFIRY_SPREADSHEET_ID)
        title = S.efiry_tab_title([ws.title for ws in sh.worksheets()], day)
        if not title:
            raise RuntimeError(f"в таблице эфиров нет листа «{S.MONTHS_NOM[day.month]} {day.year}»")
        return S.parse_efiry_grid(sh.worksheet(title).get_all_values(), day.year, day.month)

    def tab_values(self, tab: str) -> list[list[str]]:
        return self._participants_book().worksheet(tab).get_all_values()

    def headers(self) -> dict[str, list[str]]:
        out = {}
        book = self._participants_book()
        for tab in (S.TAB_PARTICIPANTS, S.TAB_CALIBRATIONS, S.TAB_COMMUNICATIONS):
            try:
                out[tab] = [h.strip() for h in book.worksheet(tab).row_values(1)]
            except Exception:  # noqa: BLE001
                out[tab] = S.DEFAULT_COLUMNS[tab]
        return out

    def faces_roles(self) -> tuple[list[str], list[str]]:
        book = self._participants_book()
        faces = [r[0].strip() for r in book.worksheet(S.TAB_FACES).get_all_values()[1:] if r and r[0].strip()]
        roles = [r[0].strip() for r in book.worksheet(S.TAB_ROLES).get_all_values()[1:] if r and r[0].strip()]
        return faces, roles

    def transcripts(self, day: dt.date) -> list[S.Transcript]:
        try:
            sh = (
                self.client.open_by_key(TRANSCRIPTS_SPREADSHEET_ID)
                if TRANSCRIPTS_SPREADSHEET_ID else self.client.open(S.TRANSCRIPTS_SPREADSHEET_NAME)
            )
            values = sh.worksheet(S.TRANSCRIPTS_TAB).get_all_values()
        except (gspread.SpreadsheetNotFound, gspread.WorksheetNotFound):
            logger.warning("Секретарь: таблицы расшифровок ещё нет — иду без записей")
            return []
        return [t for t in S.transcripts_from_values(values) if t.start_th.date() == day]

    def _journal_ws(self):
        book = self._participants_book()
        try:
            return book.worksheet(S.TAB_JOURNAL)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=S.TAB_JOURNAL, rows=500, cols=len(S.JOURNAL_HEADER))
            ws.update(range_name="A1", values=[S.JOURNAL_HEADER], value_input_option="RAW")
            return ws

    def journal(self, day: dt.date) -> dict[tuple[str, str], str]:
        """(время_мск, участник) → последний статус за день."""
        out = {}
        want = S.fmt_date(day)
        for row in self._journal_ws().get_all_values()[1:]:
            row = list(row) + [""] * len(S.JOURNAL_HEADER)
            if row[0].strip() == want:
                out[(row[1].strip(), row[2].strip())] = row[7].strip()
        return out

    def journal_append(self, rows: list[list]) -> None:
        if rows:
            self._journal_ws().append_rows(rows, value_input_option="RAW", table_range="A1")

    def append_rows(self, tab: str, rows: list[dict]) -> int:
        """Дописывает строки в первую пустую строку листа (не выше третьей), колонки по заголовку.
        USER_ENTERED — как вставка руками; ячейки, начинающиеся с «=», «+», «-», заранее получили
        пробел (secretary.sanitize_cell), поэтому формул и #ERROR! не будет."""
        if not rows:
            return 0
        ws = self._participants_book().worksheet(tab)
        values = ws.get_all_values()
        header = [h.strip() for h in (values[0] if values else S.DEFAULT_COLUMNS[tab])]
        start = S.first_empty_row(values)
        if tab == S.TAB_COMMUNICATIONS:
            next_id = S.next_comm_id(values, header)
            for r in rows:
                r["id"] = str(next_id)
                next_id += 1
        out = [S.row_values(header, r) for r in rows]
        need = start + len(out) - 1
        if need > ws.row_count:
            ws.add_rows(need - ws.row_count + 50)
        ws.update(range_name=f"A{start}", values=out, value_input_option="USER_ENTERED")
        return len(out)


google = SecretaryGoogle()


def _participant_key(slot) -> str:
    return slot["nick"] or slot["name"]


# ================================================================ сбор дня

def _is_admin(update: Update) -> bool:
    return bool(ADMIN_CHAT_ID) and update.effective_chat is not None and str(update.effective_chat.id) == str(ADMIN_CHAT_ID)


def _participant_for(slot, participants: list[S.Participant]) -> S.Participant | None:
    if slot["match"] != "ok":
        return None
    for p in participants:
        if (slot["nick"] and p.nick == slot["nick"]) or (not slot["nick"] and p.row == slot["part_row"]):
            return p
    return None


async def refresh_day(day: dt.date, until_msk: dt.time | None) -> tuple[list, list[S.Participant]]:
    """Слоты дня в базе + новые материалы. Возвращает (строки sec_slots дня, участники)."""
    dkey = day.isoformat()
    sdb.ensure_day(dkey)
    slots_all = await asyncio.to_thread(google.efiry, day)
    slots = S.slots_for_day(slots_all, day, SECRETARY_TRAINER, until_msk)
    participants = S.parse_participants(await asyncio.to_thread(google.tab_values, S.TAB_PARTICIPANTS))
    journal = await asyncio.to_thread(google.journal, day)

    ids = []
    for s in slots:
        sid = sdb.upsert_slot(dkey, s.hhmm, s.name, s.trainer)
        ids.append(sid)
        row = sdb.get_slot(sid)
        if row["match"] != "ok":
            alias = sdb.get_alias(S.norm(s.name))
            if alias:
                sdb.update_slot(sid, match="ok", nick=alias["nick"] or "", part_row=alias["part_row"])
            else:
                m = S.match_participant(s.name, participants)
                p = m.participant
                sdb.update_slot(sid, match=m.status, nick=p.nick if p else "", part_row=p.row if p else None)
        row = sdb.get_slot(sid)
        jstatus = journal.get((s.hhmm, _participant_key(row)))
        final = {"добавлено": "added", "не добавлять": "declined", "не было": "no_call", "пропущено": "skipped"}.get(jstatus or "")
        if final and row["status"] not in sdb.FINAL_STATUSES:
            sdb.update_slot(sid, status=final)

    # Слот, которого больше нет в таблице эфиров (ячейку поправили — «Яна, Ксения» стали двумя слотами),
    # пустой и не разобранный — убираем, чтобы не висел ❓ в списках и напоминаниях.
    sdb.drop_stale_slots(dkey, keep=set(ids))
    rows = [sdb.get_slot(i) for i in ids]
    by_index = {i: r for i, r in enumerate(rows)}

    transcripts = await asyncio.to_thread(google.transcripts, day)
    for idx, pieces in S.assign_transcripts(slots, transcripts).items():
        slot = by_index[idx]
        for piece in pieces:
            meta = {"shared_with": [f"{o.name} {o.hhmm}" for o in piece.shared_with], "file_start": piece.transcript.start_th.isoformat()}
            if sdb.add_material(slot["id"], "запись", piece.transcript.file, piece.text, meta):
                _mark_new_material(slot["id"])

    for post in sdb.chat_posts_of_day(dkey):
        for slot in rows:
            p = _participant_for(slot, participants)
            if S.mentions(post["text"], slot["name"], p):
                if sdb.add_material(slot["id"], "чат", f"{post['chat_id']}:{post['msg_id']}", post["text"]):
                    _mark_new_material(slot["id"])

    rows = [sdb.get_slot(i) for i in ids]
    logger.info("Секретарь %s: слотов %d, расшифровок %d", dkey, len(rows), len(transcripts))
    return rows, participants


def _mark_new_material(slot_id: int) -> None:
    slot = sdb.get_slot(slot_id)
    if slot["status"] in ("pending", "material", "extracted"):
        sdb.update_slot(slot_id, status="material")


def _sources(slot_id: int) -> list[str]:
    out = []
    for m in sdb.materials_of_slot(slot_id):
        out.append("блок" if m["source"] == "личка" and S.has_ready_block(m["text"]) else m["source"])
    return out


# ================================================================ разбор (Claude или готовый блок)

def _tz_doc() -> str:
    try:
        text = TZ_DOC_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.split("## 4.", 1)[0]


SYSTEM_PROMPT = """Ты — секретарь школы психостоматологии. По материалам одного созвона тренера с участником готовишь строки для Google-таблицы «Участники психостоматологии» строго по ТЗ ниже.

Правила (обязательны):
1. Не угадывай. Если непонятно, кто сказал, какой зуб, какая точка, какое лицо — не заполняй, а задай вопрос Антону в «вопросы». Лучше пустая колонка и вопрос, чем выдумка.
2. Никаких имён, фамилий, телефонов, адресов и ников третьих лиц: у них только роль («мама», «начальник»). Роль — из списка ролей ниже, лицо — из списка лиц ниже.
3. Слова участника — дословно, мат и сырые слова как есть, без цензуры и пересказа.
4. Телесное и медицинское — только словами участника, без диагнозов.
5. Калибровка — связка точек: А и Б (часто АА) в одной строке; несколько вариантов Б с одним адресатом — отдельные строки.
6. Коммуникация — одна встреча участника с человеком, из-за которого болит зуб: фраза адресата, ответ из А, ответ из Б, лица. Если какого-то пути нет — строку всё равно дай, но задай вопрос.
7. Не повторяй калибровки, которые уже есть в таблице (последние даны ниже).
8. Если в одной записи несколько участников подряд, бери только часть этого участника (по времени его слота и по смыслу). Если граница не ясна — вопрос.
9. Если материал не про этого участника или созвона не было — пустые списки и вопрос.
10. Строку «Участники» давай, только если участника нет в таблице.

Ответ — ТОЛЬКО JSON без текста вокруг:
{"калибровки": [{"дата": "ДД.ММ.ГГГГ", "название": "", "адресат_роль": "", "точка_А": "", "точка_АА": "", "точка_Б": "", "названия_точек": "", "источник": "", "достоверность": "", "на_сайт": ""}],
 "коммуникации": [{"дата": "", "адресат_роль": "", "лицо_адресата": "", "фраза_адресата": "", "ответ_А": "", "лицо_после_А": "", "ответ_АА": "", "лицо_после_АА": "", "ответ_Б": "", "лицо_после_Б": "", "названия_точек": "", "портал": "", "зуб": "", "источник": "", "реакция_после_А": "", "реакция_после_Б": "", "продолжение_Б": ""}],
 "участник": null,
 "вопросы": ["короткий вопрос Антону"]}
ник_tg, id и на_сайт у коммуникаций бот проставит сам.

ТЗ таблицы участников:
"""


def _material_block(materials: list) -> str:
    parts = []
    for m in materials:
        label = {"запись": "Расшифровка записи созвона (время — Таиланд)", "чат": "Пост тренера в чате «Исцеление отношений»", "личка": "Прислал Антон"}.get(m["source"], m["source"])
        meta = sdb_json(m["meta"])
        extra = ""
        if meta.get("shared_with"):
            extra = f" — в этой же записи, возможно, созвоны: {', '.join(meta['shared_with'])}; бери только часть этого участника"
        parts.append(f"### {label}{extra}\n{m['text']}")
    return "\n\n".join(parts)


def sdb_json(raw) -> dict:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _ready_questions(text: str) -> list[str]:
    out, on = [], False
    for line in text.splitlines():
        if "вопросы антону" in line.lower():
            on = True
            continue
        if on:
            m = re.match(r"^\s*(?:[-•*]|\d+[.)])\s*(.+)$", line)
            if m:
                out.append(m.group(1).strip())
    return out


async def extract_slot(slot_id: int, participants: list[S.Participant] | None = None) -> S.Extraction:
    slot = sdb.get_slot(slot_id)
    day = dt.date.fromisoformat(slot["day"])
    if participants is None:
        participants = S.parse_participants(await asyncio.to_thread(google.tab_values, S.TAB_PARTICIPANTS))
    participant = _participant_for(slot, participants)
    materials = sdb.materials_of_slot(slot_id)
    headers = await asyncio.to_thread(google.headers)

    ready = [m for m in materials if m["source"] == "личка" and S.parse_ready_block(m["text"], headers)]
    if ready:
        data = {"калибровки": [], "коммуникации": [], "участник": None, "вопросы": []}
        for m in ready:
            parsed = S.parse_ready_block(m["text"], headers)
            data["калибровки"] += parsed.get(S.TAB_CALIBRATIONS, [])
            data["коммуникации"] += parsed.get(S.TAB_COMMUNICATIONS, [])
            if parsed.get(S.TAB_PARTICIPANTS):
                data["участник"] = parsed[S.TAB_PARTICIPANTS][0]
            data["вопросы"] += _ready_questions(m["text"])
        ex = S.build_extraction(data, day, participant, "Claude-чат")
    else:
        faces, roles = await asyncio.to_thread(google.faces_roles)
        recent = []
        if participant:
            _, cal = S.records_from_values(await asyncio.to_thread(google.tab_values, S.TAB_CALIBRATIONS))
            recent = [f for _, f in cal if f.get("ник_tg", "").strip() == participant.nick][-10:]
        answered = [q for q in sdb.questions_of_slot(slot_id) if q["answer"]]
        user = [
            f"Дата созвона: {S.fmt_date(day)}, время слота: {slot['time_msk']} по Москве "
            f"({(dt.datetime.combine(day, dt.time.fromisoformat(slot['time_msk'])) + S.TH_MINUS_MSK):%H:%M} по Таиланду), 15 минут.",
            f"Участник в таблице эфиров: {slot['name']}",
            "Строка участника из листа «Участники»: " + (
                "\n" + "\n".join(f"{k}: {v}" for k, v in participant.fields.items() if v) if participant
                else "нет в таблице (новый участник или не сопоставлен)"
            ),
            "Последние калибровки участника:\n" + (
                "\n".join(" | ".join(f"{k}: {v}" for k, v in r.items() if v) for r in recent) or "нет"
            ),
            "Лица (лист «Лица»): " + ", ".join(faces),
            "Роли (лист «Роли»): " + ", ".join(roles),
            "Материалы:\n\n" + _material_block(materials),
        ]
        if answered:
            user.append("Ответы Антона на прошлые вопросы (учесть в строках):\n" + "\n".join(f"— {q['text']} → {q['answer']}" for q in answered))
        raw = await llm._ask(SYSTEM_PROMPT + _tz_doc(), "\n\n".join(user), MODEL_SONNET, max_tokens=6000)
        try:
            data = S.parse_llm_json(raw)
        except ValueError:
            logger.warning("Секретарь: слот %s — ответ Claude не JSON", slot_id)
            data = {"вопросы": ["Не смог разобрать материал автоматически — пришли заметки текстом или блок «Строки для таблицы»."]}
        ex = S.build_extraction(data, day, participant, "созвон")

    drafts = [(S.TAB_CALIBRATIONS, r) for r in ex.calibrations] + [(S.TAB_COMMUNICATIONS, r) for r in ex.communications]
    if ex.participant_row:
        drafts.insert(0, (S.TAB_PARTICIPANTS, ex.participant_row))
    sdb.replace_drafts(slot_id, drafts)
    sdb.replace_questions(slot_id, ex.questions)
    sdb.update_slot(slot_id, status="extracted")
    logger.info("Секретарь: слот %s разобран: калибровок %d, коммуникаций %d, вопросов %d",
                slot_id, len(ex.calibrations), len(ex.communications), len(ex.questions))
    return ex


async def process_pending(day: dt.date, participants=None) -> int:
    n = 0
    for slot in sdb.slots_of_day(day.isoformat()):
        # неоднозначное сопоставление ждёт выбора Антона — иначе Claude примет участника за нового
        if slot["status"] == "material" and slot["match"] != "ambiguous":
            try:
                await extract_slot(slot["id"], participants)
                n += 1
            except Exception:  # noqa: BLE001
                logger.exception("Секретарь: разбор слота %s упал", slot["id"])
    return n


# ================================================================ сообщения Антону

def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


async def _send(bot, text: str, reply_markup=None):
    parts = S.split_message(text)
    msg = None
    for i, part in enumerate(parts):
        msg = await bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=part, reply_markup=reply_markup if i == len(parts) - 1 else None
        )
    return msg


def _current_ids(day: dt.date, slot_rows: list | None) -> list[int]:
    return [r["id"] for r in (slot_rows if slot_rows is not None else sdb.slots_of_day(day.isoformat()))]


async def send_day_list(bot, day: dt.date, slot_rows: list) -> None:
    items = []
    for s in slot_rows:
        name = s["name"] + (" (нет в «Участниках»)" if s["match"] == "none" else "")
        items.append((name, s["time_msk"], _sources(s["id"]), s["status"]))
    msg = await _send(bot, S.day_list_text(day, items))
    sdb.update_day(day.isoformat(), list_msg_id=msg.message_id)

    participants = None
    for s in slot_rows:
        if s["match"] == "ambiguous" and not s["msg_id"]:
            participants = participants or S.parse_participants(await asyncio.to_thread(google.tab_values, S.TAB_PARTICIPANTS))
            cands = S.match_participant(s["name"], participants).candidates[:6]
            buttons = [[(f"{p.name} {p.nick}".strip(), f"sec:pick:{s['id']}:{p.row}")] for p in cands]
            buttons.append([("Нет в таблице", f"sec:pick:{s['id']}:0")])
            await _send(bot, f"Кто это: «{s['name']}» {s['time_msk']}? Несколько похожих в листе «Участники».", reply_markup=_kb(buttons))
        if s["status"] == "pending" and not _sources(s["id"]) and not s["msg_id"]:
            m = await _send(
                bot,
                f"❓ {s['name']} {s['time_msk']} — ничего нет.\nПришли заметки ответом на это сообщение.",
                reply_markup=_kb([[("Не было созвона", f"sec:nocall:{s['id']}"), ("Пропустить сегодня", f"sec:skip:{s['id']}")]]),
            )
            sdb.update_slot(s["id"], msg_id=m.message_id)


async def send_summary(bot, day: dt.date) -> None:
    dkey = day.isoformat()
    slots = sdb.slots_of_day(dkey)
    lines, questions = [], []
    for s in slots:
        drafts = sdb.drafts_of_slot(s["id"])
        cal = [sdb.draft_row(d) for d in drafts if d["sheet"] == S.TAB_CALIBRATIONS]
        com = [sdb.draft_row(d) for d in drafts if d["sheet"] == S.TAB_COMMUNICATIONS]
        prow = next((sdb.draft_row(d) for d in drafts if d["sheet"] == S.TAB_PARTICIPANTS), None)
        lines += S.summary_lines(s["name"], cal, com, prow)
        for q in sdb.questions_of_slot(s["id"], open_only=True):
            questions.append((s, q))
    if not lines and not questions:
        return
    waiting = [s for s in slots if s["status"] == "pending"]
    head = "По всем собрал." if not waiting else f"Собрал по части (ждут материала: {len(waiting)})."
    text = head + (" Новые случаи:\n" + "\n".join(lines) if lines else " Новых строк нет.")
    if questions:
        text += "\nВопросы:\n" + "\n".join(f"— {s['name']}: {q['text']}" for s, q in questions)
    markup = None
    if lines:
        markup = _kb([[("Добавить всё", f"sec:all:{dkey}"), ("По одному", f"sec:each:{dkey}"), ("Не добавлять", f"sec:none:{dkey}")]])
    await _send(bot, text, reply_markup=markup)
    for s, q in questions:
        if not q["msg_id"]:
            m = await _send(bot, f"❓ {s['name']} {s['time_msk']}: {q['text']}\nОтветь реплаем — допишу в строку перед записью.")
            sdb.update_question(q["id"], msg_id=m.message_id)


async def run_day(bot, day: dt.date, until_msk: dt.time | None, job_queue=None) -> None:
    async with _lock:
        first = sdb.get_day(day.isoformat()) is None or not sdb.get_day(day.isoformat())["list_msg_id"]
        try:
            rows, participants = await refresh_day(day, until_msk)
        except Exception as e:  # noqa: BLE001
            logger.exception("Секретарь: не смог собрать день %s", day)
            await _send(bot, f"Секретарь: не смог прочитать таблицы ({type(e).__name__}: {str(e)[:200]}).")
            return
        if not rows:
            await _send(bot, f"Секретарь: в таблице эфиров на {day:%d.%m} созвонов нет.")
            return
        await send_day_list(bot, day, rows)
        await process_pending(day, participants)
        await send_summary(bot, day)
    if first and job_queue is not None:
        job_queue.run_once(remind_job, when=dt.timedelta(hours=SECRETARY_REMIND_HOURS), data=day.isoformat(), name=f"sec_remind_{day}")


def _open_items(day: dt.date) -> tuple[list, int]:
    slots = sdb.slots_of_day(day.isoformat())
    waiting = [s for s in slots if s["status"] == "pending"]
    pending_rows = len(sdb.drafts_of_day(day.isoformat()))
    return waiting, pending_rows


# ================================================================ задачи по расписанию

async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    day = S.today_th(now)
    await run_day(context.bot, day, now.astimezone(S.TZ_MSK).time().replace(tzinfo=None), context.job_queue)


async def remind_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    day = dt.date.fromisoformat(context.job.data)
    rec = sdb.get_day(day.isoformat())
    if not rec or rec["reminded_3h"]:
        return
    async with _lock:
        try:
            _, participants = await refresh_day(day, None)  # могли догрузиться расшифровки
            if await process_pending(day, participants):
                await send_summary(context.bot, day)
        except Exception:  # noqa: BLE001
            logger.exception("Секретарь: обновление перед напоминанием упало")
    waiting, _ = _open_items(day)
    if waiting:
        await _send(context.bot, "Напоминаю: по этим созвонам ничего нет — " + ", ".join(f"{s['name']} {s['time_msk']}" for s in waiting)
                    + ".\nОтветь на сообщение с именем: заметки, блок из Claude-чата или «не было».")
    sdb.update_day(day.isoformat(), reminded_3h=1)


async def morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    day = S.today_th() - dt.timedelta(days=1)
    rec = sdb.get_day(day.isoformat())
    if not rec or rec["reminded_morning"]:
        return
    waiting, pending_rows = _open_items(day)
    if waiting or pending_rows:
        parts = []
        if waiting:
            parts.append(f"без материала: {len(waiting)}")
        if pending_rows:
            parts.append(f"строк ждут решения: {pending_rows}")
        await _send(context.bot, f"Вчерашний день ({day:%d.%m}) не закрыт — {', '.join(parts)}. "
                    f"Ответь на сообщения ❓ или нажми кнопки в сводке. Повторить сводку: /secretary {day:%d.%m}")
    sdb.update_day(day.isoformat(), reminded_morning=1)


# ================================================================ запись в таблицу

async def _write_drafts(day: dt.date, drafts: list) -> tuple[int, int, int]:
    """Пишет черновики в листы; по каждому слоту, у которого больше нет черновиков, — строка журнала."""
    journal = await asyncio.to_thread(google.journal, day)
    by_tab: dict[str, list] = {}
    skipped_dupes = []
    for d in drafts:
        slot = sdb.get_slot(d["slot_id"])
        if journal.get((slot["time_msk"], _participant_key(slot))) == "добавлено":  # уже писали этот созвон
            skipped_dupes.append(d)
            continue
        by_tab.setdefault(d["sheet"], []).append(d)
    counts = {S.TAB_CALIBRATIONS: 0, S.TAB_COMMUNICATIONS: 0, S.TAB_PARTICIPANTS: 0}
    for tab in (S.TAB_PARTICIPANTS, S.TAB_CALIBRATIONS, S.TAB_COMMUNICATIONS):
        items = by_tab.get(tab, [])
        if not items:
            continue
        await asyncio.to_thread(google.append_rows, tab, [sdb.draft_row(d) for d in items])
        for d in items:
            sdb.update_draft(d["id"], status="added")
        counts[tab] = len(items)
    for d in skipped_dupes:
        sdb.update_draft(d["id"], status="skipped")
    await _journal_finished(day, {d["slot_id"] for d in drafts})
    return counts[S.TAB_CALIBRATIONS], counts[S.TAB_COMMUNICATIONS], counts[S.TAB_PARTICIPANTS]


async def _journal_finished(day: dt.date, slot_ids: set[int], status_override: str | None = None) -> None:
    rows = []
    stamp = dt.datetime.now(S.TZ_TH).strftime("%d.%m.%Y %H:%M")
    for sid in sorted(slot_ids):
        if sdb.drafts_of_slot(sid, "pending"):
            continue
        slot = sdb.get_slot(sid)
        done = sdb.drafts_of_slot(sid, "added")
        cal = sum(1 for d in done if d["sheet"] == S.TAB_CALIBRATIONS)
        com = sum(1 for d in done if d["sheet"] == S.TAB_COMMUNICATIONS)
        prow = "да" if any(d["sheet"] == S.TAB_PARTICIPANTS for d in done) else ""
        if status_override:
            status, new_status = status_override, {"не добавлять": "declined", "не было": "no_call", "пропущено": "skipped"}[status_override]
        elif done:
            status, new_status = "добавлено", "added"
        else:
            status, new_status = "не добавлять", "declined"
        sdb.update_slot(sid, status=new_status)
        sources = ", ".join(dict.fromkeys(S.SOURCE_LABELS.get(s, s) for s in _sources(sid)))
        rows.append([S.fmt_date(day), slot["time_msk"], _participant_key(slot), sources, cal, com, prow, status, stamp])
    await asyncio.to_thread(google.journal_append, rows)


# ================================================================ кнопки

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer()
        raise ApplicationHandlerStop
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    bot = context.bot

    if action in ("nocall", "skip"):
        sid = int(parts[2])
        slot = sdb.get_slot(sid)
        label = "не было" if action == "nocall" else "пропущено"
        sdb.replace_drafts(sid, [])
        await _journal_finished(dt.date.fromisoformat(slot["day"]), {sid}, status_override=label)
        await query.message.reply_text(f"{slot['name']} {slot['time_msk']} — {label}.")

    elif action == "pick":
        sid, row = int(parts[2]), int(parts[3])
        slot = sdb.get_slot(sid)
        if row == 0:
            sdb.update_slot(sid, match="none", nick="", part_row=None)
            sdb.set_alias(S.norm(slot["name"]), None, "")
            await query.message.reply_text("Ок, считаю новым участником.")
        else:
            participants = S.parse_participants(await asyncio.to_thread(google.tab_values, S.TAB_PARTICIPANTS))
            p = next((x for x in participants if x.row == row), None)
            if p is None:
                await query.message.reply_text("Эта строка в «Участниках» уже другая — запусти /secretary ещё раз.")
                raise ApplicationHandlerStop
            sdb.update_slot(sid, match="ok", nick=p.nick, part_row=p.row)
            sdb.set_alias(S.norm(slot["name"]), p.row, p.nick)
            await query.message.reply_text(f"Запомнил: «{slot['name']}» — это {p.label}.")
        if sdb.materials_of_slot(sid) and slot["status"] not in sdb.FINAL_STATUSES:
            sdb.update_slot(sid, status="material")
            await process_pending(dt.date.fromisoformat(slot["day"]))
            await send_summary(bot, dt.date.fromisoformat(slot["day"]))

    elif action == "inbox":
        iid, sid = int(parts[2]), int(parts[3])
        item = sdb.get_inbox(iid)
        if sid == 0:
            await query.message.reply_text("Ок, не привязываю.")
        else:
            await _attach_material(bot, sid, f"inbox:{iid}", item["text"])
            sdb.assign_inbox(iid, sid)

    elif action in ("all", "each", "none"):
        day = dt.date.fromisoformat(parts[2])
        drafts = sdb.drafts_of_day(day.isoformat())
        if not drafts:
            await query.message.reply_text("Нечего добавлять — всё уже решено.")
        elif action == "all":
            async with _lock:
                cal, com, prt = await _write_drafts(day, drafts)
            extra = f", участников: {prt}" if prt else ""
            await query.message.reply_text(f"Добавил: калибровок {cal}, коммуникаций {com}{extra}.")
        elif action == "none":
            for d in drafts:
                sdb.update_draft(d["id"], status="skipped")
            await _journal_finished(day, {d["slot_id"] for d in drafts})
            await query.message.reply_text("Ничего не добавляю. Отметил в журнале.")
        else:
            for d in drafts:
                await _send_draft(bot, d)

    elif action in ("add", "edit", "drop"):
        draft = sdb.get_draft(int(parts[2]))
        if not draft or draft["status"] != "pending":
            await query.message.reply_text("Эта строка уже решена.")
            raise ApplicationHandlerStop
        day = dt.date.fromisoformat(sdb.get_slot(draft["slot_id"])["day"])
        if action == "add":
            async with _lock:
                await _write_drafts(day, [draft])
            await query.message.reply_text("Добавил.")
        elif action == "drop":
            sdb.update_draft(draft["id"], status="skipped")
            await _journal_finished(day, {draft["slot_id"]})
            await query.message.reply_text("Не добавляю.")
        else:
            m = await query.message.reply_text(
                "Ответь на это сообщение: что поменять (словами) или исправленную строку целиком (через табуляцию, в порядке колонок листа)."
            )
            sdb.update_draft(draft["id"], msg_id=m.message_id)
    raise ApplicationHandlerStop


async def _send_draft(bot, draft) -> None:
    slot = sdb.get_slot(draft["slot_id"])
    text = f"{slot['name']} {slot['time_msk']}\n" + S.draft_preview(draft["sheet"], sdb.draft_row(draft))
    m = await _send(bot, text, reply_markup=_kb([[
        ("Добавить", f"sec:add:{draft['id']}"), ("Изменить", f"sec:edit:{draft['id']}"), ("Не добавлять", f"sec:drop:{draft['id']}"),
    ]]))
    sdb.update_draft(draft["id"], msg_id=m.message_id)


# ================================================================ сообщения Антона

_NO_CALL_RE = re.compile(r"^\s*(не было|не был|не была|не состоялся|отмена|отменили)\W*$", re.I)


async def _attach_material(bot, slot_id: int, ref: str, text: str) -> None:
    slot = sdb.get_slot(slot_id)
    day = dt.date.fromisoformat(slot["day"])
    if _NO_CALL_RE.match(text):
        sdb.replace_drafts(slot_id, [])
        await _journal_finished(day, {slot_id}, status_override="не было")
        await _send(bot, f"{slot['name']} {slot['time_msk']} — отметил «не было».")
        return
    if slot["status"] in ("added", "declined"):
        await _send(bot, f"По {slot['name']} {slot['time_msk']} строки уже решены — добавил материал, но заново не разбираю.")
        sdb.add_material(slot_id, "личка", ref, text)
        return
    sdb.add_material(slot_id, "личка", ref, text)
    sdb.update_slot(slot_id, status="material")
    await _send(bot, f"Записал к {slot['name']} {slot['time_msk']}. Разбираю…")
    async with _lock:
        await process_pending(day)
    await send_summary(bot, day)


def _latest_day() -> dt.date | None:
    from bot.db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT day FROM sec_days ORDER BY day DESC LIMIT 1").fetchone()
    return dt.date.fromisoformat(row["day"]) if row else None


async def admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Личка Антона. Перехватывается только то, что адресовано секретарю: ответ на его сообщение,
    пересылка или блок «Строки для таблицы». Остальное идёт дальше — в обычные ветки бота."""
    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    bot = context.bot
    reply = msg.reply_to_message
    if reply is not None:
        rid = reply.message_id
        q = sdb.question_by_msg(rid)
        if q:
            sdb.update_question(q["id"], answer=text)
            slot = sdb.get_slot(q["slot_id"])
            if slot["status"] in ("added", "declined"):
                await msg.reply_text("Записал ответ, но строки по этому созвону уже решены — поправь в таблице руками.")
            else:
                sdb.update_slot(slot["id"], status="material")
                await msg.reply_text("Спасибо, учитываю. Пересобираю строки…")
                async with _lock:
                    await process_pending(dt.date.fromisoformat(slot["day"]))
                await send_summary(bot, dt.date.fromisoformat(slot["day"]))
            raise ApplicationHandlerStop
        d = sdb.draft_by_msg(rid)
        if d and d["status"] == "pending":
            await _edit_draft(bot, d, text)
            raise ApplicationHandlerStop
        s = sdb.slot_by_msg(rid)
        if s:
            await _attach_material(bot, s["id"], f"msg:{msg.message_id}", text)
            raise ApplicationHandlerStop
        day_rec = sdb.day_by_list_msg(rid)
        if day_rec:
            await _route_inbox(bot, dt.date.fromisoformat(day_rec["day"]), msg.message_id, text)
            raise ApplicationHandlerStop
        return
    if getattr(msg, "forward_origin", None) is not None or S.has_ready_block(text):
        day = _latest_day()
        if day is None:
            return  # секретарь ещё ни разу не запускался — пусть обрабатывает бот как обычно
        await _route_inbox(bot, day, msg.message_id, text)
        raise ApplicationHandlerStop


async def _route_inbox(bot, day: dt.date, msg_id: int, text: str) -> None:
    slots = [s for s in sdb.slots_of_day(day.isoformat()) if s["status"] not in ("no_call", "skipped")]
    participants = S.parse_participants(await asyncio.to_thread(google.tab_values, S.TAB_PARTICIPANTS))
    hits = [s for s in slots if S.mentions(text, s["name"], _participant_for(s, participants))]
    iid = sdb.add_inbox(day.isoformat(), msg_id, text)
    if len(hits) == 1:
        sdb.assign_inbox(iid, hits[0]["id"])
        await _attach_material(bot, hits[0]["id"], f"inbox:{iid}", text)
        return
    choices = hits or slots
    buttons = [[(f"{s['name']} {s['time_msk']}", f"sec:inbox:{iid}:{s['id']}")] for s in choices[:12]]
    buttons.append([("Не к этому дню", f"sec:inbox:{iid}:0")])
    await _send(bot, f"К кому это ({day:%d.%m})?", reply_markup=_kb(buttons))


EDIT_PROMPT = """Ты правишь одну строку таблицы участников по просьбе Антона. Тебе дан JSON строки и поправка.
Измени только то, о чём просят; ничего не выдумывай; у третьих лиц только роль; мат как есть.
Ответ — ТОЛЬКО исправленный JSON-объект с теми же ключами."""


async def _edit_draft(bot, draft, text: str) -> None:
    sheet = draft["sheet"]
    row = sdb.draft_row(draft)
    slot = sdb.get_slot(draft["slot_id"])
    day = dt.date.fromisoformat(slot["day"])
    if "\t" in text:
        headers = await asyncio.to_thread(google.headers)
        cols = headers.get(sheet) or S.DEFAULT_COLUMNS[sheet]
        cells = text.split("\t")
        new = {c: (cells[i].strip() if i < len(cells) else "") for i, c in enumerate(cols)}
    else:
        raw = await llm._ask(EDIT_PROMPT, f"Строка ({sheet}):\n{json.dumps(row, ensure_ascii=False)}\n\nПоправка: {text}", MODEL_SONNET, max_tokens=2000)
        try:
            new = S.parse_llm_json(raw)
        except ValueError:
            await _send(bot, "Не понял поправку — пришли исправленную строку через табуляцию.")
            return
    nick = row.get("ник_tg", "")
    if sheet == S.TAB_CALIBRATIONS:
        new, _ = S.finalize_calibration(new, day, nick, row.get("источник") or "созвон")
    elif sheet == S.TAB_COMMUNICATIONS:
        new, _ = S.finalize_communication(new, day, nick, row.get("источник") or "созвон")
    else:
        new = S.finalize_participant(new)
    sdb.update_draft(draft["id"], row=new)
    await _send_draft(bot, sdb.get_draft(draft["id"]))


# ================================================================ посты тренеров в чате

async def chat_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.id not in SECRETARY_TRAINER_IDS:
        return
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    day = (msg.date or dt.datetime.now(dt.timezone.utc)).astimezone(S.TZ_TH).date()
    sdb.add_chat_post(msg.chat_id, msg.message_id, day.isoformat(), user.id, text)


# ================================================================ команда и регистрация

async def secretary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/secretary [ДД.ММ] — собрать день сейчас (без даты — сегодня по Таиланду)."""
    if not _is_admin(update):
        return
    now = dt.datetime.now(dt.timezone.utc)
    day, until = S.today_th(now), now.astimezone(S.TZ_MSK).time().replace(tzinfo=None)
    if context.args:
        try:
            d = dt.datetime.strptime(context.args[0], "%d.%m").date()
            day = d.replace(year=S.today_th(now).year)
            until = None
        except ValueError:
            await update.effective_message.reply_text("Формат: /secretary 26.11")
            return
    await update.effective_message.reply_text(f"Собираю созвоны {day:%d.%m}…")
    await run_day(context.bot, day, until, context.job_queue)


def register(application) -> None:
    """Подключает секретаря к боту. Выключен (SECRETARY_ENABLED не задан) — ничего не делает."""
    if not SECRETARY_ENABLED or not ADMIN_CHAT_ID:
        logger.info("Секретарь созвонов выключен (SECRETARY_ENABLED/ADMIN_CHAT_ID)")
        return
    sdb.init()
    # Группа -3 — раньше author_questions (-1): в одной группе срабатывает только первый подходящий
    # обработчик, а реплай Антона подходит и под «ответ автора». Чужие сообщения admin_message
    # пропускает дальше (без ApplicationHandlerStop).
    admin = filters.Chat(chat_id=int(ADMIN_CHAT_ID)) & filters.ChatType.PRIVATE
    application.add_handler(CallbackQueryHandler(callback, pattern=r"^sec:"), group=-3)
    application.add_handler(CommandHandler("secretary", secretary_command, filters=admin), group=-3)
    application.add_handler(
        MessageHandler(admin & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, admin_message), group=-4
    )
    if RELATIONSHIPS_CHAT_ID:
        # Посты тренеров в «Исцелении отношений» только запоминаются; бот в чате не отвечает.
        application.add_handler(
            MessageHandler(filters.Chat(chat_id=int(RELATIONSHIPS_CHAT_ID)) & (filters.TEXT | filters.CAPTION), chat_post),
            group=-5,
        )
    jq = application.job_queue
    jq.run_daily(daily_job, time=dt.time(hour=SECRETARY_HOUR_UTC, minute=SECRETARY_MINUTE_UTC, tzinfo=dt.timezone.utc), name="secretary_daily")
    jq.run_daily(morning_job, time=dt.time(hour=SECRETARY_MORNING_HOUR_UTC, minute=0, tzinfo=dt.timezone.utc), name="secretary_morning")
    logger.info("Секретарь созвонов включён: %02d:%02d UTC", SECRETARY_HOUR_UTC, SECRETARY_MINUTE_UTC)
