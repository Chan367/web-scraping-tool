"""Best-effort English translation helper used by scraper.py.

Uses Google's public translate endpoint through ``requests``.  Nothing here may
ever stop a crawl: every method returns an empty result on failure so the
caller keeps the original text.  Results are cached in a small JSON file.
"""
from __future__ import annotations
   
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

logger = logging.getLogger("sitecrawler.translator")

ENDPOINT = "https://translate.googleapis.com/translate_a/single"
CHUNK_CHARS = 3500          # keep every request comfortably small
MAX_CONSECUTIVE_FAILURES = 6  # then pause translating for a while
PAUSE_SECONDS = 120.0
LINE_SEP = "\n@@@\n"


class EnglishTranslator:
    def __init__(self, cache_path: Optional[Path] = None, timeout: float = 20.0) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.timeout = timeout
        self.available = requests is not None
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._dirty = 0
        self._failures = 0
        self._paused_until = 0.0
        self._load()

    # ------------------------------------------------------------ cache ---
    def _load(self) -> None:
        if not self.cache_path or not self.cache_path.is_file():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._cache = {str(k): str(v) for k, v in data.items()}
        except Exception:
            self._cache = {}

    def _save(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)
        except Exception:
            pass

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()

    # ---------------------------------------------------------- network ---
    def _request(self, text: str) -> str:
        if requests is None:
            return ""
        now = time.monotonic()
        with self._lock:
            if now < self._paused_until:
                return ""
        for attempt in range(3):
            try:
                resp = requests.post(
                    ENDPOINT,
                    params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t"},
                    data={"q": text},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    out = "".join(part[0] for part in (payload[0] or []) if part and part[0])
                    with self._lock:
                        self._failures = 0
                    return out
                if resp.status_code in (429, 503):
                    time.sleep(2 ** attempt)
                    continue
                break
            except Exception as exc:
                logger.debug("translate request failed: %s", exc)
                time.sleep(1 + attempt)
        with self._lock:
            self._failures += 1
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._paused_until = time.monotonic() + PAUSE_SECONDS
                self._failures = 0
                logger.warning("Translation service failing; pausing translation for %.0fs",
                               PAUSE_SECONDS)
        return ""

    @staticmethod
    def _split(text: str, limit: int) -> list[str]:
        parts, cur, size = [], [], 0
        for line in text.split("\n"):
            while len(line) > limit:           # very long single line
                if cur:
                    parts.append("\n".join(cur)); cur, size = [], 0
                parts.append(line[:limit]); line = line[limit:]
            if size + len(line) + 1 > limit and cur:
                parts.append("\n".join(cur)); cur, size = [], 0
            cur.append(line); size += len(line) + 1
        if cur:
            parts.append("\n".join(cur))
        return parts

    # -------------------------------------------------------- public API ---
    def translate(self, text: str) -> str:
        """Return the English text, or ``""`` when translation failed."""
        text = (text or "").strip()
        if not text or not self.available:
            return ""
        key = self._key(text)
        with self._lock:
            hit = self._cache.get(key)
        if hit:
            return hit
        pieces = []
        for chunk in self._split(text, CHUNK_CHARS):
            if not chunk.strip():
                pieces.append(chunk)
                continue
            out = self._request(chunk)
            if not out:
                return ""            # all-or-nothing: keep original on failure
            pieces.append(out)
        result = "\n".join(pieces).strip()
        if result:
            with self._lock:
                self._cache[key] = result
                self._dirty += 1
                if self._dirty >= 20:
                    self._dirty = 0
                    self._save()
        return result

    def translate_lines(self, lines: list[str]) -> list[str]:
        """Translate short labels; returns ``[]`` on failure (caller keeps originals)."""
        if not lines or not self.available:
            return []
        joined = LINE_SEP.join(l.replace("\n", " ") for l in lines)
        out = self.translate(joined)
        if not out:
            return []
        parts = [p.strip() for p in out.split("@@@")]
        return parts if len(parts) == len(lines) else []

    def flush(self) -> None:
        with self._lock:
            self._save()
