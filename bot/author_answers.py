"""База примеров «вопрос → ответ автора» для ветки «Концепция» (bot/data/author_answers.json).
Обновляется через новый экспорт xlsx от автора, коммитом в репозиторий — не через Google Sheets,
т.к. в файле структурные колонки (тон, указание боту), а не свободный текст."""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parent / "data" / "author_answers.json"


def _load() -> list[dict]:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось загрузить базу ответов автора (%s)", _PATH)
        return []


ENTRIES: list[dict] = _load()


def all_entries() -> list[dict]:
    """База из файла + ответы автора на вопросы пользователей (ТЗ-доп. №8): ответил раз — в
    следующий раз бот знает сам. num для вторых — 10000 + id вопроса, чтобы не пересечься."""
    from bot import db

    try:
        extra = [
            {"num": 10000 + r["id"], "category": "из вопросов пользователей", "question": r["question"],
             "answer": r["answer"], "tone": "серьёзно", "instruction": ""}
            for r in db.answered_author_questions()
        ]
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось прочитать ответы автора из БД")
        extra = []
    return ENTRIES + extra
