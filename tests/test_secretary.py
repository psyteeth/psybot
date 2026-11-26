"""Тесты «Секретаря созвонов»: разбор таблиц, сопоставление, расшифровки, доводка строк и сквозной
прогон дня на фейковых Google / Telegram / Claude (сеть не нужна).

Запуск из корня psybot:  .venv/bin/python -m unittest tests.test_secretary -v
Все имена — выдуманные."""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, datetime, time
from types import SimpleNamespace

from bot import db

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "secretary_test.db")
db.init_db()

from bot import secretary as S  # noqa: E402
from bot import secretary_bot as SB  # noqa: E402
from bot import secretary_db as sdb  # noqa: E402

sdb.init()


def efiry_grid() -> list[list[str]]:
    """Как в живой таблице: A — день недели, B — тренер, недели по три колонки [дата|время|имя]."""
    w = 14
    rows = [[""] * w for _ in range(12)]
    rows[0][1] = "ТРЕНЕРЫ"
    rows[0][5], rows[0][8] = "НЕДЕЛЯ 23 - 29 ноября", "НЕДЕЛЯ 30 ноября"
    # ЧТ 26 ноября (колонки F,G,H), блок Антона
    rows[1][0], rows[1][1] = "ЧТ", "АНТОН ❤️ ОРАКУЛ ❤️ ПО ДУШАМ"
    rows[1][5], rows[1][6], rows[1][7] = "26 ноября", "8:00", "Глафира"
    rows[2][6], rows[2][7] = "8:15", "Дуся Х"
    rows[3][6], rows[3][7] = "8:30", "Фекла"
    rows[4][6], rows[4][7] = "8:45", ""  # пустой слот
    # тот же день, блок другого тренера
    rows[5][1] = "ЗОЯ ❤️ СКАЗКИ"
    rows[5][6], rows[5][7] = "20:00", "Василиса"
    # ПТ — даты в колонке F нет (выход за начало дня не должен дать дату четверга)
    rows[6][0], rows[6][1] = "ПТ", "АНТОН ❤️"
    rows[6][6], rows[6][7] = "8:30", "Марфа"
    # ВС 30 ноября в следующей неделе (I,J,K), заголовок эфира без времени слева
    rows[8][0], rows[8][1] = "ВС", "АНТОН ❤️ ПОПИЗДЕЛКИ"
    rows[8][8], rows[8][9] = "30 ноября", "Эфир в Психостоматологии"
    rows[9][9], rows[9][10] = "7:07", "@marfa_m"
    return rows


PARTICIPANTS = [
    S.PARTICIPANT_COLUMNS,
    ["@nick", "Дуся", "Персонаж"] + [""] * 15,  # строка подсказок
    ["@glafira_g", "Глафира", "Паулетта", "ж", "текущий", "", "Антон", "46", "46", "", "", "", "", "мама", "", "", "да", ""],
    ["@dusya_one", "Дуся", "", "ж"] + [""] * 14,
    ["@dusya_two", "Евдокия", "", "ж"] + [""] * 14,
    ["@marfa_m", "Марфа", "", "ж"] + [""] * 14,
]


class EfiryTests(unittest.TestCase):
    def test_tab_title_with_trailing_spaces(self):
        titles = ["НОЯБРЬ 2026 ", "ОКТЯБРЬ 2026  ", "НОЯБРЬ "]
        self.assertEqual(S.efiry_tab_title(titles, date(2026, 11, 26)), "НОЯБРЬ 2026 ")
        self.assertIsNone(S.efiry_tab_title(titles, date(2026, 12, 1)))

    def test_grid(self):
        slots = S.parse_efiry_grid(efiry_grid(), 2026, 11)
        got = [(s.day.day, s.hhmm, s.name, s.trainer) for s in slots]
        self.assertIn((26, "08:00", "Глафира", "АНТОН"), got)
        self.assertIn((26, "08:15", "Дуся Х", "АНТОН"), got)
        self.assertIn((26, "20:00", "Василиса", "ЗОЯ"), got)
        self.assertIn((30, "07:07", "@marfa_m", "АНТОН"), got)
        self.assertFalse(any(name == "Марфа" for _, _, name, _ in got), "пятница без даты не должна взять дату четверга")
        self.assertFalse(any("Эфир" in name for _, _, name, _ in got))
        self.assertEqual(sum(1 for g in got if g[0] == 26), 4)  # пустой слот 8:45 пропущен

    def test_day_filter_trainer_and_until(self):
        slots = S.parse_efiry_grid(efiry_grid(), 2026, 11)
        day = S.slots_for_day(slots, date(2026, 11, 26), "АНТОН")
        self.assertEqual([s.hhmm for s in day], ["08:00", "08:15", "08:30"])
        self.assertEqual(len(S.slots_for_day(slots, date(2026, 11, 26), "")), 4)
        early = S.slots_for_day(slots, date(2026, 11, 26), "АНТОН", until_msk=time(8, 10))
        self.assertEqual([s.hhmm for s in early], ["08:00"])

    def test_slot_thai_time(self):
        s = S.Slot(date(2026, 11, 26), time(8, 45), "x")
        self.assertEqual(s.start_th, datetime(2026, 11, 26, 12, 45))

    def test_year_rollover(self):
        self.assertEqual(S._parse_day("1 января", 2026, 12), date(2027, 1, 1))
        self.assertEqual(S._parse_day("31 декабря", 2027, 1), date(2026, 12, 31))


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.ps = S.parse_participants(PARTICIPANTS)

    def test_data_from_third_row(self):
        self.assertEqual([p.row for p in self.ps], [3, 4, 5, 6])
        self.assertNotIn("@nick", [p.nick for p in self.ps])

    def test_exact_and_diminutive(self):
        self.assertEqual(S.match_participant("Глафира", self.ps).participant.nick, "@glafira_g")
        self.assertEqual(S.match_participant("Марфа ❤️", self.ps).participant.nick, "@marfa_m")

    def test_nick(self):
        self.assertEqual(S.match_participant("@dusya_two", self.ps).participant.nick, "@dusya_two")
        self.assertEqual(S.match_participant("dusya_one", self.ps).participant.nick, "@dusya_one")

    def test_ambiguous_not_guessed(self):
        m = S.match_participant("Дуся Х", self.ps)  # «Дуся» и «Евдокия» — одно имя, двое в таблице
        self.assertEqual(m.status, "ambiguous")
        self.assertEqual({p.nick for p in m.candidates}, {"@dusya_one", "@dusya_two"})
        self.assertIsNone(m.participant)

    def test_none(self):
        self.assertEqual(S.match_participant("Фекла", self.ps).status, "none")


class TranscriptTests(unittest.TestCase):
    def test_recording_name(self):
        start, label = S.parse_recording_name("Meeting in Yandex Telemost 26.11.26 12-48-05 — recording.webm")
        self.assertEqual(start, datetime(2026, 11, 26, 12, 48, 5))
        self.assertEqual(label, "")
        start, label = S.parse_recording_name("Глафира 26.11.webm")
        self.assertIsNone(start)
        self.assertEqual(label, "Глафира 26.11")

    def test_whisper_output_and_lines(self):
        out = "[00:00:00.000 --> 00:00:04.200]   Привет.\n[00:01:05.500 --> 00:01:09.000]  Мама говорит: хватит лежать.\nшум\n"
        segs = S.parse_whisper_output(out)
        self.assertEqual(segs, [(0.0, "Привет."), (65.5, "Мама говорит: хватит лежать.")])
        lines = S.format_transcript_lines(segs, datetime(2026, 11, 26, 12, 0, 0))
        self.assertEqual(lines, ["[12:00:00] Привет.", "[12:01:05] Мама говорит: хватит лежать."])

    def test_chunk(self):
        text = "\n".join(["x" * 30] * 10)
        parts = S.chunk_text(text, size=100)
        self.assertTrue(all(len(p) <= 100 for p in parts))
        self.assertEqual("\n".join(parts), text)

    def test_transcripts_from_values_joins_parts(self):
        values = [S.TRANSCRIPTS_HEADER,
                  ["f.webm", "26.11.2026", "12:00:00", "900", "2", "вторая"],
                  ["f.webm", "26.11.2026", "12:00:00", "900", "1", "первая"]]
        [t] = S.transcripts_from_values(values)
        self.assertEqual(t.text, "первая\nвторая")
        self.assertEqual(t.end_th, datetime(2026, 11, 26, 12, 15))

    def _slots(self):
        d = date(2026, 11, 26)
        return [S.Slot(d, time(8, 0), "Глафира"), S.Slot(d, time(8, 15), "Дуся Х"), S.Slot(d, time(8, 30), "Фекла")]

    def test_assign_single_slot(self):
        tr = S.Transcript("Meeting in Yandex Telemost 26.11.26 12-01-00 — recording.webm", datetime(2026, 11, 26, 12, 1), 600, "[12:01:00] а")
        got = S.assign_transcripts(self._slots(), [tr])
        self.assertEqual(list(got), [0])
        self.assertEqual(got[0][0].shared_with, [])

    def test_assign_shared_recording_is_cut(self):
        text = "[12:02:00] про маму\n[12:16:00] про мужа\n[12:31:00] про начальника"
        tr = S.Transcript("Meeting in Yandex Telemost 26.11.26 12-00-00 — recording.webm", datetime(2026, 11, 26, 12, 0), 45 * 60, text)
        got = S.assign_transcripts(self._slots(), [tr], tolerance_min=3)
        self.assertEqual(sorted(got), [0, 1, 2])
        self.assertIn("про маму", got[0][0].text)
        self.assertNotIn("про начальника", got[0][0].text)
        self.assertIn("про мужа", got[1][0].text)
        self.assertEqual({s.name for s in got[1][0].shared_with}, {"Глафира", "Фекла"})

    def test_assign_renamed_file_by_name(self):
        tr = S.Transcript("Фекла созвон.webm", datetime(2026, 11, 26, 18, 0), 600, "текст")
        got = S.assign_transcripts(self._slots(), [tr])
        self.assertEqual(list(got), [2])


class MentionAndBlockTests(unittest.TestCase):
    def test_mentions(self):
        p = S.Participant(3, {"ник_tg": "@glafira_g", "имя": "Глафира"})
        self.assertTrue(S.mentions("Сегодня с Глафирой разобрали маму", "Глафира", p))
        self.assertTrue(S.mentions("@glafira_g молодец", "Глафира", p))
        self.assertFalse(S.mentions("Сегодня с Феклой", "Глафира", p))
        self.assertFalse(S.mentions("мариновать огурцы", "Глафира", p))

    def test_ready_block_by_sheet_header_order(self):
        com_header = S.COMMUNICATION_COLUMNS  # как в живой таблице: реакции в конце
        text = (
            "Строки для таблицы\n"
            "Лист «Калибровки» — вставить в первую пустую строку\n"
            "21.11.2026\t@nick\tуборка с мамой\tмама\tДа, мам\tВиню себя\tМам, я злюсь\t\tсозвон\tчётко\tда\n"
            "Лист «Коммуникации»\n"
            "\t21.11.2026\t@nick\tмама\tгрустный\tХватит лежать\tХорошо\tулыбка\t\t\tНе, не хочу\tшок\t\tда\t46\tсозвон\tда\tМолодец\tЧтооо?\tЧто ты себе позволяешь? / Бессовестная!\n"
        )
        got = S.parse_ready_block(text, {S.TAB_COMMUNICATIONS: com_header, S.TAB_CALIBRATIONS: S.CALIBRATION_COLUMNS})
        self.assertEqual(got[S.TAB_CALIBRATIONS][0]["точка_Б"], "Мам, я злюсь")
        c = got[S.TAB_COMMUNICATIONS][0]
        self.assertEqual(c["ответ_Б"], "Не, не хочу")
        self.assertEqual(c["портал"], "да")
        self.assertEqual(c["реакция_после_Б"], "Чтооо?")
        self.assertTrue(S.has_ready_block(text))
        self.assertFalse(S.has_ready_block("просто заметка"))


class FormattingTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(S.sanitize_cell("=SUM(A1)"), " =SUM(A1)")
        self.assertEqual(S.sanitize_cell("- да иди ты"), " - да иди ты")
        self.assertEqual(S.sanitize_cell("+1"), " +1")
        self.assertEqual(S.sanitize_cell("a\tb"), "a b")
        self.assertEqual(S.sanitize_cell(None), "")

    def test_na_sait_only_with_both_paths(self):
        d = date(2026, 11, 26)
        both, _ = S.finalize_communication({"ответ_А": "Да", "ответ_Б": "Нет", "на_сайт": "нет"}, d, "@x", "созвон")
        only_b, _ = S.finalize_communication({"ответ_Б": "Нет", "на_сайт": "да"}, d, "@x", "созвон")
        self.assertEqual(both["на_сайт"], "да")
        self.assertEqual(only_b["на_сайт"], "нет")
        self.assertEqual(both["дата"], "26.11.2026")
        self.assertEqual(both["ник_tg"], "@x")
        self.assertEqual(both["id"], "")

    def test_calibration_defaults(self):
        r, _ = S.finalize_calibration({"точка_А": "молчу", "достоверность": "наверное"}, date(2026, 11, 26), "@x", "созвон")
        self.assertEqual(r["достоверность"], "неясно")
        self.assertEqual(r["на_сайт"], "нет")
        self.assertEqual(r["источник"], "созвон")

    def test_build_extraction_questions(self):
        p = S.Participant(3, {"ник_tg": "@glafira_g", "имя": "Глафира"})
        data = {
            "калибровки": [{"точка_А": "молчу", "точка_Б": "говорю"}, {"название": "пусто"}],
            "коммуникации": [{"адресат_роль": "начальник", "ответ_Б": "Нет", "фраза_адресата": "Сделай"}],
            "участник": {"имя": "Глафира"},
            "вопросы": ["Какой зуб: 43 или 33?"],
        }
        ex = S.build_extraction(data, date(2026, 11, 26), p, "созвон")
        self.assertEqual(len(ex.calibrations), 1)  # строка без точек выброшена
        self.assertEqual(ex.communications[0]["на_сайт"], "нет")
        self.assertIsNone(ex.participant_row)  # участник уже есть — строку не даём
        self.assertIn("Какой зуб: 43 или 33?", ex.questions)
        self.assertTrue(any("нет ответа из А" in q for q in ex.questions))
        self.assertTrue(any("уже есть в таблице" in q for q in ex.questions))

    def test_foreign_handle_flagged(self):
        r, q = S.finalize_calibration({"точка_А": "как @other_girl сказала"}, date(2026, 11, 26), "@me", "созвон")
        self.assertTrue(q)

    def test_row_values_and_positions(self):
        header = ["id", "дата", "ник_tg", "", "на_сайт"]
        self.assertEqual(S.row_values(header, {"id": "5", "дата": "1", "на_сайт": "да", "лишнее": "x"}), ["5", "1", "", "", "да"])
        values = [["id"], ["подсказка"], ["3"], ["x"], [""], ["12"], ["", ""]]
        self.assertEqual(S.next_comm_id(values, ["id"]), 13)
        self.assertEqual(S.first_empty_row(values), 7)
        self.assertEqual(S.first_empty_row([["h"], ["hint"]]), 3)

    def test_parse_llm_json(self):
        self.assertEqual(S.parse_llm_json('```json\n{"a": "}"}\n```'), {"a": "}"})
        self.assertEqual(S.parse_llm_json('Вот: {"a": {"b": 1}} и всё'), {"a": {"b": 1}})
        with self.assertRaises(ValueError):
            S.parse_llm_json("нет json")

    def test_texts(self):
        text = S.day_list_text(date(2026, 11, 26), [
            ("Глафира", "10:00", ["запись"], "material"),
            ("Марфа", "10:15", ["запись", "чат"], "material"),
            ("Фекла", "10:30", [], "pending"),
        ])
        self.assertIn("✅ Глафира 10:00 — запись", text)
        self.assertIn("✅ Марфа 10:15 — запись + пост в чате", text)
        self.assertIn("❓ Фекла 10:30 — ничего нет", text)
        lines = S.summary_lines("Хуанита", [{}, {}], [{"адресат_роль": "мама", "ответ_А": "Да, хорошо", "ответ_Б": "Нет, не хочу"},
                                                       {"адресат_роль": "начальник", "ответ_Б": "Нет"}], None)
        self.assertIn("• Хуанита — мама: А «Да, хорошо» / Б «Нет, не хочу» ✅ на сайт", lines)
        self.assertIn("• Хуанита — начальник: только Б (нет ответа из А) — на сайт не пойдёт", lines)
        self.assertIn("• Хуанита — 2 калибровки", lines)


# ---------------------------------------------------------------- сквозной прогон на фейках

class FakeWS:
    def __init__(self, title, values):
        self.title = title
        self.values = [list(r) for r in values]
        self.row_count = max(len(self.values), 100)

    def get_all_values(self):
        last = 0
        for i, r in enumerate(self.values, 1):
            if any(c for c in r):
                last = i
        return [list(r) for r in self.values[:last]]

    def row_values(self, n):
        return list(self.values[n - 1]) if n <= len(self.values) else []

    def add_rows(self, n):
        self.row_count += n

    def update(self, range_name, values, value_input_option=None):
        start = int(range_name[1:])
        for i, row in enumerate(values):
            idx = start - 1 + i
            while len(self.values) <= idx:
                self.values.append([])
            self.values[idx] = list(row)

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.values = self.get_all_values() + [list(r) for r in rows]


class FakeBook:
    def __init__(self, tabs):
        self.tabs = {t: FakeWS(t, v) for t, v in tabs.items()}

    def worksheets(self):
        return list(self.tabs.values())

    def worksheet(self, title):
        import gspread
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS(title, [])
        return self.tabs[title]


class FakeClient:
    def __init__(self, books, by_name):
        self.books, self.by_name = books, by_name

    def open_by_key(self, key):
        return self.books[key]

    def open(self, title):
        return self.by_name[title]


class FakeBot:
    def __init__(self):
        self.sent = []
        self._id = 100

    async def send_message(self, chat_id, text, reply_markup=None):
        self._id += 1
        self.sent.append(SimpleNamespace(message_id=self._id, text=text, markup=reply_markup))
        return SimpleNamespace(message_id=self._id)


class FakeQueryMessage:
    def __init__(self, bot):
        self.bot = bot

    async def reply_text(self, text, **kw):
        return await self.bot.send_message(0, text)


def buttons(msg):
    return [b.callback_data for row in (msg.markup.inline_keyboard if msg.markup else []) for b in row]


class EndToEndTests(unittest.TestCase):
    DAY = date(2026, 11, 26)

    def setUp(self):
        with db.get_conn() as conn:
            for t in ("sec_days", "sec_slots", "sec_materials", "sec_chat_posts", "sec_inbox", "sec_drafts", "sec_questions", "sec_aliases"):
                conn.execute(f"DELETE FROM {t}")
        header_c = S.COMMUNICATION_COLUMNS
        self.book = FakeBook({
            S.TAB_PARTICIPANTS: PARTICIPANTS,
            S.TAB_CALIBRATIONS: [S.CALIBRATION_COLUMNS, ["подсказка"] * 11],
            S.TAB_COMMUNICATIONS: [header_c, ["подсказка"] * len(header_c), ["7", "20.11.2026", "@glafira_g"]],
            S.TAB_FACES: [["код"], ["улыбка"], ["шок"]],
            S.TAB_ROLES: [["роль"], ["мама"], ["начальник"]],
        })
        efiry = FakeBook({"НОЯБРЬ 2026 ": efiry_grid()})
        transcripts = FakeBook({S.TRANSCRIPTS_TAB: [
            S.TRANSCRIPTS_HEADER,
            ["Meeting in Yandex Telemost 26.11.26 12-01-00 — recording.webm", "26.11.2026", "12:01:00", "700", "1", "[12:02:00] Мама: хватит лежать. Я: хорошо. А из Б: не, не хочу."],
        ]})
        SB.google._client = FakeClient(
            {SB.PARTICIPANTS_SPREADSHEET_ID: self.book, SB.EFIRY_SPREADSHEET_ID: efiry, SB.TRANSCRIPTS_SPREADSHEET_ID: transcripts},
            {S.TRANSCRIPTS_SPREADSHEET_NAME: transcripts},
        )
        SB.ADMIN_CHAT_ID = "1"
        SB.SECRETARY_TRAINER = "АНТОН"
        self.llm_calls = []

        async def fake_ask(system, user_text, model, max_tokens=500):
            self.llm_calls.append(user_text)
            return json.dumps({
                "калибровки": [{"название": "уборка с мамой", "адресат_роль": "мама", "точка_А": "хорошо", "точка_Б": "не, не хочу", "достоверность": "чётко"}],
                "коммуникации": [{"адресат_роль": "мама", "лицо_адресата": "злой", "фраза_адресата": "Хватит лежать", "ответ_А": "Хорошо", "ответ_Б": "Не, не хочу", "лицо_после_Б": "шок"}],
                "участник": None,
                "вопросы": [],
            }, ensure_ascii=False)

        self._orig_ask = SB.llm._ask
        SB.llm._ask = fake_ask
        self.bot = FakeBot()

    def tearDown(self):
        SB.llm._ask = self._orig_ask

    def run_async(self, coro):
        return asyncio.run(coro)

    def press(self, data):
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1),
            callback_query=SimpleNamespace(
                data=data, answer=_anoop, edit_message_reply_markup=_anoop, message=FakeQueryMessage(self.bot)
            ),
        )
        ctx = SimpleNamespace(bot=self.bot)
        from telegram.ext import ApplicationHandlerStop
        with self.assertRaises(ApplicationHandlerStop):
            self.run_async(SB.callback(update, ctx))

    def reply(self, to_msg_id, text):
        msg = SimpleNamespace(
            text=text, caption=None, message_id=self.bot._id + 1000,
            reply_to_message=SimpleNamespace(message_id=to_msg_id), forward_origin=None,
            reply_text=FakeQueryMessage(self.bot).reply_text,
        )
        update = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=1))
        from telegram.ext import ApplicationHandlerStop
        with self.assertRaises(ApplicationHandlerStop):
            self.run_async(SB.admin_message(update, SimpleNamespace(bot=self.bot)))

    def test_day_flow_add_all_and_no_duplicates(self):
        self.run_async(SB.run_day(self.bot, self.DAY, None))
        texts = [m.text for m in self.bot.sent]
        day_list = texts[0]
        self.assertIn("Созвоны 26.11: 3", day_list)
        self.assertIn("✅ Глафира 08:00 — запись", day_list)
        self.assertIn("❓ Фекла (нет в «Участниках») 08:30 — ничего нет", day_list)
        self.assertTrue(any(t.startswith("Кто это: «Дуся Х»") for t in texts), "неоднозначное — вопрос с кнопками")
        q_irina = next(m for m in self.bot.sent if m.text.startswith("❓ Фекла"))
        self.assertIn("sec:nocall:", " ".join(buttons(q_irina)))
        summary = next(m for m in self.bot.sent if m.text.startswith("Собрал по части"))
        self.assertIn("Глафира — мама: А «Хорошо» / Б «Не, не хочу» ✅ на сайт", summary.text)
        self.assertEqual(len(self.llm_calls), 1)
        self.assertIn("[12:02:00]", self.llm_calls[0])
        self.assertNotIn("Фекла", self.llm_calls[0])  # в запрос по Глафире чужие созвоны не попали

        self.press(f"sec:all:{self.DAY.isoformat()}")
        cal = self.book.tabs[S.TAB_CALIBRATIONS].get_all_values()
        com = self.book.tabs[S.TAB_COMMUNICATIONS].get_all_values()
        self.assertEqual(len(cal), 3)  # заголовок, подсказка, новая строка в третьей
        self.assertEqual(cal[2][:4], ["26.11.2026", "@glafira_g", "уборка с мамой", "мама"])
        self.assertEqual(len(com), 4)
        row = dict(zip(S.COMMUNICATION_COLUMNS, com[3]))
        self.assertEqual(row["id"], "8")
        self.assertEqual(row["на_сайт"], "да")
        self.assertEqual(row["ник_tg"], "@glafira_g")
        journal = self.book.tabs[S.TAB_JOURNAL].get_all_values()
        self.assertEqual(journal[0], S.JOURNAL_HEADER)
        self.assertEqual(journal[1][:3], ["26.11.2026", "08:00", "@glafira_g"])
        self.assertEqual(journal[1][7], "добавлено")

        # повторный запуск того же дня: ничего не дописывается и Claude не зовётся
        self.run_async(SB.run_day(self.bot, self.DAY, None))
        self.assertEqual(len(self.llm_calls), 1)
        self.press(f"sec:all:{self.DAY.isoformat()}")
        self.assertEqual(len(self.book.tabs[S.TAB_CALIBRATIONS].get_all_values()), 3)
        self.assertEqual(len(self.book.tabs[S.TAB_COMMUNICATIONS].get_all_values()), 4)

        # журнал переживает потерю базы: после очистки sec_* слот Глафиры сразу «добавлено»
        with db.get_conn() as conn:
            for t in ("sec_days", "sec_slots", "sec_materials", "sec_drafts", "sec_questions"):
                conn.execute(f"DELETE FROM {t}")
        self.run_async(SB.run_day(self.bot, self.DAY, None))
        self.assertEqual(len(self.llm_calls), 1)

    def test_ready_block_reply_closes_participant_without_claude(self):
        self.run_async(SB.run_day(self.bot, self.DAY, None))
        calls_before = len(self.llm_calls)
        q_irina = next(m for m in self.bot.sent if m.text.startswith("❓ Фекла"))
        block = ("Строки для таблицы\nЛист «Калибровки»\n"
                 "26.11.2026\t\tтелефон бывшего\tмуж\tмолчу\t\tговорю «хватит»\t\tсозвон\tчётко\tнет\n")
        self.reply(q_irina.message_id, block)
        self.assertEqual(len(self.llm_calls), calls_before, "готовый блок разбирается без Claude")
        slot = next(s for s in sdb.slots_of_day(self.DAY.isoformat()) if s["name"] == "Фекла")
        drafts = sdb.drafts_of_slot(slot["id"])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(sdb.draft_row(drafts[0])["источник"], "созвон")
        qs = [q["text"] for q in sdb.questions_of_slot(slot["id"])]
        self.assertTrue(any("ник_tg пустая" in q for q in qs), "новый участник без ника — вопрос, не догадка")

    def test_no_call_button_and_question_answer(self):
        self.run_async(SB.run_day(self.bot, self.DAY, None))
        q_irina = next(m for m in self.bot.sent if m.text.startswith("❓ Фекла"))
        slot_id = int(buttons(q_irina)[0].split(":")[2])
        self.press(f"sec:nocall:{slot_id}")
        self.assertEqual(sdb.get_slot(slot_id)["status"], "no_call")
        journal = self.book.tabs[S.TAB_JOURNAL].get_all_values()
        self.assertEqual(journal[-1][7], "не было")

    def test_pick_ambiguous_then_material(self):
        self.run_async(SB.run_day(self.bot, self.DAY, None))
        ask = next(m for m in self.bot.sent if m.text.startswith("Кто это"))
        pick = next(b for b in buttons(ask) if b.endswith(":4"))  # строка 4 — @dusya_one
        self.press(pick)
        slot = next(s for s in sdb.slots_of_day(self.DAY.isoformat()) if s["name"] == "Дуся Х")
        self.assertEqual(slot["nick"], "@dusya_one")
        self.assertEqual(sdb.get_alias(S.norm("Дуся Х"))["nick"], "@dusya_one")


async def _anoop(*a, **k):
    return None


if __name__ == "__main__":
    unittest.main()


class TranscriberUploadTests(unittest.TestCase):
    def test_upload_chunks_and_skips_repeat(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("tt", Path(__file__).resolve().parent.parent / "mac" / "telemost_transcribe.py")
        tt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tt)
        tmp = Path(tempfile.mkdtemp()) / "a.txt"
        tmp.write_text("# файл: a.webm\n# начало\n\n[12:00:00] =привет\n[12:00:05] пока\n", encoding="utf-8")
        meta = {"file": "a.webm", "start": "2026-11-26T12:00:00", "duration_sec": 65}
        ws = FakeWS(S.TRANSCRIPTS_TAB, [S.TRANSCRIPTS_HEADER])
        self.assertEqual(tt.upload(tmp, meta, ws), 1)
        rows = ws.get_all_values()
        self.assertEqual(rows[1][:5], ["a.webm", "26.11.2026", "12:00:00", 65, 1])
        self.assertTrue(rows[1][5].startswith("[12:00:00] =привет"))
        self.assertEqual(tt.upload(tmp, meta, ws), 0)  # повторно не грузит
        [tr] = S.transcripts_from_values([[str(c) for c in r] for r in ws.get_all_values()])
        self.assertEqual(tr.start_th, datetime(2026, 11, 26, 12, 0))
