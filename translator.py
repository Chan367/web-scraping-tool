"""Best-effort English translation for scraped non-English pages.

The crawler uses this to turn Hindi/Tamil/... page text into English when a
site has no English version.  Translation is optional: if the request fails,
the caller keeps the original text, so a page is never lost.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

try:  # requests is already a scraper dependency
    import requests
except Exception:  # pragma: no cover - requests is normally available
    requests = None  # type: ignore[assignment]

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MAX_CHUNK = 4000
MAX_CONSECUTIVE_FAILURES = 6


def split_for_translation(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split text into newline-preserving chunks no longer than *limit*."""
    text = text or ""
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for block in text.split("\n"):
        while len(block) > limit:
            cut = block.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            piece, block = block[:cut], block[cut:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        if not block:
            if current:
                current += "\n"
            continue
        if current and len(current) + 1 + len(block) > limit:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks or [text]


class EnglishTranslator:
    """Thread-safe, cached translator with a hard fallback to the source text."""

    def __init__(self, cache_path: Path | None = None, timeout: float = 20.0,
                 min_interval: float = 0.2) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.timeout = max(5.0, float(timeout or 20.0))
        self.min_interval = max(0.0, float(min_interval or 0.0))
        self._lock = threading.RLock()
        self._last_call = 0.0
        self._cache: dict[str, str] = {}
        self._consecutive_failures = 0
        self.disabled = False
        self._session = requests.Session() if requests is not None else None
        if self._session is not None:
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            })
        self._load_cache()

    @property
    def available(self) -> bool:
        return self._session is not None and not self.disabled

    # -- cache -------------------------------------------------------------
    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.is_file():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, dict):
            self._cache = {
                str(key): str(value) for key, value in data.items()
                if isinstance(value, str)
            }

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass

    # -- public API --------------------------------------------------------
    def translate(self, text: str) -> str | None:
        """Return *text* in English, or ``None`` when translation failed."""
        text = text or ""
        if not text.strip():
            return ""
        if not self.available:
            return None
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached

        pieces: list[str] = []
        for chunk in split_for_translation(text):
            translated = self._translate_chunk(chunk)
            if translated is None:
                return None
            pieces.append(translated)
        result = "\n".join(pieces)
        with self._lock:
            self._cache[key] = result
            self._save_cache()
        return result

    def translate_lines(self, lines: list[str]) -> list[str] | None:
        """Translate many short labels (menus) in one request."""
        if not lines:
            return []
        if not any(line.strip() for line in lines):
            return list(lines)
        joined = "\n".join(line or "" for line in lines)
        translated = self.translate(joined)
        if translated is None:
            return None
        out = translated.split("\n")
        # Google can merge/split lines; only trust an exact line count.
        if len(out) != len(lines):
            return None
        return out

    # -- one request -------------------------------------------------------
    def _translate_chunk(self, chunk: str) -> str | None:
        if not chunk.strip():
            return chunk
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                response = self._session.get(  # type: ignore[union-attr]
                    GOOGLE_URL,
                    params={
                        "client": "gtx",
                        "sl": "auto",
                        "tl": "en",
                        "dt": "t",
                        "q": chunk,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                segments = data[0] or []
                translated = "".join(
                    segment[0] for segment in segments
                    if segment and segment[0]
                )
                if not translated.strip():
                    raise ValueError("empty translation")
                self._consecutive_failures = 0
                return translated
            except Exception:
                self._consecutive_failures += 1
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.disabled = True
                return None
