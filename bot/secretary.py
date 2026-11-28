"""«Секретарь созвонов» — чистая логика без Telegram, Google и Claude (ТЗ_секретарь_созвонов.md).

Здесь всё, что можно проверить тестами на готовых данных: разбор сетки таблицы эфиров, сопоставление
с листом «Участники», имена файлов Телемоста и вывод whisper, привязка расшифровок к слотам, поиск
упоминаний в постах, разбор готового блока «Строки для таблицы», доводка строк по правилам ТЗ таблицы
участников и тексты сообщений Антону. Модуль ничего не импортирует из bot.config — его использует и
расшифровщик на Mac (mac/telemost_transcribe.py).

Часовые пояса: таблица эфиров — по Москве (UTC+3), файлы Телемоста и Mac — по Таиланду (UTC+7),
Таиланд = Москва + 4 часа.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

TZ_TH = timezone(timedelta(hours=7))
TZ_MSK = timezone(timedelta(hours=3))
TH_MINUS_MSK = timedelta(hours=4)
SLOT_MINUTES = 15

EFIRY_SPREADSHEET_ID = "1p-Qw2ibqJLJUzZBRIbcHU7xgKyjQ0vpL2xHJqHVCi-M"
PARTICIPANTS_SPREADSHEET_ID = "1ZVLStap4vQI3R3B_kpKohpFsW65mGA6CxK4rGMVFIWY"
TAB_PARTICIPANTS = "Участники"
TAB_CALIBRATIONS = "Калибровки"
TAB_COMMUNICATIONS = "Коммуникации"
TAB_FACES = "Лица"
TAB_ROLES = "Роли"
TAB_JOURNAL = "Журнал_секретаря"
TAB_CARDS = "Карточки"
CARD_COLUMNS = [
    "ник_tg", "зуб", "триггер", "точка_А", "точка_Б", "все_СП", "все_АЖ", "ОЧЛ", "дофаминовая_ID",
    "опасная_ID", "зачем_зуб", "залупа", "путь_роста",
]
GROWTH_MAX = 400
JOURNAL_HEADER = ["дата", "время_мск", "участник", "источники", "калибровок", "коммуникаций", "участник_строка", "статус", "обновлено"]
FIRST_DATA_ROW = 3  # строка 1 — заголовки, строка 2 — подсказки

# Расшифровки: у сервисного аккаунта нет квоты на Drive, поэтому текст лежит в Google-таблице
# владельца внутри папки «Созвоны — расшифровки» (см. mac/telemost_transcribe.py).
TRANSCRIPTS_FOLDER_NAME = "Созвоны — расшифровки"
TRANSCRIPTS_SPREADSHEET_NAME = "Расшифровки созвонов"
TRANSCRIPTS_SPREADSHEET_ID = "1aPaKLHeiMP-fUe0nl7eJAMeGasgbUAxDxDd_pCfISTI"  # создана владельцем 26.11 в папке «Созвоны — расшифровки»
TRANSCRIPTS_TAB = "Расшифровки"
TRANSCRIPTS_HEADER = ["файл", "дата", "начало_тай", "длительность_сек", "часть", "текст"]
CELL_CHUNK = 40000  # лимит ячейки Google — 50 000 символов

# Порядок колонок из ТЗ таблицы участников — запасной, если заголовок листа прочитать не удалось.
# В живой таблице «Коммуникации» реакции стоят в конце, поэтому по-настоящему колонки берутся по заголовку.
PARTICIPANT_COLUMNS = [
    "ник_tg", "имя", "персонаж", "пол", "статус", "старт", "тренер", "зубы", "главный_зуб",
    "точка_А", "точка_АА", "точка_Б", "СП", "альфа_жертва", "поддерживает", "осуждает",
    "показ_на_сайте", "заметки_тренера",
]
CALIBRATION_COLUMNS = [
    "дата", "ник_tg", "название", "адресат_роль", "точка_А", "точка_АА", "точка_Б",
    "названия_точек", "источник", "достоверность", "на_сайт",
]
COMMUNICATION_COLUMNS = [
    "id", "дата", "ник_tg", "адресат_роль", "лицо_адресата", "фраза_адресата", "ответ_А",
    "лицо_после_А", "ответ_АА", "лицо_после_АА", "ответ_Б", "лицо_после_Б", "названия_точек",
    "портал", "зуб", "источник", "на_сайт", "реакция_после_А", "реакция_после_Б", "продолжение_Б",
    "до_триггера",
]
DEFAULT_COLUMNS = {
    TAB_PARTICIPANTS: PARTICIPANT_COLUMNS,
    TAB_CALIBRATIONS: CALIBRATION_COLUMNS,
    TAB_COMMUNICATIONS: COMMUNICATION_COLUMNS,
}
SOURCES = ("созвон", "эфир", "тренировка", "чат", "Claude-чат")
RELIABILITY = ("чётко", "заметки", "тренер", "неясно")

MONTHS_GEN = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MONTHS_NOM = {
    1: "ЯНВАРЬ", 2: "ФЕВРАЛЬ", 3: "МАРТ", 4: "АПРЕЛЬ", 5: "МАЙ", 6: "ИЮНЬ",
    7: "ИЮЛЬ", 8: "АВГУСТ", 9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ", 11: "НОЯБРЬ", 12: "ДЕКАБРЬ",
}
WEEKDAYS = {"ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"}

_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
_DAY_RE = re.compile(r"^\s*(\d{1,2})\s+([а-яё]+)\s*$", re.I)


def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def today_th(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(TZ_TH).date()


# ---------------------------------------------------------------- таблица эфиров

def efiry_tab_title(titles: list[str], d: date) -> str | None:
    """Лист месяца: «НОЯБРЬ 2026 » (в живой таблице у названий бывают хвостовые пробелы)."""
    want = f"{MONTHS_NOM[d.month]} {d.year}"
    for t in titles:
        if " ".join(t.split()).upper() == want:
            return t
    return None


@dataclass
class Slot:
    day: date
    time_msk: time
    name: str
    trainer: str = ""

    @property
    def start_th(self) -> datetime:
        return datetime.combine(self.day, self.time_msk) + TH_MINUS_MSK

    @property
    def hhmm(self) -> str:
        return self.time_msk.strftime("%H:%M")


def _parse_time(cell: str) -> time | None:
    m = _TIME_RE.match(cell or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return time(h, mi)


def _parse_day(cell: str, tab_year: int, tab_month: int) -> date | None:
    m = _DAY_RE.match(cell or "")
    if not m:
        return None
    month = MONTHS_GEN.get(m.group(2).lower())
    if not month:
        return None
    year = tab_year
    if tab_month == 12 and month == 1:
        year += 1
    elif tab_month == 1 and month == 12:
        year -= 1
    try:
        return date(year, month, int(m.group(1)))
    except ValueError:
        return None


def _cell(values: list[list[str]], r: int, c: int) -> str:
    if r < 0 or c < 0 or r >= len(values) or c >= len(values[r]):
        return ""
    return (values[r][c] or "").strip()


def _trainer_label(text: str) -> str:
    m = re.match(r"\s*([A-Za-zА-Яа-яЁё]+)", text or "")
    return m.group(1).upper() if m else ""


def parse_efiry_grid(values: list[list[str]], tab_year: int, tab_month: int) -> list[Slot]:
    """Сетка эфиров: колонка A — день недели (ПН…ВС) в первой строке дня, колонка B — тренер в
    первой строке своего блока, дальше недели по три колонки [дата | время | имя]: дата стоит в
    строке дня недели, время — в каждой строке слота, имя участника — справа от времени.

    Имя считается участником, только если слева от него время. Дата ищется вверх по колонке слева
    от времени, не выходя за начало дня (строка с днём недели в колонке A)."""
    slots: list[Slot] = []
    trainer_by_row: dict[int, str] = {}
    current = ""
    for r in range(len(values)):
        b = _cell(values, r, 1)
        if b:
            current = _trainer_label(b)
        trainer_by_row[r] = current

    for r, row in enumerate(values):
        for c in range(1, len(row) - 1):
            t = _parse_time(_cell(values, r, c))
            if t is None:
                continue
            name = _cell(values, r, c + 1)
            if not name or _parse_time(name) or _parse_day(name, tab_year, tab_month):
                continue
            day = None
            for rr in range(r, -1, -1):
                day = _parse_day(_cell(values, rr, c - 1), tab_year, tab_month)
                if day or _cell(values, rr, 0).upper() in WEEKDAYS:
                    break
            if day is None:
                continue
            # «Яна, Ксения» — двое в одном слоте: у каждого своя строка
            for one in split_slot_names(name):
                slots.append(Slot(day=day, time_msk=t, name=one, trainer=trainer_by_row.get(r, "")))
    slots.sort(key=lambda s: (s.day, s.time_msk))
    return slots


def split_slot_names(cell: str) -> list[str]:
    parts = re.split(r"\s*(?:[,;/+&]|\s\bи\b\s)\s*", " ".join(cell.split()))
    return [p.strip() for p in parts if p.strip()]


def slots_for_day(slots: list[Slot], day: date, trainer: str = "", until_msk: time | None = None) -> list[Slot]:
    """Слоты дня; trainer — первое слово из колонки B («АНТОН»), пусто = все тренеры; until_msk —
    только созвоны, которые уже начались к этому времени по Москве."""
    out = []
    for s in slots:
        if s.day != day:
            continue
        if trainer and s.trainer != trainer.upper():
            continue
        if until_msk and s.time_msk > until_msk:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------- лист «Участники»

@dataclass
class Participant:
    row: int  # номер строки в листе (1-based)
    fields: dict

    @property
    def nick(self) -> str:
        n = (self.fields.get("ник_tg") or "").strip()
        if n and not n.startswith("@"):
            n = "@" + n
        return n

    @property
    def name(self) -> str:
        return (self.fields.get("имя") or "").strip()

    @property
    def label(self) -> str:
        return self.nick or self.name or f"строка {self.row}"


def records_from_values(values: list[list[str]]) -> tuple[list[str], list[tuple[int, dict]]]:
    """Строка 1 — заголовки, строка 2 — подсказки, данные с третьей. Возвращает (заголовок,
    [(номер_строки, {колонка: значение})]) без пустых строк."""
    if not values:
        return [], []
    header = [h.strip() for h in values[0]]
    out = []
    for i, row in enumerate(values[FIRST_DATA_ROW - 1:], start=FIRST_DATA_ROW):
        if not any((c or "").strip() for c in row):
            continue
        out.append((i, {h: (row[j].strip() if j < len(row) else "") for j, h in enumerate(header) if h}))
    return header, out


def parse_participants(values: list[list[str]]) -> list[Participant]:
    _, records = records_from_values(values)
    return [Participant(row=r, fields=f) for r, f in records if f.get("ник_tg") or f.get("имя")]


DIMINUTIVES = [
    {"анна", "аня", "анечка", "анюта", "нюра"},
    {"мария", "маша", "машенька", "маруся", "мари"},
    {"екатерина", "катя", "катюша", "катерина"},
    {"елена", "лена", "леночка", "алена", "алёна"},
    {"александра", "саша", "шура", "аля"},
    {"александр", "саша", "шура"},
    {"наталья", "наталия", "наташа"},
    {"ольга", "оля", "олечка"},
    {"татьяна", "таня", "танечка"},
    {"ирина", "ира", "ирочка"},
    {"юлия", "юля"},
    {"светлана", "света"},
    {"евгения", "женя"},
    {"евгений", "женя"},
    {"анастасия", "настя", "настена"},
    {"дарья", "даша"},
    {"ксения", "ксюша", "ксюха", "ксюшка"},
    {"виктория", "вика"},
    {"валентина", "валя"},
    {"людмила", "люда", "мила"},
    {"галина", "галя"},
    {"надежда", "надя"},
    {"любовь", "люба"},
    {"елизавета", "лиза"},
    {"софия", "софья", "соня"},
    {"полина", "поля"},
    {"кристина", "кристи"},
    {"маргарита", "рита"},
    {"оксана", "ксана"},
    {"дмитрий", "дима"},
    {"михаил", "миша"},
    {"сергей", "сережа"},
    {"алексей", "леша"},
    {"владимир", "вова", "володя"},
    {"николай", "коля"},
    {"иван", "ваня"},
    {"антон", "тоша"},
    {"глафира", "глаша"},
    {"евдокия", "дуся"},
]


def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9@_ ]+", " ", s).strip()


def _tokens(s: str) -> list[str]:
    return [t for t in norm(s).split() if t]


def same_name(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return any(a in g and b in g for g in DIMINUTIVES)


@dataclass
class Match:
    status: str  # ok / ambiguous / none
    candidates: list[Participant] = field(default_factory=list)

    @property
    def participant(self) -> Participant | None:
        return self.candidates[0] if self.status == "ok" else None


def match_participant(efiry_name: str, participants: list[Participant]) -> Match:
    """По нику (@nick или nick без @ в ячейке эфиров), иначе по имени с уменьшительными.
    «Дуся Х» — первое слово сравнивается с именем; если таких участников несколько, а второе слово
    ни у кого из них не совпадает — неоднозначно, спрашиваем Антона."""
    toks = _tokens(efiry_name)
    if not toks:
        return Match("none")
    nick_hits = [
        p for p in participants
        if p.nick and any(t.lstrip("@") == norm(p.nick).lstrip("@") for t in toks)
    ]
    if len(nick_hits) == 1:
        return Match("ok", nick_hits)
    if len(nick_hits) > 1:
        return Match("ambiguous", nick_hits)

    first = toks[0].lstrip("@")
    hits = []
    for p in participants:
        ptoks = _tokens(p.name)
        if ptoks and same_name(ptoks[0], first):
            hits.append(p)
    if len(hits) > 1 and len(toks) > 1:
        rest = toks[1:]
        narrowed = [p for p in hits if any(t in _tokens(p.name)[1:] for t in rest)]
        if narrowed:
            hits = narrowed
    if not hits:  # «Янинка» → Янина, «Гульгназ» → Гульназ: -ка, падеж, одна опечатка
        hits = [p for p in participants if _tokens(p.name) and name_like(first, _tokens(p.name)[0])]
    if len(hits) == 1:
        return Match("ok", hits)
    if len(hits) > 1:
        return Match("ambiguous", hits)
    return Match("none")


# ---------------------------------------------------------------- записи Телемоста и расшифровки

_REC_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{2,4})\s+(\d{2})-(\d{2})-(\d{2})")


def parse_recording_name(filename: str) -> tuple[datetime | None, str]:
    """«Meeting in Yandex Telemost 26.11.26 12-48-05 — recording.webm» → (26.11.2026 12:48:05 по
    Таиланду, ""). Переименованный вручную файл («Глафира 26.11.webm») → (None, «Глафира 26.11»):
    метка потом сравнивается с именами участников."""
    stem = re.sub(r"\.(webm|txt|wav|mp4|m4a)$", "", filename.strip(), flags=re.I)
    m = _REC_RE.search(stem)
    if m and "telemost" in stem.lower():
        dd, mm, yy, hh, mi, ss = (int(x) for x in m.groups())
        year = yy + 2000 if yy < 100 else yy
        try:
            return datetime(year, mm, dd, hh, mi, ss), ""
        except ValueError:
            pass
    return None, stem


_WHISPER_RE = re.compile(r"^\[(\d+):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*[\d:.,]+\]\s*(.*)$")


def parse_whisper_output(stdout: str) -> list[tuple[float, str]]:
    """Строки whisper-cli «[00:01:02.340 --> 00:01:05.000]  текст» → [(секунды от начала, текст)]."""
    out = []
    for line in (stdout or "").splitlines():
        m = _WHISPER_RE.match(line.strip())
        if not m:
            continue
        h, mi, s, ms, text = m.groups()
        text = text.strip()
        if text:
            out.append((int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000, text))
    return out


def format_transcript_lines(segments: list[tuple[float, str]], start: datetime) -> list[str]:
    """Отметки времени — по часам Таиланда, чтобы бот мог резать общую запись по слотам."""
    return [f"[{(start + timedelta(seconds=sec)):%H:%M:%S}] {text}" for sec, text in segments]


def chunk_text(text: str, size: int = CELL_CHUNK) -> list[str]:
    text = text.strip()
    if not text:
        return [""]
    out = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


@dataclass
class Transcript:
    file: str
    start_th: datetime  # наивное время Таиланда
    duration_sec: int
    text: str

    @property
    def end_th(self) -> datetime:
        return self.start_th + timedelta(seconds=self.duration_sec)

    @property
    def label(self) -> str:
        return parse_recording_name(self.file)[1]


def transcripts_from_values(values: list[list[str]]) -> list[Transcript]:
    """Лист «Расшифровки»: файл | дата | начало_тай | длительность_сек | часть | текст; длинный текст
    разбит на части по строкам."""
    parts: dict[str, list] = {}
    for row in values[1:]:
        row = list(row) + [""] * 6
        fname, d, t, dur, part, text = (c.strip() if isinstance(c, str) else c for c in row[:6])
        if not fname:
            continue
        try:
            start = datetime.strptime(f"{d} {t}", "%d.%m.%Y %H:%M:%S")
        except ValueError:
            continue
        entry = parts.setdefault(fname, [start, 0, []])
        try:
            entry[1] = int(float(dur or 0))
        except ValueError:
            pass
        try:
            idx = int(float(part or 1))
        except ValueError:
            idx = 1
        entry[2].append((idx, text))
    out = []
    for fname, (start, dur, chunks) in parts.items():
        text = "\n".join(t for _, t in sorted(chunks))
        out.append(Transcript(file=fname, start_th=start, duration_sec=dur, text=text))
    out.sort(key=lambda tr: tr.start_th)
    return out


_TS_LINE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]")


def cut_transcript(text: str, day: date, start: datetime, end: datetime) -> str:
    """Строки с отметкой [ЧЧ:ММ:СС] (время Таиланда) в окне [start, end). Без отметок — весь текст."""
    lines = text.splitlines()
    picked, any_ts = [], False
    for line in lines:
        m = _TS_LINE.match(line)
        if not m:
            continue
        any_ts = True
        ts = datetime.combine(day, time(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        if start <= ts < end:
            picked.append(line)
    if not any_ts:
        return text
    return "\n".join(picked)


@dataclass
class TranscriptPiece:
    transcript: Transcript
    text: str
    shared_with: list[Slot]  # другие слоты, попавшие в ту же запись


def assign_transcripts(
    slots: list[Slot], transcripts: list[Transcript], tolerance_min: int = 5, drift_min: int = 30
) -> dict[int, list[TranscriptPiece]]:
    """Индекс слота → куски расшифровок. Три прохода:
    1) переименованный файл («Янка Хэппиэндово», «Гульгназ боль») — к участнику по имени в названии
       (уменьшительные, падежи, одна опечатка);
    2) остальные — по пересечению времени записи с 15 минутами слота; если одна запись покрывает
       несколько слотов, каждому достаётся отрезок до начала следующего, а в shared_with — соседи
       (Claude делит по смыслу и спрашивает при сомнении);
    3) запись, которая не попала ни в один слот (созвоны съезжают по времени), — к ближайшему по
       началу свободному слоту не дальше drift_min минут, если такой один.
    Слот, получивший запись по имени, во 2–3 проходах уже не участвует."""
    tol = timedelta(minutes=tolerance_min)
    out: dict[int, list[TranscriptPiece]] = {}
    rest = []
    for tr in transcripts:
        label = tr.label
        by_name = [
            i for i, s in enumerate(slots) if label and s.day == tr.start_th.date() and _label_mentions(label, s.name)
        ] if label else []
        if len(by_name) == 1:
            out.setdefault(by_name[0], []).append(TranscriptPiece(tr, tr.text, []))
        else:
            rest.append(tr)
    claimed = set(out)
    drift = timedelta(minutes=drift_min)
    own = _own_files(slots, rest, drift)
    leftovers = []
    for tr in rest:
        covered = [i for i, s in enumerate(slots) if i not in claimed and _covers(tr, s, tol)]
        if len(covered) > 1:
            # Созвон затянулся: у следующего слота есть своя запись, начавшаяся ближе к его времени, —
            # хвост этой записи не его.
            covered = [i for i in covered if own.get(i) in (None, id(tr))] or covered[:1]
        if len(covered) > 1:
            # В записи звучит имя только одного из слотов — запись целиком его.
            named = [i for i in covered if count_mentions(tr.text, slots[i].name, None)]
            if len(named) == 1:
                covered = named
        if not covered:
            leftovers.append(tr)
            continue
        for k, i in enumerate(covered):
            s = slots[i]
            if len(covered) == 1:
                piece = tr.text
            else:
                lo = s.start_th - tol
                nxt = slots[covered[k + 1]].start_th + tol if k + 1 < len(covered) else tr.end_th + tol
                piece = cut_transcript(tr.text, tr.start_th.date(), lo, nxt)
            others = [slots[j] for j in covered if j != i]
            if piece.strip():
                out.setdefault(i, []).append(TranscriptPiece(tr, piece, others))
    for tr in leftovers:
        free = [
            (abs(tr.start_th - s.start_th), i) for i, s in enumerate(slots)
            if i not in out and s.day == tr.start_th.date() and abs(tr.start_th - s.start_th) <= drift
        ]
        free.sort()
        if len(free) > 1 and free[0][0] == free[1][0]:
            # два слота в одно время («Ирра, Ксения») — к тому, чьё имя звучит в записи
            tied = [i for d, i in free if d == free[0][0]]
            named = [i for i in tied if count_mentions(tr.text, slots[i].name, None)]
            free = [(free[0][0], named[0])] if len(named) == 1 else []
        if free:
            out[free[0][1]] = [TranscriptPiece(tr, tr.text, [])]
    return out


def _own_files(slots: list[Slot], transcripts: list[Transcript], drift: timedelta) -> dict[int, int]:
    """Слот → id записи, начавшейся ближе всех к его времени (не дальше drift)."""
    out = {}
    for i, s in enumerate(slots):
        near = sorted(
            (abs(tr.start_th - s.start_th), k, tr) for k, tr in enumerate(transcripts)
            if tr.start_th.date() == s.day and abs(tr.start_th - s.start_th) <= drift
        )
        if near:
            out[i] = id(near[0][2])
    return out


def _covers(tr: Transcript, s: Slot, tol: timedelta, min_overlap: timedelta = timedelta(minutes=3)) -> bool:
    """Запись покрывает слот: пересечение с его 15 минутами не меньше 3 минут, или короткая запись
    началась в пределах допуска от начала слота."""
    s_end = s.start_th + timedelta(minutes=SLOT_MINUTES)
    overlap = min(tr.end_th, s_end) - max(tr.start_th, s.start_th)
    return overlap >= min_overlap or abs(tr.start_th - s.start_th) <= tol


def _edit1(a: str, b: str) -> bool:
    """Не больше одной вставки/удаления/замены — опечатка в имени файла («Гульгназ»)."""
    if abs(len(a) - len(b)) > 1:
        return False
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return a[i + 1:] == b[i + 1:] or a[i:] == b[i + 1:] or a[i + 1:] == b[i:]


def name_like(word: str, name: str) -> bool:
    """Слово из названия файла или поста похоже на имя: то же имя, уменьшительное, падеж/-ка
    («Янка» → «Яна»), одна опечатка у длинных имён."""
    word, name = norm(word).lstrip("@"), norm(name).lstrip("@")
    if len(name) < 3 or len(word) < 3:
        return False
    if same_name(word, name) or _stem_eq(word, name):
        return True
    if name[-1] in "ая" and any(word.startswith(name[:-1] + suf) for suf in ("к", "ч", "ш", "х", "ень", "йк")):
        return True  # Яна → Янка, Ира → Ирочка/Ирка
    return len(name) >= 5 and _edit1(word, name)


def _label_mentions(label: str, name: str) -> bool:
    toks = _tokens(name)
    if not toks:
        return False
    return any(name_like(t, toks[0]) for t in _tokens(label))


# ---------------------------------------------------------------- посты в чате

def mentions(text: str, efiry_name: str, participant: Participant | None, known_nicks: set[str] | None = None) -> bool:
    """Упомянут ли участник в посте: @ник или имя (с уменьшительными) отдельным словом.
    Пост начинается с @ника другого участника из листа — он про того, даже если в тексте
    звучит похожее имя («@ianina_n … Яна» — не про Яну)."""
    toks = _tokens(text)
    own_nick = norm(participant.nick).lstrip("@") if participant and participant.nick else ""
    if known_nicks and toks and toks[0].startswith("@"):
        head = toks[0].lstrip("@")
        if head in known_nicks and head != own_nick:
            return False
    if participant and participant.nick:
        nick = norm(participant.nick).lstrip("@")
        if nick and any(t.lstrip("@") == nick for t in toks):
            return True
    names = set()
    if participant and participant.name:
        names.add(_tokens(participant.name)[0])
    en = _tokens(efiry_name)
    if en:
        names.add(en[0].lstrip("@"))
    for t in toks:
        for n in names:
            if len(n) >= 3 and (same_name(t, n) or _stem_eq(t, n)):
                return True
    # Участника нет в листе «Участники» (ник неизвестен): @YanaPanchenk0 → «Яна», @Irralvova → «Ирра» —
    # ник начинается с имени латиницей. Если ник в листе есть, чужой @ник — про другого человека.
    if not (participant and participant.nick):
        handles = [t.lstrip("@") for t in toks if t.startswith("@")]
        for n in names:
            for v in translit_variants(n):
                if len(v) >= 3 and any(h.startswith(v) and not _longer_name_prefix(h, v, n) for h in handles):
                    return True
    return False


_TRANSLIT = {
    "а": ["a"], "б": ["b"], "в": ["v"], "г": ["g"], "д": ["d"], "е": ["e"], "ж": ["zh"], "з": ["z"],
    "и": ["i"], "й": ["y", "i", "j"], "к": ["k"], "л": ["l"], "м": ["m"], "н": ["n"], "о": ["o"], "п": ["p"],
    "р": ["r"], "с": ["s"], "т": ["t"], "у": ["u"], "ф": ["f"], "х": ["kh", "h"], "ц": ["ts", "c"],
    "ч": ["ch"], "ш": ["sh"], "щ": ["sch", "shch"], "ъ": [""], "ы": ["y"], "ь": [""], "э": ["e"],
    "ю": ["yu", "iu", "ju"], "я": ["ya", "ia", "ja"],
}


def translit_variants(name: str, limit: int = 32) -> list[str]:
    out = [""]
    for ch in norm(name):
        opts = _TRANSLIT.get(ch, [ch])
        out = [o + v for o in out for v in opts][:limit]
    return out


def _longer_name_prefix(handle: str, variant: str, name: str) -> bool:
    """@irina_x не про «Иру»: после латинского имени на -а/-я сразу идёт n/i/y — это начало более
    длинного имени (Ирина, Анна), а не фамилия. Лучше вопрос Антону, чем чужая заметка."""
    rest = handle[len(variant):]
    return bool(rest) and name.endswith(("а", "я")) and rest[:1] in ("n", "i", "y")


def _stem_eq(word: str, name: str) -> bool:
    """«Глафире», «Глафиру», «Глафиры» → «Глафира»: падежные окончания имени."""
    if len(name) < 4:
        return False
    base = name[:-1] if name[-1] in "аяйь" else name
    return word.startswith(base) and len(word) - len(base) <= 2


# ---------------------------------------------------------------- готовый блок «Строки для таблицы»

_SHEET_HINTS = (
    (TAB_PARTICIPANTS, re.compile(r"участник", re.I)),
    (TAB_CALIBRATIONS, re.compile(r"калибровк", re.I)),
    (TAB_COMMUNICATIONS, re.compile(r"коммуникац", re.I)),
)


def has_ready_block(text: str) -> bool:
    return "строки для таблицы" in (text or "").lower() or bool(parse_ready_block(text or ""))


def parse_ready_block(text: str, headers: dict[str, list[str]] | None = None) -> dict[str, list[dict]]:
    """Блок «Строки для таблицы» из Claude-чата: строки данных через табуляцию, перед каждым блоком —
    подпись «Лист «Калибровки»». Строка без подписи определяется по числу колонок. Колонки — в порядке
    заголовка листа (headers), как велит ТЗ таблицы участников."""
    headers = headers or DEFAULT_COLUMNS
    out: dict[str, list[dict]] = {}
    current = None
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if "\t" not in line:
            for tab, rx in _SHEET_HINTS:
                if rx.search(line) and len(line) < 120:
                    current = tab
            continue
        cells = line.split("\t")
        if sum(1 for c in cells if c.strip()) < 2:
            continue
        tab = current or _guess_tab(len(cells), headers)
        if not tab:
            continue
        cols = headers.get(tab) or DEFAULT_COLUMNS[tab]
        out.setdefault(tab, []).append({c: (cells[i].strip() if i < len(cells) else "") for i, c in enumerate(cols)})
    return out


def _guess_tab(n: int, headers: dict[str, list[str]]) -> str | None:
    for tab in (TAB_COMMUNICATIONS, TAB_PARTICIPANTS, TAB_CALIBRATIONS):
        if n == len(headers.get(tab) or DEFAULT_COLUMNS[tab]):
            return tab
    return None


# ---------------------------------------------------------------- доводка строк по ТЗ таблицы

def sanitize_cell(value) -> str:
    """Ни одна ячейка не начинается с «=», «+» или «-» (иначе формула и #ERROR!) — ставим пробел."""
    s = "" if value is None else str(value)
    s = s.replace("\t", " ").replace("\r", " ").strip()
    if s[:1] in ("=", "+", "-"):
        s = " " + s
    return s


def _yes_no(v) -> str:
    s = norm(str(v or ""))
    if s in ("да", "yes", "true", "1"):
        return "да"
    if s in ("нет", "no", "false", "0"):
        return "нет"
    return ""


def has_both_paths(row: dict) -> bool:
    return bool(str(row.get("ответ_А") or "").strip()) and bool(str(row.get("ответ_Б") or "").strip())


_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{3,}")


def finalize_calibration(row: dict, day: date, nick: str, default_source: str) -> tuple[dict, list[str]]:
    r = {c: sanitize_cell(row.get(c, "")) for c in CALIBRATION_COLUMNS}
    questions = []
    r["дата"] = r["дата"].strip() or fmt_date(day)
    if nick:
        r["ник_tg"] = nick
    r["источник"] = r["источник"].strip() or default_source
    rel = r["достоверность"].strip()
    r["достоверность"] = rel if rel in RELIABILITY else "неясно"
    r["на_сайт"] = _yes_no(r["на_сайт"]) or "нет"
    questions += _foreign_handles(r, nick)
    return r, questions


def finalize_communication(row: dict, day: date, nick: str, default_source: str) -> tuple[dict, list[str]]:
    r = {c: sanitize_cell(row.get(c, "")) for c in COMMUNICATION_COLUMNS}
    r["адресат_слово"] = sanitize_cell(row.get("адресат_слово", ""))  # только для отчёта Антону, в лист не пишется
    questions = []
    r["id"] = ""  # проставляется при записи: следующий номер после последнего в листе
    r["дата"] = r["дата"].strip() or fmt_date(day)
    if nick:
        r["ник_tg"] = nick
    r["источник"] = r["источник"].strip() or default_source
    r["портал"] = _yes_no(r["портал"])
    chain = [x.strip() for x in re.split(r"\s*/\s*", r["продолжение_Б"]) if x.strip()]
    if any(not re.search(r"[A-Za-zА-Яа-яЁё]", x) for x in chain):  # «?» вместо реплики — не выдумываем, обрезаем
        chain = chain[: next(i for i, x in enumerate(chain) if not re.search(r"[A-Za-zА-Яа-яЁё]", x))]
        r["продолжение_Б"] = " / ".join(chain)
    if chain and norm(r["реакция_после_Б"]) == norm(chain[0]):
        r["реакция_после_Б"] = ""  # первая реплика перепалки уже в продолжении — не показывать дважды
    # ТЗ: на сайт — только коммуникация с обоими путями (ответ из А и ответ из Б)
    r["на_сайт"] = "да" if has_both_paths(r) else "нет"
    questions += _foreign_handles(r, nick)
    return r, questions


def finalize_growth(row: dict, nick: str) -> dict:
    """Успех участника одной строкой «научился …, теперь …», не длиннее 400 символов."""
    text = " ".join(str(row.get("текст") or row.get("путь_роста") or "").split())
    if len(text) > GROWTH_MAX:
        text = text[: GROWTH_MAX - 1].rstrip() + "…"
    tooth = " ".join(str(row.get("зуб") or "").split())
    return {"ник_tg": nick or sanitize_cell(row.get("ник_tg", "")).strip(), "зуб": tooth, "путь_роста": sanitize_cell(text)}


def _nick_key(n: str) -> str:
    return norm(n).lstrip("@")


def plan_growth(values: list[list[str]], rows: list[dict]) -> tuple[list[tuple[int, int, str]], list[list[str]]]:
    """Как дописать успехи в лист «Карточки»: (обновления (строка, колонка, новое значение), новые строки).
    Строка карточки — та же пара ник+зуб; если у ника одна карточка без зуба — она. Иначе новая строка
    (ник + зуб). Старые строки «Пути роста» не трогаем, новая — через перевод строки; повтор не пишем."""
    header = [h.strip() for h in (values[0] if values else CARD_COLUMNS)]
    ci = {h: i for i, h in enumerate(header)}
    cells: dict[int, str] = {}
    updates: dict[int, str] = {}
    appends: list[dict] = []
    def find(nick, tooth):
        mine = [i for i, r in enumerate(values) if i >= 2 and _nick_key(_cell(values, i, ci["ник_tg"])) == _nick_key(nick)]
        exact = [i for i in mine if norm(_cell(values, i, ci["зуб"])) == norm(tooth)]
        if exact:
            return exact[0]
        blank = [i for i in mine if not _cell(values, i, ci["зуб"]).strip()]
        return blank[0] if len(mine) == 1 and blank else None
    for r in rows:
        line = r["путь_роста"].strip()
        if not line:
            continue
        i = find(r["ник_tg"], r["зуб"])
        if i is None:
            same = next((a for a in appends if _nick_key(a["ник_tg"]) == _nick_key(r["ник_tg"]) and a["зуб"] == r["зуб"]), None)
            if same:
                if line not in same["путь_роста"].split("\n"):
                    same["путь_роста"] += "\n" + line
            else:
                appends.append({"ник_tg": r["ник_tg"], "зуб": r["зуб"], "путь_роста": line})
            continue
        cur = cells.get(i, _cell(values, i, ci["путь_роста"]))
        if line in cur.split("\n"):
            continue
        cells[i] = (cur.rstrip("\n") + "\n" + line) if cur.strip() else line
        updates[i] = cells[i]
    return ([(i + 1, ci["путь_роста"] + 1, v) for i, v in sorted(updates.items())],
            [row_values(header, a) for a in appends])


def finalize_participant(row: dict) -> dict:
    return {c: sanitize_cell(row.get(c, "")) for c in PARTICIPANT_COLUMNS}


def _foreign_handles(r: dict, nick: str) -> list[str]:
    own = norm(nick)
    found = {h for v in r.values() for h in _HANDLE_RE.findall(v) if norm(h) != own}
    if found and (r.get("ник_tg") or "") not in found:
        return ["В строке есть чужой @ник — у третьих лиц по ТЗ только роль. Проверь перед записью."]
    return []


def row_values(header: list[str], row: dict) -> list[str]:
    """Строка в порядке колонок листа (по заголовку, как в живой таблице)."""
    return [sanitize_cell(row.get(h, "")) if h else "" for h in header]


def next_comm_id(values: list[list[str]], header: list[str]) -> int:
    if "id" not in header:
        return 1
    j = header.index("id")
    best = 0
    for row in values[FIRST_DATA_ROW - 1:]:
        if j < len(row):
            try:
                best = max(best, int(float(row[j])))
            except ValueError:
                pass
    return best + 1


def first_empty_row(values: list[list[str]]) -> int:
    last = 0
    for i, row in enumerate(values, start=1):
        if any((c or "").strip() for c in row):
            last = i
    return max(FIRST_DATA_ROW, last + 1)


# ---------------------------------------------------------------- ответ Claude

def parse_llm_json(text: str) -> dict:
    """Первый JSON-объект в ответе модели (на случай ```json …``` или текста вокруг)."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    start = s.find("{")
    if start < 0:
        raise ValueError("в ответе нет JSON")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("JSON не закрыт")


@dataclass
class Extraction:
    calibrations: list[dict] = field(default_factory=list)
    communications: list[dict] = field(default_factory=list)
    participant_row: dict | None = None
    growth: list[dict] = field(default_factory=list)  # «Путь роста»: {ник_tg, зуб, путь_роста}
    questions: list[str] = field(default_factory=list)


def build_extraction(data: dict, day: date, participant: Participant | None, default_source: str) -> Extraction:
    """Ответ Claude или разобранный готовый блок → доведённые строки и вопросы Антону."""
    nick = participant.nick if participant else ""
    ex = Extraction()
    ex.questions = [str(q).strip() for q in (data.get("вопросы") or []) if str(q).strip()]
    for raw in data.get("калибровки") or []:
        if not isinstance(raw, dict):
            continue
        if not any(str(raw.get(k) or "").strip() for k in ("точка_А", "точка_Б", "точка_АА")):
            continue
        row, q = finalize_calibration(raw, day, nick, default_source)
        ex.calibrations.append(row)
        ex.questions += q
    for raw in data.get("коммуникации") or []:
        if not isinstance(raw, dict):
            continue
        if not any(str(raw.get(k) or "").strip() for k in ("фраза_адресата", "ответ_А", "ответ_Б")):
            continue
        row, q = finalize_communication(raw, day, nick, default_source)
        ex.communications.append(row)
        ex.questions += q
        if not has_both_paths(row):
            missing = "ответа из Б" if row["ответ_А"].strip() else "ответа из А"
            role = row["адресат_роль"].strip() or "адресат не ясен"
            ex.questions.append(f"Коммуникация «{role}»: нет {missing} — на сайт не пойдёт, пока не будет обоих путей.")
    for raw in data.get("путь_роста") or []:
        if not isinstance(raw, dict):
            continue
        g = finalize_growth(raw, nick)
        if not g["путь_роста"]:
            continue
        if not g["ник_tg"]:
            ex.questions.append("Успех для «Пути роста» есть, а ника участника нет — в карточку не записать.")
            continue
        ex.growth.append(g)
    prow = data.get("участник")
    if isinstance(prow, dict) and any(str(v).strip() for v in prow.values()):
        if participant:
            ex.questions.append("Участник уже есть в таблице — строку «Участники» не добавляю; что поменять в ней, напиши сам.")
        else:
            ex.participant_row = finalize_participant(prow)
    if not participant and (ex.calibrations or ex.communications):
        new_nick = (ex.participant_row or {}).get("ник_tg", "").strip()
        if new_nick:
            for r in ex.calibrations + ex.communications:
                r["ник_tg"] = r["ник_tg"] or new_nick
        else:
            ex.questions.append("Участника нет в листе «Участники» и ник не известен — колонка ник_tg пустая.")
    seen, uniq = set(), []
    for q in ex.questions:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    ex.questions = uniq
    return ex


# ---------------------------------------------------------------- тексты сообщений

def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


SOURCE_LABELS = {"запись": "запись", "чат": "пост в чате", "личка": "твои заметки", "блок": "блок из Claude-чата"}


MARK_OK, MARK_WARN, MARK_NONE = "✅", "⚠️", "▫️"
DAY_LEGEND = "🎙 запись · 💬 заметка · 📖 история"


@dataclass
class DayItem:
    name: str
    hhmm: str
    rec: str = ""      # "" нет / "ok" совпала с расписанием / "other" похоже, там другой человек
    note: bool = False  # заметка в «Исцелении отношений» или присланная Антоном
    story: str = ""    # "" нет / "full" триггер + А + Б / "partial" одного пути нет
    status: str = "pending"


def day_list_text(day: date, items: list[DayItem]) -> str:
    """Список дня: у каждого три отметки — запись, заметка, история (триггер + реакции из А и Б)."""
    lines = [f"Созвоны {day:%d.%m}: {len(items)}   ({DAY_LEGEND})"]
    empty = 0
    for it in items:
        if it.status == "no_call":
            lines.append(f"➖ {it.name} {it.hhmm} — не было")
            continue
        if it.status == "skipped":
            lines.append(f"⏭ {it.name} {it.hhmm} — пропущено")
            continue
        rec = {"ok": MARK_OK, "other": MARK_WARN}.get(it.rec, MARK_NONE)
        story = {"full": MARK_OK, "partial": MARK_WARN}.get(it.story, MARK_NONE)
        done = " · в таблице" if it.status == "added" else ""
        lines.append(f"{it.name} {it.hhmm}   🎙{rec} 💬{MARK_OK if it.note else MARK_NONE} 📖{story}{done}")
        if not it.rec and not it.note and not it.story:
            empty += 1
    if any(it.rec == "other" for it in items):
        lines.append("\n🎙⚠️ — в записи, похоже, другой человек: проверь.")
    if empty:
        lines.append("\nГде пусто — ответь на сообщение с именем ниже: заметка или «не было».")
    return "\n".join(lines)


def count_mentions(text: str, efiry_name: str, participant: "Participant | None") -> int:
    """Сколько раз в тексте звучит имя (уменьшительные, падежи) или @ник участника."""
    names = set()
    if participant and participant.name:
        names.add(_tokens(participant.name)[0])
    en = _tokens(efiry_name)
    if en:
        names.add(en[0].lstrip("@"))
    nick = norm(participant.nick).lstrip("@") if participant and participant.nick else ""
    n = 0
    for t in _tokens(text):
        if nick and t.lstrip("@") == nick:
            n += 1
        elif any(len(x) >= 3 and (same_name(t, x) or _stem_eq(t, x) or name_like(t, x)) for x in names):
            n += 1
    return n


def recording_state(own: int, others: dict[str, int], by_label: bool) -> str:
    """ok — запись совпала с расписанием (по имени в названии, в тексте или по времени);
    other — своего имени в тексте нет, а чужое звучит не меньше двух раз."""
    if by_label or own:
        return "ok"
    if others and max(others.values()) >= 2:
        return "other"
    return "ok"


def story_state(communications: list[dict]) -> str:
    if any(has_both_paths(c) and (c.get("фраза_адресата") or c.get("адресат_роль")) for c in communications):
        return "full"
    return "partial" if communications else ""


def dative(name: str, gender: str = "") -> str:
    """«по Сереге», «по Ксении», «по Ане Х»: дательный падеж первого слова, остальное как есть."""
    parts = name.split(" ", 1)
    w = parts[0]
    low = w.lower()
    if low.endswith("ия"):
        w = w[:-1] + "и"
    elif low.endswith(("а", "я")):
        w = w[:-1] + "е"
    elif low.endswith("ь"):
        w = w[:-1] + "и"
    elif gender.lower().startswith("м") and low and low[-1] in "бвгджзклмнпрстфхцчшщ" and not w.startswith("@"):
        w = w + "у"
    return " ".join([w] + parts[1:])


PERSON_EMOJI = {"ж": "👧", "м": "👦"}
TRIGGER_EMOJI = "🐲"
TOOTH_EMOJI = "🦷"


def story_text(name: str, gender: str, communications: list[dict], calibrations: int = 0, questions: list[str] | None = None,
               growth: list[dict] | None = None) -> str:
    """Отчёт по участнику, как Антон пишет сам («ПО СЕРЕГЕ»): 🐲 ТРИГГЕР и его действие, участник из
    точки А, потом участник из точки Б (🦷) и цепочка реакций триггера. Продолжение_Б — по очереди
    триггер, участник, триггер… (так же читает сайт)."""
    me = PERSON_EMOJI.get((gender or "").strip().lower()[:1], "🧑")
    out = [f"ПО {dative(name, gender).upper()}"]
    for k, c in enumerate(communications):
        if k:
            out.append("— — —")
        who = (c.get("адресат_слово") or c.get("адресат_роль") or "триггер").strip().upper()
        intro = (c.get("до_триггера") or "").strip()
        if intro:
            out.append(f"{me} {intro}")
        tooth = (c.get("зуб") or "").strip()
        head = f"{TRIGGER_EMOJI} {who} {c.get('фраза_адресата', '').strip()}".rstrip()
        out.append(head + (f"   ({TOOTH_EMOJI}{tooth})" if tooth else ""))
        a = (c.get("ответ_А") or "").strip()
        out.append(f"{me} {a}" if a else f"{me} точка А — ❓ нет")
        if (c.get("реакция_после_А") or "").strip():
            out.append(f"{TRIGGER_EMOJI} {who} {c['реакция_после_А'].strip()}")
        aa = (c.get("ответ_АА") or "").strip()
        if aa:
            out.append(f"{me}😩 {aa}")
        out.append("")
        b = (c.get("ответ_Б") or "").strip()
        out.append(f"{me}{TOOTH_EMOJI} {b}" if b else f"{me}{TOOTH_EMOJI} точка Б — ❓ нет")
        if (c.get("реакция_после_Б") or "").strip():
            out.append(f"{TRIGGER_EMOJI} {who} {c['реакция_после_Б'].strip()}")
        chain = [x.strip() for x in re.split(r"\s*/\s*|\n+", c.get("продолжение_Б") or "") if x.strip()]
        for i, beat in enumerate(chain):
            if i % 2 == 0:
                out.append(f"{TRIGGER_EMOJI} {who} {beat}")
            else:
                out.append("")
                out.append(f"{me}{TOOTH_EMOJI} {beat}")
    if not communications:
        out.append("Истории (триггер + реакции из А и Б) не нашёл.")
    wins = [g for g in (growth or []) if (g.get("путь_роста") or "").strip()]
    if wins:
        out.append("\n🌱 Путь роста:")
        out += [f"🌱 {g['путь_роста'].strip()}" + (f" (🦷{g['зуб']})" if g.get("зуб") else "") for g in wins]
    if calibrations:
        out.append(f"\n＋ {calibrations} {plural(calibrations, 'калибровка', 'калибровки', 'калибровок')}")
    qs = [_q(q, 160) for q in (questions or []) if q][:3]
    if qs:
        out.append("")
        out += [f"❓ {q}" for q in qs]
        out.append("Ответь на это сообщение — поправлю.")
    return "\n".join(out)


def _q(s: str, limit: int = 60) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def summary_lines(name: str, calibrations: list[dict], communications: list[dict], participant_row: dict | None) -> list[str]:
    out = []
    for c in communications:
        role = c.get("адресат_роль") or "?"
        a, b = (c.get("ответ_А") or "").strip(), (c.get("ответ_Б") or "").strip()
        if a and b:
            out.append(f"• {name} — {role}: А «{_q(a)}» / Б «{_q(b)}» ✅ на сайт")
        elif b:
            out.append(f"• {name} — {role}: только Б (нет ответа из А) — на сайт не пойдёт")
        elif a:
            out.append(f"• {name} — {role}: только А (нет ответа из Б) — на сайт не пойдёт")
        else:
            out.append(f"• {name} — {role}: без ответов — на сайт не пойдёт")
    if calibrations:
        n = len(calibrations)
        out.append(f"• {name} — {n} {plural(n, 'калибровка', 'калибровки', 'калибровок')}")
    if participant_row:
        out.append(f"• {name} — новая строка в «Участники»")
    return out


def draft_preview(sheet: str, row: dict) -> str:
    keys = {
        TAB_CALIBRATIONS: ["дата", "название", "адресат_роль", "точка_А", "точка_АА", "точка_Б", "источник", "достоверность"],
        TAB_COMMUNICATIONS: ["адресат_роль", "лицо_адресата", "фраза_адресата", "ответ_А", "лицо_после_А", "ответ_Б", "лицо_после_Б", "реакция_после_Б", "зуб", "на_сайт"],
        TAB_PARTICIPANTS: PARTICIPANT_COLUMNS,
        TAB_CARDS: ["ник_tg", "зуб", "путь_роста"],
    }[sheet]
    lines = [f"Лист «{sheet}»:"]
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def split_message(text: str, limit: int = 4000) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts
