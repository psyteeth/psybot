"""Расшифровщик записей Яндекс Телемоста на Mac (ТЗ «Секретарь созвонов», раздел 3.1).

Аудио не покидает компьютер: ffmpeg → 16 кГц моно WAV → whisper-cli (whisper.cpp, модель
large-v3-turbo, язык ru) — всё локально. В облако уходит только текст расшифровки.

Куда уходит текст. У сервисного аккаунта бота нет своей квоты на Google Drive (проверено:
загрузка файла даёт 403 «Service Accounts do not have storage quota»), поэтому .txt-файлы в папку
он положить не может. Вместо этого текст дописывается строками в Google-таблицу
«Расшифровки созвонов», которую владелец один раз создаёт в папке «Созвоны — расшифровки»
(владелец таблицы — Антон, у сервисного аккаунта доступ «Редактор» через папку). Бот читает
расшифровки оттуда. Локальная копия — ~/Documents/Yandex.Telemost/расшифровки/<имя>.txt.

Отметки рядом с .txt:
  <имя>.txt   — расшифровано (повторно whisper не запускается);
  <имя>.done  — текст загружен в таблицу (повторно не загружается).

Запуск: launchd каждые 30 минут с 11:00 до 17:00 (mac/install_transcriber.sh), или вручную:
  ~/aider-test/psybot/.venv/bin/python ~/aider-test/psybot/mac/telemost_transcribe.py [--dry-run] [--all]
По умолчанию берутся только записи за последние TRANSCRIBE_MAX_AGE_DAYS (2) дня; архив — с --all.

Переменные окружения (берутся и из ~/aider-test/psybot/.env):
  GOOGLE_SERVICE_ACCOUNT_JSON — тот же ключ, что у бота;
  TRANSCRIPTS_SPREADSHEET_ID  — id таблицы расшифровок (по умолчанию — из bot/secretary.py);
  WHISPER_MODEL               — путь к ggml-large-v3-turbo.bin.
"""
import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from bot.secretary import (  # noqa: E402
    TRANSCRIPTS_HEADER,
    TRANSCRIPTS_SPREADSHEET_ID,
    TRANSCRIPTS_TAB,
    chunk_text,
    format_transcript_lines,
    parse_recording_name,
    parse_whisper_output,
)

WATCH_DIR = Path.home() / "Documents" / "Yandex.Telemost"
OUT_DIR = WATCH_DIR / "расшифровки"
DEFAULT_MODEL = Path.home() / ".local" / "share" / "whisper.cpp" / "ggml-large-v3-turbo.bin"
BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"]
STABLE_SECONDS = 120  # файл не менялся 2 минуты — запись закончена
MAX_AGE_DAYS = float(os.environ.get("TRANSCRIBE_MAX_AGE_DAYS", "2"))
LOCK_PATH = Path(tempfile.gettempdir()) / "telemost_transcribe.lock"

logger = logging.getLogger("telemost")


def _which(name: str) -> str:
    found = shutil.which(name, path=os.pathsep.join(BIN_DIRS + [os.environ.get("PATH", "")]))
    if not found:
        raise SystemExit(f"Не найден {name} (ищу в {', '.join(BIN_DIRS)})")
    return found


def _load_env() -> None:
    env_path = REPO_DIR / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        pass


def _label(path: Path) -> str:
    """Для лога — без имени файла: переименованные файлы могут содержать имя участника."""
    start, _ = parse_recording_name(path.name)
    return start.strftime("%d.%m %H:%M:%S") if start else "переименованный файл"


def pending_recordings(now: float | None = None, max_age_days: float | None = MAX_AGE_DAYS) -> list[Path]:
    """Новые записи: ещё без .done, не менялись 2 минуты и (по умолчанию) не старше MAX_AGE_DAYS —
    чтобы launchd не взялся за весь архив разом. Архив — вручную с --all."""
    now = now or time.time()
    out = []
    for p in sorted(WATCH_DIR.glob("*.webm"), key=lambda x: x.stat().st_mtime):
        if (OUT_DIR / (p.stem + ".done")).exists():
            continue
        age = now - p.stat().st_mtime
        if age < STABLE_SECONDS:
            continue  # ещё пишется
        if max_age_days is not None and age > max_age_days * 86400:
            continue
        out.append(p)
    return out


def recording_start(path: Path, duration_sec: float) -> dt.datetime:
    """Тайское время начала: из имени файла, а если файл переименован — mtime минус длительность
    (Телемост дописывает файл до конца звонка)."""
    start, _ = parse_recording_name(path.name)
    if start:
        return start
    end = dt.datetime.fromtimestamp(path.stat().st_mtime)  # Mac в тайском поясе (UTC+7)
    return (end - dt.timedelta(seconds=duration_sec)).replace(microsecond=0)


def transcribe(path: Path, model: Path, threads: int) -> tuple[Path, dict]:
    ffmpeg, whisper = _which("ffmpeg"), _which("whisper-cli")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(path),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
            check=True,
        )
        duration = max(0.0, (wav.stat().st_size - 44) / (16000 * 2))
        proc = subprocess.run(
            [whisper, "-m", str(model), "-l", "ru", "-t", str(threads), "-np", "-f", str(wav)],
            check=True, capture_output=True, text=True,
        )
    segments = parse_whisper_output(proc.stdout)
    start = recording_start(path, duration)
    meta = {
        "file": path.name,
        "start": start.isoformat(),
        "duration_sec": int(round(duration)),
    }
    lines = format_transcript_lines(segments, start)
    txt = OUT_DIR / (path.stem + ".txt")
    header = (
        f"# файл: {path.name}\n"
        f"# начало (Таиланд, UTC+7): {start:%d.%m.%Y %H:%M:%S}\n"
        f"# длительность: {meta['duration_sec'] // 60} мин {meta['duration_sec'] % 60} с\n\n"
    )
    txt.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    (OUT_DIR / (path.stem + ".meta.json")).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return txt, meta


def _open_transcripts_sheet():
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON не задан (нужен в ~/aider-test/psybot/.env)")
    creds = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.readonly"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.environ.get("TRANSCRIPTS_SPREADSHEET_ID") or TRANSCRIPTS_SPREADSHEET_ID)
    try:
        ws = sh.worksheet(TRANSCRIPTS_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TRANSCRIPTS_TAB, rows=200, cols=len(TRANSCRIPTS_HEADER))
        ws.update(range_name="A1", values=[TRANSCRIPTS_HEADER], value_input_option="RAW")
    return ws


def upload(txt: Path, meta: dict, ws) -> int:
    body = txt.read_text(encoding="utf-8").split("\n\n", 1)[-1]
    start = dt.datetime.fromisoformat(meta["start"])
    existing = {r[0] for r in ws.get_all_values()[1:] if r}
    if meta["file"] in existing:
        return 0  # уже загружали (например, упали до записи .done)
    rows = [
        [meta["file"], start.strftime("%d.%m.%Y"), start.strftime("%H:%M:%S"), meta["duration_sec"], i + 1, part]
        for i, part in enumerate(chunk_text(body))
    ]
    # RAW: текст расшифровки может начинаться с «=» или «-», формулы не нужны
    ws.append_rows(rows, value_input_option="RAW", table_range="A1")
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет расшифровано")
    ap.add_argument("--all", action="store_true", help=f"и записи старше {MAX_AGE_DAYS:g} дн. (архив; долго)")
    args = ap.parse_args()

    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    _load_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("Предыдущий запуск ещё работает — выхожу")
        return 0

    todo = pending_recordings(max_age_days=None if args.all else MAX_AGE_DAYS)
    if args.dry_run:
        for p in todo:
            state = "расшифровано, ждёт загрузки" if (OUT_DIR / (p.stem + ".txt")).exists() else "новое"
            print(f"{_label(p)} — {state}")
        return 0
    if not todo:
        return 0

    model = Path(os.environ.get("WHISPER_MODEL", str(DEFAULT_MODEL))).expanduser()
    threads = max(1, (os.cpu_count() or 4) - 2)
    ws = None
    errors = 0
    for p in todo:
        txt = OUT_DIR / (p.stem + ".txt")
        meta_path = OUT_DIR / (p.stem + ".meta.json")
        try:
            if txt.exists() and meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            else:
                if not model.exists():
                    logger.error(
                        "Нет модели whisper: %s. Скачать один раз (~1,6 ГБ): см. mac/README в psybot", model
                    )
                    return 2
                t0 = time.time()
                txt, meta = transcribe(p, model, threads)
                logger.info("%s: расшифровано за %d с", _label(p), time.time() - t0)
            if ws is None:
                ws = _open_transcripts_sheet()
            n = upload(txt, meta, ws)
            (OUT_DIR / (p.stem + ".done")).write_text(dt.datetime.now().isoformat(), encoding="utf-8")
            logger.info("%s: загружено в таблицу (%d строк)", _label(p), n)
        except Exception as e:  # noqa: BLE001
            errors += 1
            logger.error("%s: ошибка %s: %s", _label(p), type(e).__name__, str(e)[:300])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
