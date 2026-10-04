"""Прогон тестов из ТЗ-доп. №7 (реальные реплики из логов 04.10) на живом LLM.

Запуск из корня psybot:  .venv/bin/python -m tests.test_tz7
Обработчики вызываются напрямую (мимо debounce/Telegram): временная SQLite, запись в Sheets
отключена, все исходящие сообщения перехватываются. Нужен ANTHROPIC_API_KEY в .env."""
import asyncio
import os
import sys
import tempfile
import types

from dotenv import load_dotenv

load_dotenv(".env")

from bot import config, db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
db.init_db()

from bot import analytics, debounce, llm  # noqa: E402
from bot.handlers import relationships as rel  # noqa: E402
from bot.handlers import teeth  # noqa: E402
from bot.sheets import sheets_logger  # noqa: E402


async def _noop(*args, **kwargs):
    return None


sheets_logger.append = _noop
analytics.log = _noop
rel.dialogue_matrix.build_relationship_matrix_column = _noop
teeth.dialogue_matrix.build_teeth_matrix_column = _noop

SENT: list[dict] = []


def _capture(module):
    async def fake_send(update, context, session_id, step, text, msg_type="обычный", **kwargs):
        SENT.append({"step": step, "text": text, "kwargs": kwargs})
    module._send = fake_send


_capture(rel)
_capture(teeth)

USER = types.SimpleNamespace(id=999000111, username="tz7_test")


class FakeMessage:
    async def reply_text(self, text, **kwargs):
        SENT.append({"step": "reply_text", "text": text, "kwargs": kwargs})


def fake_update():
    return types.SimpleNamespace(
        effective_user=USER, effective_message=FakeMessage(), callback_query=None,
        effective_chat=types.SimpleNamespace(id=USER.id),
    )


def fake_context(user_data=None):
    bot = types.SimpleNamespace(send_message=_noop, send_chat_action=_noop)
    return types.SimpleNamespace(user_data=user_data if user_data is not None else {}, bot=bot)


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, details: str = "") -> None:
    RESULTS.append((name, ok, details))
    print(("✅" if ok else "❌"), name, ("— " + details if details else ""))


def new_rel_session(**fields) -> int:
    session_id = db.create_relationship_session(USER.id, USER.username)
    base = dict(
        a_event="муж дома мало времени, выбирает работу",
        b_narrative_confirmed="Правильно понимаю: он должен был бы больше бывать дома, и тогда всё было бы хорошо?",
        c_consequence="злюсь\nЧувство: одиноко",
        other_person="муж", other_person_label="муж", other_person_gender="m",
    )
    base.update(fields)
    db.update_relationship_session(session_id, **base)
    return session_id


async def test_1_e_shift():
    SENT.clear()
    sid = new_rel_session()
    ctx = fake_context({"rel_session_id": sid, "rel_e_question": "Как тебе теперь хочется на это реагировать?"})
    await rel._process_e_summary(fake_update(), ctx, "хочется просто пойти и делать своё, для себя")
    final = [m for m in SENT if m["step"] == "E_finish"]
    text = final[-1]["text"] if final else ""
    check(
        "1. E «хочется просто пойти и делать своё» → отражение, не «ответ не находится»",
        bool(final) and "не находится" not in text and "появилось" in text, text[:140].replace("\n", " "),
    )


async def test_2_e_empty():
    SENT.clear()
    sid = new_rel_session()
    ctx = fake_context({"rel_session_id": sid, "rel_e_question": "Как тебе теперь хочется на это реагировать?"})
    state = await rel._process_e_summary(fake_update(), ctx, "не знаю")
    if state == rel.E_FOLLOWUP:
        await rel._process_e_followup(fake_update(), ctx, "не знаю")
    final = [m for m in SENT if m["step"] == "E_finish"]
    check(
        "2. E «не знаю» → допустим «ответ не находится»",
        bool(final) and "не находится" in final[-1]["text"], final[-1]["text"][:100] if final else "нет финала",
    )


async def test_4_one_at_a_time():
    SENT.clear()
    sid = new_rel_session()
    ctx = fake_context({"rel_session_id": sid})
    await rel._ask_d(fake_update(), ctx, sid, "3")
    await rel._process_d_answer(fake_update(), ctx, "отдаляюсь и навешиваю ему")
    await rel._process_d_answer(fake_update(), ctx, "не помогает")
    SENT.clear()
    state = await rel._process_d_answer(fake_update(), ctx, "Можешь задавать по одному вопросу?")
    ack = SENT[-1]["text"] if SENT else ""
    ok_ack = state == rel.D_QUESTION and ack.startswith("Понял, по одному") and ctx.user_data["rel_d_step"] == "4"
    await rel._process_d_answer(fake_update(), ctx, "что это он виноват в моей усталости")
    step_after_4a = ctx.user_data["rel_d_step"]
    state = await rel._process_d_answer(fake_update(), ctx, "я меньше его люблю и отдаляюсь")
    check(
        "4. «Можешь задавать по одному?» на D4 → согласие, D4 заново, потом D4b, только потом D5",
        ok_ack and step_after_4a == "4b" and state == rel.D5_QUESTION,
        f"ack={ack[:60]!r}, после D4a шаг={step_after_4a}, после D4b state={state}",
    )


async def test_5_answer_to_previous():
    SENT.clear()
    sid = new_rel_session(d7_friend="все как бы сходит с рук ему, он работал")
    ctx = fake_context({"rel_session_id": sid})
    await rel._ask_d(fake_update(), ctx, sid, "7b")
    await rel._ask_d(fake_update(), ctx, sid, "8")
    SENT.clear()
    state = await rel._process_d_answer(
        fake_update(), ctx, "я посоветую поговорить и попробовать что-то поменять, потому что в таком жить трудно"
    )
    row = db.get_relationship_session(sid)
    check(
        "5. Ответ на D8 по смыслу про D7 → записан в D7, D8 задан заново",
        state == rel.D_QUESTION and "посоветую" in (row["d7_friend"] or "") and not row["d8_semantic"]
        and SENT and "прошлому вопросу" in SENT[-1]["text"],
        f"d7_friend={row['d7_friend']!r}, d8={row['d8_semantic']!r}",
    )


async def test_6_b_no_additions():
    text = await llm.reformulate_narrative(
        "Муж быстро вспыхивает и вечно раздражается непонятно почему", "Должен подстраиваться под меня",
        other=llm.other_person_note("муж", "m"),
    )
    check("6. Сверка B без «сдерживать раздражение»", "сдерж" not in text.lower(), text)


async def test_7_single_final():
    SENT.clear()
    sid = new_rel_session()
    ctx = fake_context({
        "rel_session_id": sid, "exit_intent_flagged": True,
        "rel_e_question": "Как тебе теперь хочется на это реагировать?",
    })
    state = await rel._process_e_summary(fake_update(), ctx, "хочется просто пойти и делать своё, для себя")
    steps_before = [m["step"] for m in SENT]
    await rel._process_exit_intent_clarify(fake_update(), ctx, "2")
    steps = [m["step"] for m in SENT]
    finals = [m for m in SENT if m["step"] == "E_finish"]
    ok = (
        state == rel.EXIT_INTENT_CLARIFY and "E_finish" not in steps_before and steps.count("E_finish") == 1
        and "cta_offer" not in steps and "Мария сориентирует" in finals[0]["text"]
        and "запрещаешь" in finals[0]["text"]
    )
    check("7. E → «избегание/интеграция» → одно финальное сообщение, без cta_offer", ok, " → ".join(steps))


async def test_8_teeth_his():
    SENT.clear()
    session_id = db.create_teeth_session(USER.id, USER.username)
    ctx = fake_context({"teeth_session_id": session_id})
    state = await teeth._process_ask_scary(fake_update(), ctx, "Его не станет")
    steps = [m["step"] for m in SENT]
    check(
        "8. Зубы «Его не станет» → без проверки «за другого», сразу «Каким ты тогда себя чувствуешь?»",
        state == teeth.ASK_FEELING and steps == ["ask_feeling"], " → ".join(steps),
    )


async def test_9_two_teeth():
    SENT.clear()
    session_id = db.create_teeth_session(USER.id, USER.username)
    ctx = fake_context({"teeth_session_id": session_id})
    state = await teeth._process_ask_tooth(fake_update(), ctx, "33, 34")
    markup = SENT[-1]["kwargs"].get("reply_markup") if SENT else None
    labels = [b.text for row in markup.inline_keyboard for b in row] if markup else []
    check("9. Зубы «33, 34» → просьба выбрать один, кнопки 33 и 34", state == teeth.ASK_TOOTH and labels == ["33", "34"],
          f"кнопки={labels}")


async def test_10_no_duplicates():
    debounce._inflight.add(USER.id)
    upd = types.SimpleNamespace(effective_user=USER, effective_message=types.SimpleNamespace(text="Стараюсь"))
    returned = await debounce.collect(
        upd, fake_context(), step_id="rel_d", conv_handler=None, state="STATE", process=None,
    )
    held = debounce._late_updates.pop(USER.id, [])
    debounce._inflight.discard(USER.id)
    check(
        "10. Сообщение во время обработки шага не запускает шаг второй раз (откладывается до смены состояния)",
        returned == "STATE" and held == [upd] and not debounce._buffers.get((USER.id, "rel_d")),
    )


def test_3_one_question():
    bad = [m for m in SENT_ALL if m["text"].count("?") > 1]
    check("3. Ни одно сообщение бота не содержит больше одного «?»", not bad,
          "; ".join(m["text"][:80] for m in bad))


SENT_ALL: list[dict] = []


async def main():
    tests = [
        test_1_e_shift, test_2_e_empty, test_4_one_at_a_time, test_5_answer_to_previous,
        test_6_b_no_additions, test_7_single_final, test_8_teeth_his, test_9_two_teeth, test_10_no_duplicates,
    ]
    for t in tests:
        try:
            await t()
        except Exception as e:  # noqa: BLE001
            check(t.__name__, False, f"исключение: {e!r}")
        SENT_ALL.extend(SENT)
    test_3_one_question()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} пройдено")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
