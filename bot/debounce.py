"""Объединение подряд идущих сообщений пользователя в одно перед обработкой шага.

Если пользователь шлёт два-три сообщения подряд, не дожидаясь ответа бота, мы не
хотим обрабатывать их как отдельные (второе может «потеряться», упав мимо
текущего состояния разговора). Вместо этого каждое сообщение шага копится в
буфере; если в течение DEBOUNCE_SECONDS не пришло новое — буфер склеивается в
один текст и обрабатывается как единый ответ.

Механика: обработчик шага сам ничего не делает с сообщением — только буферизует
и возвращает ТЕКУЩЕЕ состояние (разговор остаётся на месте). Настоящая обработка
(процесс шага, следующий вопрос, переход состояния) происходит в отложенной
джобе через JobQueue, которая по завершении сама выставляет новое состояние в
ConversationHandler.

Буфер и джоба живут в модульных словарях, а НЕ в context.user_data: user_data
персистентен на диск (PicklePersistence, см. bot/main.py) и должен оставаться
picklable — job-объект JobQueue таким не является. Плата за это — сообщение,
попавшее ровно в момент рестарта процесса (окно DEBOUNCE_SECONDS), потеряется;
это гораздо дешевле, чем терять picklability всего user_data."""
import logging

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import DEBOUNCE_SECONDS

logger = logging.getLogger(__name__)

_buffers: dict[tuple[int, str], list[str]] = {}
_jobs: dict[tuple[int, str], object] = {}


async def collect(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    step_id: str,
    conv_handler: ConversationHandler,
    state,
    process,
) -> object:
    """Буферизует текст сообщения и (пере)планирует отложенную обработку.

    `process(combined_text: str) -> new_state` — корутина с полной логикой шага
    (всё, что раньше было телом обработчика), принимает склеенный текст и
    возвращает новое состояние ConversationHandler.

    Возвращает `state` — обработчик, вызвавший `collect`, должен вернуть это же
    значение как есть, ничего больше не делая.
    """
    user = update.effective_user
    key = (user.id, step_id)

    text = update.effective_message.text or ""
    _buffers.setdefault(key, []).append(text)

    old_job = _jobs.pop(key, None)
    if old_job is not None:
        old_job.schedule_removal()

    chat = update.effective_chat
    conv_key = (chat.id, user.id)

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    except Exception:  # noqa: BLE001
        pass

    async def _fire(job_context: ContextTypes.DEFAULT_TYPE) -> None:
        combined = "\n".join(_buffers.pop(key, []))
        _jobs.pop(key, None)
        try:
            new_state = await process(combined)
        except Exception:  # noqa: BLE001
            logger.exception("debounce: обработка отложенного шага упала (step_id=%s)", step_id)
            return
        conv_handler._update_state(new_state, conv_key)  # noqa: SLF001

    job = context.application.job_queue.run_once(_fire, DEBOUNCE_SECONDS)
    _jobs[key] = job
    return state
