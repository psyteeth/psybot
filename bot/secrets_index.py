"""«Секреты из таблицы» в ветке «Концепция» (мини-ТЗ от 29.09) — индекс по ВСЕМ листам
таблицы концепции (кроме «Концепция психостоматологии (нарратив)», у неё уже есть свой путь
через ConceptStore), с доступом по листам, статусами и локальным BM25-поиском.

Обновляется раз в SECRETS_INDEX_REFRESH_SECONDS и по команде /reload (только админ).
Сбой Sheets API не должен ронять бота — как и ConceptStore, ловим и логируем, работаем на
старом кэше.
"""
import asyncio
import hashlib
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from bot.config import (
    CONCEPT_SPREADSHEET_ID,
    SECRETS_ACCESS_DEFAULTS,
    SECRETS_EXCLUSION_MARKERS,
    SECRETS_FOR_BOT_TAB,
    SECRETS_INDEX_REFRESH_SECONDS,
    SECRETS_MAX_CHUNK_CHARS,
)
from bot.sheets import _build_client

logger = logging.getLogger(__name__)

STATUS_TAGS = ["Confirmed", "Contested", "Not found", "Theoretical bridge", "Speculative", "Certain", "Likely"]
_STATUS_RE = re.compile(r"\[(" + "|".join(re.escape(t) for t in STATUS_TAGS) + r")\]", re.IGNORECASE)

_HANDLE_RE = re.compile(r"@[A-Za-zА-Яа-яёЁ0-9_]{3,}")
_PHONE_RE = re.compile(r"(\+?\d[\d\-\s()]{8,}\d)")

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

FOR_BOT_ACCESS_HEADER = ["лист", "доступ"]
FOR_BOT_STOPWORDS_HEADER = "стоп-слова (имена/ники — заменяются на «один человек»)"
FOR_BOT_FAVORITES_HEADER = "любимые секреты автора (необязательно, свободная подсказка)"
FOR_BOT_PARAMS_HEADER = "параметры роутера (key=value, по одному на строку)"


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _split_paragraphs(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) <= 1:
        parts = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
        return parts
    out: list[str] = []
    buf = ""
    for p in parts:
        candidate = f"{buf}\n\n{p}" if buf else p
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                out.append(buf)
            buf = p if len(p) <= max_chars else p[:max_chars]
    if buf:
        out.append(buf)
    return out


@dataclass
class Chunk:
    chunk_id: str
    sheet: str
    row_header: str
    col_header: str
    text: str
    status: str | None
    access: str  # "основной" | "да" | "с оговоркой"


@dataclass
class ForBotConfig:
    access: dict[str, str] = field(default_factory=dict)
    stop_words: list[str] = field(default_factory=list)
    favorites_hint: str = ""
    params: dict[str, float] = field(default_factory=dict)


def _scrub_pii(text: str, stop_words: list[str]) -> str:
    text = _HANDLE_RE.sub("один человек", text)
    text = _PHONE_RE.sub("один человек", text)
    for name in stop_words:
        name = name.strip()
        if not name:
            continue
        text = re.sub(re.escape(name), "один человек", text, flags=re.IGNORECASE)
    return text


def _has_exclusion_marker(text: str) -> bool:
    upper = text.upper()
    return any(marker.upper() in upper for marker in SECRETS_EXCLUSION_MARKERS)


def _parse_status(text: str) -> str | None:
    m = _STATUS_RE.search(text)
    return m.group(1) if m else None


def _pick_header_row_idx(rows: list[list[str]], scan_limit: int = 10) -> int | None:
    best_idx, best_count = None, 1
    for i, row in enumerate(rows[:scan_limit]):
        non_empty = [c for c in row if c.strip()]
        if len(non_empty) > best_count:
            best_idx, best_count = i, len(non_empty)
    return best_idx


def _chunk_id(sheet: str, row_header: str, col_header: str, part_idx: int) -> str:
    raw = f"{sheet}|{row_header}|{col_header}|{part_idx}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class SecretsIndex:
    def __init__(self) -> None:
        self._client = _build_client()
        self._chunks: dict[str, Chunk] = {}
        self._by_sheet: dict[str, list[str]] = {}
        self._for_bot = ForBotConfig()
        self._bm25_idf: dict[str, float] = {}
        self._bm25_doc_freqs: dict[str, Counter] = {}
        self._bm25_doc_lens: dict[str, int] = {}
        self._bm25_avgdl = 0.0
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()

    # --- «Для бота»: доступ по листам, стоп-слова, любимые секреты ---

    def _ensure_for_bot_tab_sync(self, sh) -> ForBotConfig:
        try:
            ws = sh.worksheet(SECRETS_FOR_BOT_TAB)
        except Exception:  # noqa: BLE001
            ws = sh.add_worksheet(title=SECRETS_FOR_BOT_TAB, rows=100, cols=8)
            rows = [FOR_BOT_ACCESS_HEADER]
            for title, access in SECRETS_ACCESS_DEFAULTS.items():
                rows.append([title, access])
            ws.update(rows, "A1")
            ws.update_cell(1, 4, FOR_BOT_STOPWORDS_HEADER)
            ws.update_cell(1, 6, FOR_BOT_FAVORITES_HEADER)
            ws.update_cell(1, 8, FOR_BOT_PARAMS_HEADER)
            return ForBotConfig(access=dict(SECRETS_ACCESS_DEFAULTS), stop_words=[], favorites_hint="", params={})

        values = ws.get_all_values()
        access: dict[str, str] = {}
        for row in values[1:]:
            if len(row) < 2 or not row[0].strip():
                continue
            title = row[0].strip()
            level = row[1].strip().lower()
            if level in ("да", "нет", "с оговоркой"):
                access[title] = level
        # новый лист, которого нет в «Для бота» вообще — закрыт по умолчанию (ТЗ)
        for title in SECRETS_ACCESS_DEFAULTS:
            access.setdefault(title, SECRETS_ACCESS_DEFAULTS[title])

        stop_words = []
        for row in values[1:]:
            if len(row) > 3 and row[3].strip():
                stop_words.append(row[3].strip())

        favorites = []
        for row in values[1:]:
            if len(row) > 5 and row[5].strip():
                favorites.append(row[5].strip())

        params: dict[str, float] = {}
        for row in values[1:]:
            if len(row) <= 7 or not row[7].strip():
                continue
            raw_param = row[7].strip()
            if "=" not in raw_param:
                continue
            key, _, value = raw_param.partition("=")
            try:
                params[key.strip()] = float(value.strip())
            except ValueError:
                continue

        return ForBotConfig(
            access=access, stop_words=stop_words, favorites_hint="; ".join(favorites), params=params
        )

    # --- Чанкинг ---

    def _chunk_sheet(self, ws, for_bot: ForBotConfig) -> list[Chunk]:
        title = ws.title
        access = for_bot.access.get(title, "нет")
        if access == "нет":
            return []
        rows = ws.get_all_values()
        header_idx = _pick_header_row_idx(rows)
        col_headers = rows[header_idx] if header_idx is not None else []
        data_start = (header_idx + 1) if header_idx is not None else 0

        out: list[Chunk] = []
        for row in rows[data_start:]:
            if not any(c.strip() for c in row):
                continue
            row_header = row[0].strip() if row else ""
            multi_col = len(row) > 1 and any(c.strip() for c in row[1:])
            start_col = 1 if multi_col else 0
            for col_idx in range(start_col, len(row)):
                cell = row[col_idx].strip()
                if not cell or _has_exclusion_marker(cell):
                    continue
                col_header = col_headers[col_idx].strip() if col_idx < len(col_headers) else ""
                status = _parse_status(cell)
                clean_text = _STATUS_RE.sub("", cell).strip()
                clean_text = _scrub_pii(clean_text, for_bot.stop_words)
                for part_idx, part in enumerate(_split_paragraphs(clean_text, SECRETS_MAX_CHUNK_CHARS)):
                    if not part:
                        continue
                    out.append(
                        Chunk(
                            chunk_id=_chunk_id(title, row_header, col_header, part_idx),
                            sheet=title,
                            row_header=row_header if multi_col else "",
                            col_header=col_header,
                            text=part,
                            status=status,
                            access=access,
                        )
                    )
        return out

    def _refresh_sync(self) -> tuple[dict[str, Chunk], dict[str, list[str]], ForBotConfig] | None:
        if not self._client:
            return None
        sh = self._client.open_by_key(CONCEPT_SPREADSHEET_ID)
        for_bot = self._ensure_for_bot_tab_sync(sh)

        chunks: dict[str, Chunk] = {}
        by_sheet: dict[str, list[str]] = {}
        for ws in sh.worksheets():
            if ws.title == SECRETS_FOR_BOT_TAB:
                continue
            if "нарратив" in ws.title.lower():
                continue  # у неё уже есть отдельный путь через ConceptStore
            try:
                sheet_chunks = self._chunk_sheet(ws, for_bot)
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось разбить на чанки лист %s", ws.title)
                continue
            for c in sheet_chunks:
                chunks[c.chunk_id] = c
                by_sheet.setdefault(c.sheet, []).append(c.chunk_id)
        return chunks, by_sheet, for_bot

    def _build_bm25(self) -> None:
        doc_freqs: dict[str, Counter] = {}
        doc_lens: dict[str, int] = {}
        df: Counter = Counter()
        for chunk_id, chunk in self._chunks.items():
            tokens = _tokenize(f"{chunk.row_header} {chunk.col_header} {chunk.text}")
            doc_freqs[chunk_id] = Counter(tokens)
            doc_lens[chunk_id] = len(tokens)
            for term in set(tokens):
                df[term] += 1
        n_docs = len(self._chunks) or 1
        self._bm25_idf = {
            term: math.log(1 + (n_docs - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()
        }
        self._bm25_doc_freqs = doc_freqs
        self._bm25_doc_lens = doc_lens
        self._bm25_avgdl = (sum(doc_lens.values()) / n_docs) if doc_lens else 0.0

    async def ensure_fresh(self) -> None:
        stale = (time.time() - self._last_refresh) > SECRETS_INDEX_REFRESH_SECONDS
        if self._chunks and not stale:
            return
        async with self._lock:
            stale = (time.time() - self._last_refresh) > SECRETS_INDEX_REFRESH_SECONDS
            if self._chunks and not stale:
                return
            await self._do_refresh()

    async def reload(self) -> int:
        """/reload — принудительное обновление, возвращает число чанков в индексе."""
        async with self._lock:
            await self._do_refresh()
        return len(self._chunks)

    async def _do_refresh(self) -> None:
        try:
            result = await asyncio.to_thread(self._refresh_sync)
            if result:
                self._chunks, self._by_sheet, self._for_bot = result
                self._build_bm25()
                self._last_refresh = time.time()
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось обновить индекс секретов, используем старый кэш")

    # --- Поиск ---

    def _bm25_score(self, query_tokens: list[str], chunk_id: str) -> float:
        freqs = self._bm25_doc_freqs.get(chunk_id)
        if not freqs:
            return 0.0
        dl = self._bm25_doc_lens.get(chunk_id, 0) or 1
        k1, b = 1.5, 0.75
        score = 0.0
        for term in query_tokens:
            f = freqs.get(term, 0)
            if not f:
                continue
            idf = self._bm25_idf.get(term, 0.0)
            denom = f + k1 * (1 - b + b * dl / (self._bm25_avgdl or 1))
            score += idf * (f * (k1 + 1)) / (denom or 1)
        return score

    def search_scored(
        self, query: str, top_k: int = 5, exclude_ids: set[str] | None = None
    ) -> list[tuple[Chunk, float]]:
        exclude_ids = exclude_ids or set()
        query_tokens = _tokenize(query)
        if not query_tokens or not self._chunks:
            return []
        scored = [
            (cid, self._bm25_score(query_tokens, cid))
            for cid in self._chunks
            if cid not in exclude_ids
        ]
        scored.sort(key=lambda x: -x[1])
        return [(self._chunks[cid], score) for cid, score in scored[:top_k] if score > 0]

    def search(self, query: str, top_k: int = 5, exclude_ids: set[str] | None = None) -> list[Chunk]:
        return [c for c, _score in self.search_scored(query, top_k, exclude_ids)]

    def get(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    def param(self, key: str, default: float) -> float:
        return self._for_bot.params.get(key, default)

    def neighbors(self, chunk_id: str, limit: int = 3) -> list[Chunk]:
        chunk = self._chunks.get(chunk_id)
        if not chunk:
            return []
        sibling_ids = [cid for cid in self._by_sheet.get(chunk.sheet, []) if cid != chunk_id]
        return [self._chunks[cid] for cid in sibling_ids[:limit]]

    def is_loaded(self) -> bool:
        return bool(self._chunks)

    def chunk_count(self) -> int:
        return len(self._chunks)


secrets_index = SecretsIndex()
