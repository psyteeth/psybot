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


async def test_11_dozhim_wife():
    """Живой кейс 07.10: честные ответы про жену не должны получать «мозги не еби» и закрытие."""
    SENT.clear()
    sid = new_rel_session(a_event="", other_person_label="жена", other_person_gender="f")
    ctx = fake_context({"rel_session_id": sid})
    r1 = await rel._check_dozhim(fake_update(), ctx, sid, "A", "жена меня как-то расстраивает")
    r2 = await rel._check_dozhim(fake_update(), ctx, sid, "A", "бесит то, что она не считается с моими чувствами")
    texts = " ".join(m["text"] for m in SENT)
    check(
        "11. «не считается с моими чувствами» после переспроса — принят, без «мозги не еби»",
        r1 == rel.A_EVENT and r2 is None and "мозги" not in texts, f"r1={r1}, r2={r2}",
    )


async def test_12_abcde_additions():
    """24.10, методичка ABCDE: стрела → C, тело → дискомфорт, % веры → D1, D6 → альтернатива → D7, карточка."""
    SENT.clear()
    sid = new_rel_session(other_person_label="жена", other_person_gender="f",
                          a_event="бесит то, что она не считается с моими чувствами")
    ctx = fake_context({"rel_session_id": sid})
    s1 = await rel._process_arrow_worst(fake_update(), ctx, "что мои чувства ничего не значат")
    s2 = await rel._process_arrow_self(fake_update(), ctx, "что я пустое место")
    s3 = await rel._process_c_body(fake_update(), ctx, "сжимаю челюсть")
    s4 = await rel._process_belief(fake_update(), ctx, "80", after=False)
    row = db.get_relationship_session(sid)
    await rel._ask_d(fake_update(), ctx, sid, "6")
    await rel._process_d_answer(fake_update(), ctx, "от отца, у него было так")
    step_after_6 = ctx.user_data["rel_d_step"]
    await rel._process_d_answer(fake_update(), ctx, "может, она просто устала и не замечает")
    step_after_alt = ctx.user_data["rel_d_step"]
    db.update_relationship_session(sid, d8_semantic="становится спокойнее", e_summary="скажу ей прямо, что мне важно")
    SENT.clear()
    await rel._finish_e(fake_update(), ctx, sid, "insight", quote="желание сказать ей прямо, что мне важно")
    final = SENT[-1]["text"] if SENT else ""
    ok = (
        s1 == rel.ARROW_SELF and s2 == rel.C_CONSEQUENCE and s3 == rel.C_DISCOMFORT and s4 == rel.D_QUESTION
        and row["arrow_self"] == "что я пустое место" and row["c_body"] == "сжимаю челюсть" and row["belief_before"] == 80
        and step_after_6 == "alt" and step_after_alt == "7a" and "Карточка на следующий раз" in final
    )
    check("12. Методичка ABCDE: стрела, тело, % веры, альтернатива, карточка", ok,
          f"states={s1},{s2},{s3},{s4}; после D6→{step_after_6}, после alt→{step_after_alt}; карточка={'да' if 'Карточка' in final else 'нет'}")


async def test_13_tooth_bridge():
    """26.10, решение автора: зуб после C ведёт в отношения — «где с людьми так же», переход в разбор с человеком."""
    from bot import limits

    async def _lim(*a, **k):
        return 99
    limits.get_limit = _lim
    SENT.clear()
    sid = new_rel_session(
        object_type="tooth", tooth_number=46, other_person="зуб", other_person_label="зуб 46", other_person_gender="m",
        a_event="воспаление гнойное на десне, сломан",
        b_narrative_confirmed="Правильно понимаю: зуб 46 должен был бы оставаться крепким, с укреплённой здоровой десной вокруг, и тогда всё было бы хорошо?",
        c_consequence="я в ахуе: говорю ему, эй ты че делаешь? Потом бесит воспаление, а потом боль и обида",
        c_feeling="блять ну почему это происходит со мной",
        arrow_worst="тогда я проиграл, я чмо позорное, закрываю глаза, чтобы не показать как мне",
        core_belief="я чмо позорное, слабак",
    )
    ctx = fake_context({"rel_session_id": sid})
    s1 = await rel._process_c_body(fake_update(), ctx, "глаза закрываются и звучит внутреннее «ну пиздец»")
    bridge_q = SENT[-1]["text"] if SENT else ""
    s2 = await rel._process_tooth_bridge(fake_update(), ctx, "с отцом так же, вечно подводит")
    s3 = await rel.tooth_bridge_text(types.SimpleNamespace(
        effective_user=USER, effective_message=types.SimpleNamespace(text="да", reply_text=FakeMessage().reply_text),
        callback_query=None, effective_chat=types.SimpleNamespace(id=USER.id),
    ), ctx)
    new_sid = ctx.user_data.get("rel_session_id")
    new_row = db.get_relationship_session(new_sid) if new_sid else None
    ok = (
        s1 == rel.TOOTH_BRIDGE and "чмо позорное" in bridge_q and s2 == rel.TOOTH_BRIDGE_CHOICE
        and s3 == rel.A_EVENT and new_row is not None and new_row["from_tooth_session"] == sid
        and (new_row["object_type"] or "") == "person"
    )
    check("13. Зуб → мост «где с людьми так же» → разбор с человеком", ok,
          f"states={s1},{s2},{s3}; мост={bridge_q[:120]!r}; новый={new_row['other_person_label'] if new_row else None}")

    SENT.clear()
    sid2 = new_rel_session(object_type="tooth", tooth_number=46, other_person_label="зуб 46", arrow_worst="я слабак")
    ctx2 = fake_context({"rel_session_id": sid2, "rel_bridge_q": rel.TOOTH_BRIDGE_FALLBACK_Q})
    s4 = await rel._process_tooth_bridge(fake_update(), ctx2, "не знаю, ни с кем вроде")
    final = SENT[-1]["text"] if SENT else ""
    check("14. Мост: «не знаю» → без дожима, финал с диагностикой", s4 == rel.ConversationHandler.END and "диагностик" in final,
          final[:100].replace("\n", " "))


async def test_15_vague_feeling_and_despair():
    """12.11: «Хреново» на C_feeling падало с TypeError (бот молчал); «хочется сдохнуть от
    безысходности» на E финал отразил как сдвиг."""
    SENT.clear()
    sid = new_rel_session()
    ctx = fake_context({"rel_session_id": sid})
    s1 = await rel._process_c_feeling(fake_update(), ctx, "Хреново")
    asked = SENT[-1]["text"] if SENT else ""
    check("15. «Хреново» на чувство → переспрос одним словом, без падения",
          s1 == rel.C_FEELING and asked == rel.VAGUE_FEELING_Q, f"state={s1}, бот={asked[:60]!r}")

    SENT.clear()
    sid = new_rel_session(d8_semantic="давление")
    ctx = fake_context({"rel_session_id": sid, "rel_e_question": "Как тебе теперь хочется на это реагировать?",
                        "exit_intent_flagged": True})
    await rel._process_e_summary(fake_update(), ctx, "никак, ничего не работает, хочется либо расстаться, либо сдохнуть уже от безысходности")
    final = [m for m in SENT if m["step"] == "E_finish"]
    text = final[-1]["text"] if final else ""
    check("16. Отчаяние на E → без «появилось», без карточки и «избегание/интеграция»",
          bool(final) and "появилось" not in text and "Карточка" not in text
          and not any(m["step"] == "exit_intent_check" for m in SENT), text[:100].replace("\n", " "))


async def test_17_d8_self_answer():
    """12.11: «Похоже, мне самой пора решать такое» на D8 — ответ, а не повод для напоминания."""
    SENT.clear()
    sid = new_rel_session()
    ctx = fake_context({"rel_session_id": sid})
    await rel._ask_d(fake_update(), ctx, sid, "8")
    SENT.clear()
    await rel._process_d_answer(fake_update(), ctx, "Похоже, мне самой пора решать такое")
    reminded = any("Слышу: хочется от себя другой реакции" in m["text"] for m in SENT)
    row = db.get_relationship_session(sid)
    # переспрос D8 проще допустим (ответ по контексту теста слабый) — проверяем только, что нет напоминания
    check("17. Ответ о себе на D8 → без напоминания «хочется от себя другой реакции»", not reminded,
          f"напоминание={'да' if reminded else 'нет'}, d8={row['d8_semantic']!r}, бот={[m['text'][:60] for m in SENT]}")


async def test_18_author_question_and_concept_prefill():
    """ТЗ-доп. №8: «Да, уточни» → вопрос автору; реплай автора → пользователю и в базу ответов;
    «Разобрать» из «Концепции» → A предзаполнен, «Так?», после «с кем» — сразу B."""
    from telegram.ext import ApplicationHandlerStop
    from bot import author_questions as aq
    from bot.author_answers import all_entries

    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))
            return types.SimpleNamespace(message_id=4242)

    ctx = types.SimpleNamespace(user_data={}, bot=Bot())
    aq.remember(ctx, "Почему в чате Вася так ответил Пете?", "человек: …")
    aq.AUTHOR_CHAT_ID = "1"
    query = types.SimpleNamespace(data="ask_author:yes", message=FakeMessage(), answer=_noop, edit_message_reply_markup=_noop)
    try:
        await aq.choice(types.SimpleNamespace(effective_user=USER, callback_query=query), ctx)
    except ApplicationHandlerStop:
        pass
    reply = FakeMessage()
    reply.text = "Спроси в чате сама."
    reply.reply_to_message = types.SimpleNamespace(message_id=4242)
    try:
        await aq.author_reply(types.SimpleNamespace(effective_message=reply), ctx)
    except ApplicationHandlerStop:
        pass
    ok_author = (
        sent[0][0] == "1" and "Вася" in sent[0][1]
        and sent[-1] == (USER.id, "Ответ автора:\n\nСпроси в чате сама.")
        and all_entries()[-1]["answer"] == "Спроси в чате сама."
    )
    check("18. «Да, уточни» → автору; реплай → пользователю с «Ответ автора:» и в базу", ok_author, repr(sent[-1]))

    from bot import limits

    async def _lim(*a, **k):
        return 99
    limits.get_limit = _lim
    SENT.clear()
    ctx = fake_context({"concept_rel_prefill": "в чате подкалывают друг друга и вешают ярлыки"})
    upd = types.SimpleNamespace(
        effective_user=USER, effective_message=FakeMessage(), effective_chat=types.SimpleNamespace(id=USER.id),
        callback_query=types.SimpleNamespace(data="concept_rel", answer=_noop, message=FakeMessage(),
                                             edit_message_reply_markup=_noop),
    )
    s1 = await rel.entry(upd, ctx)
    upd.callback_query.data = "rel_prefill:yes"
    s2 = await rel.prefill_confirm(upd, ctx)
    s3 = await rel._process_who_first(fake_update(), ctx, "участники чата")
    row = db.get_relationship_session(ctx.user_data["rel_session_id"])
    check("19. «Разобрать» из «Концепции» → «Так?» → с кем → сразу B",
          s1 == rel.PREFILL_CONFIRM and s2 == rel.WHO_FIRST and s3 == rel.B_NARRATIVE
          and row["a_event"] == "в чате подкалывают друг друга и вешают ярлыки", f"states={s1},{s2},{s3}")


def test_3_one_question():
    bad = [m for m in SENT_ALL if m["text"].count("?") > 1]
    check("3. Ни одно сообщение бота не содержит больше одного «?»", not bad,
          "; ".join(m["text"][:80] for m in bad))


SENT_ALL: list[dict] = []


async def main():
    tests = [
        test_1_e_shift, test_2_e_empty, test_4_one_at_a_time, test_5_answer_to_previous,
        test_6_b_no_additions, test_7_single_final, test_8_teeth_his, test_9_two_teeth, test_10_no_duplicates,
        test_11_dozhim_wife, test_12_abcde_additions, test_13_tooth_bridge,
        test_15_vague_feeling_and_despair, test_17_d8_self_answer,
        test_18_author_question_and_concept_prefill,
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
