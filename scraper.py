#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
site_scraper.py -- universal, category-aware website crawler.

Crawls every internal page of a website (BFS), extracts text + media, downloads
images / PDFs / audio / video / documents into per-category, per-page folders,
and writes one UTF-8 JSONL record per page.

Quick start (Windows):
    python scraper.py --url https://www.india.gov.in/ --max-pages 50
    python scraper.py --url https://www.pib.gov.in/
    python scraper.py --url https://example.com/ --root-dir "D:\\scrape" --delay 2

Output layout:
    <root-dir>\\<site_name>\\
        scraper\\scraper.py, config.json, requirements.txt
        categories\\<category>\\pages.jsonl
        categories\\<category>\\media\\{images,pdfs,audio,video,documents}\\<page_id>\\
        data\\state.json, category_index.json
        data\\scraped_index.json      <- URLs already scraped (skip list)
        data\\scraped_pages.xlsx      <- cumulative Excel report of every page
        data\\new_pages.xlsx          <- ONLY the pages discovered in the last run
        logs\\scraper.log, failed_urls.txt

Incremental scraping (default): every scraped URL is recorded in
data\\scraped_index.json + data\\scraped_pages.xlsx.  Running the same URL again
skips all recorded pages (no re-download, no re-parse), replays the links found
on them to discover new pages, and scrapes only the new ones.  Use --restart to
force a full re-crawl, or --no-incremental for the old behaviour.

Following everything:
    python scraper.py --url https://www.india.gov.in/ --follow-external
        Also follows hyperlinks to other websites (up to --max-depth hops and
        --external-max-pages off-site pages), downloads the PDFs/documents
        linked from them, and stores off-site pages under "External Sites".
        Add --follow-service-links (implied by --follow-external) to crawl the
        ministry/state portals stored in harvested india.gov.in service records.

English content:
    By default the crawler switches to the English version when a site offers
    one.  Pages that are still Hindi/Tamil/... are translated to English with
    Google's public translate endpoint; if translation fails the page is saved
    in its original language, so nothing is skipped.  Use --no-translate to
    keep every page in its original language.

Internal first (on-site before off-site):
    python scraper.py --url https://www.india.gov.in/ --internal-first
        Keeps the external queue but serves every on-site page first; off-site
        portals only start after the internal crawl is exhausted.
        --include-service-pages also fetches the /services/details/... pages
        that --india-services normally skips, storing them under
        "Service Pages" (same URLs as the API rows, so they need their own
        category instead of colliding with them).

Refreshing changed pages:
    python scraper.py --url https://www.india.gov.in/ --refresh-existing
        Re-fetches pages that are already stored, compares a per-field content
        fingerprint and rewrites only the records whose content really changed.
        Unchanged pages cost one request and no disk write.
        --refresh-after DAYS   only pages not checked in the last DAYS days
        --refresh-limit N      stop after refreshing N pages this run
        --refresh-media        also re-download PDFs/documents/audio/video and
                               replace a file only when its bytes differ
        data\\refresh_index.json    hashes + last-checked times
        data\\refresh_changes.jsonl what changed, one line per changed page

Requires Python 3.10+.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cfutures
import hashlib
import html as html_lib
import importlib.util
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter, deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import (
    parse_qsl,
    unquote,
    unquote_plus,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from tqdm import tqdm

import navigation
from crawl_modes import Depth, select_mode
from crawl_queue import CrawlQueue
from translator import EnglishTranslator

try:  # optional: trust the same certificate authorities as the operating system
    import truststore
except Exception:  # pragma: no cover - optional dependency
    truststore = None

try:  # optional, used for PDF text extraction
    import pdfplumber
except Exception:  # pragma: no cover
    pdfplumber = None

try:  # optional, fallback PDF text extraction (PyMuPDF)
    import pymupdf as fitz
except Exception:  # pragma: no cover
    try:
        import fitz  # older PyMuPDF releases
    except Exception:
        fitz = None

try:  # optional, only used with --download-youtube
    import yt_dlp
except Exception:  # pragma: no cover
    yt_dlp = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.4.0"
APP_NAME = "SiteCrawler"
DEFAULT_ROOT_DIR = r"C:\Users\chan8\OneDrive\Desktop\web_scraping"
# A current desktop-browser UA is used because many government sites put
# bot-like user agents behind a WAF. Change it with --user-agent if needed.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_PATH_LEN = 250          # stay safely below Windows' 260-char limit
MAX_TEXT_CHARS = 1_500_000
MAX_IMAGES_PER_PAGE = 1_000
STATE_SAVE_EVERY = 10
PDF_TEXT_LIMIT = 2_000_000

IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".tif", ".tiff", ".avif", ".jfif", ".apng",
}
PDF_EXT = {".pdf"}
AUDIO_EXT = {
    ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus",
    ".wma", ".mid", ".midi", ".aiff",
}
VIDEO_EXT = {
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".ogv", ".3gp", ".m3u8", ".ts",
}
DOC_EXT = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    ".odp", ".rtf", ".txt", ".csv", ".tsv", ".epub", ".zip", ".rar",
    ".7z", ".tar", ".gz", ".json", ".xml", ".jsonl", ".md",
}
NON_HTML_EXT = IMAGE_EXT | PDF_EXT | AUDIO_EXT | VIDEO_EXT | DOC_EXT | {
    ".exe", ".msi", ".apk", ".dmg", ".iso", ".bin", ".dat", ".woff",
    ".woff2", ".ttf", ".eot", ".css", ".js", ".map",
}

KIND_DIRS = {
    "image": "images",
    "pdf": "pdfs",
    "audio": "audio",
    "video": "video",
    "document": "documents",
}
KIND_DEFAULT_EXT = {
    "image": ".jpg",
    "pdf": ".pdf",
    "audio": ".mp3",
    "video": ".mp4",
    "document": ".bin",
}
KIND_COUNT_KEYS = {
    "image": "images",
    "pdf": "pdfs",
    "audio": "audio",
    "video": "video",
    "document": "documents",
}

DROP_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "_ga", "_gl",
    "igshid", "yclid", "srsltid", "gbraid", "wbraid",
}
DROP_QUERY_PREFIXES = ("utm_",)
TRACKING_PATH_HINTS = ("/logout", "/signout", "/wp-login.php", "/wp-admin/")

# Language-switch endpoints, not real content pages.  When English is
# preferred these are skipped so a link such as /lang/hi cannot flip the
# site (and its shared session cookie) back to Hindi mid-crawl.
#
# A non-English version can live in many places:
#   /hi/...  /hindi/...  /census.website/hi/...  /lang/hi  ?lang=hi
# Short codes that are also ordinary English words (/or/, /as/, /it/,
# /in/, /no/, /is/) are deliberately not treated as languages on their
# own; they are only trusted after a /lang/ container.
LANG_PATH_SEGMENTS = {
    "hi", "ta", "te", "bn", "mr", "gu", "kn", "ml", "pa", "ur",
    "sa", "ne", "si", "fr", "de", "es", "ar", "zh", "ja", "ru",
    "pt", "nl", "ko", "tr",
    "hindi", "tamil", "telugu", "bengali", "marathi", "gujarati",
    "kannada", "malayalam", "punjabi", "urdu", "sanskrit", "nepali",
    "sinhala", "odia", "oriya", "assamese",
    "french", "german", "spanish", "arabic", "chinese", "japanese",
    "russian", "portuguese", "italian", "dutch", "korean", "turkish",
}
LANG_CONTAINER_SEGMENTS = {"lang", "language", "locale", "lng", "langcode"}
ENGLISH_LANG_VALUES = {
    "en", "eng", "english", "en-us", "en-gb", "en-in",
    "en_us", "en_gb", "en_in",
}
LANG_QUERY_RE = re.compile(
    r"(?:^|&)(?:lang|language|locale|hl|setlang|lng|lang_code|language_code|"
    r"langid|languageid)=([^&#]+)",
    re.I,
)
# Script ranges used to decide whether a page is actually English.  Language
# menus often list every script, so the ratio matters more than the raw count.
INDIC_SCRIPT_RE = re.compile(
    "[\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF"
    "\u0B00-\u0B7F\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF"
    "\u0D00-\u0D7F\u0D80-\u0DFF]"
)
NON_LATIN_SCRIPT_RE = re.compile(
    "[\u0370-\u03FF\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF"
    "\u0E00-\u0E7F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF"
    "\uAC00-\uD7AF\u1100-\u11FF]"
)
LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
# A language switcher lists several scripts on one line.  It is boilerplate,
# not page content, so it must not make an English page look non-English.
LANGUAGE_NAME_RE = re.compile(
    r"\b(?:english|hindi|tamil|telugu|bengali|marathi|gujarati|kannada|"
    r"malayalam|punjabi|urdu|sanskrit|nepali|sinhala|odia|oriya|assamese|"
    r"french|german|spanish|arabic|chinese|japanese|russian|portuguese|"
    r"italian|dutch|korean|turkish)\b",
    re.I,
)
INDIC_SCRIPT_RANGES = (
    "\u0900-\u097F", "\u0980-\u09FF", "\u0A00-\u0A7F", "\u0A80-\u0AFF",
    "\u0B00-\u0B7F", "\u0B80-\u0BFF", "\u0C00-\u0C7F", "\u0C80-\u0CFF",
    "\u0D00-\u0D7F", "\u0D80-\u0DFF",
)


def _script_counts(text: str) -> tuple[int, int, int]:
    return (
        len(INDIC_SCRIPT_RE.findall(text)),
        len(NON_LATIN_SCRIPT_RE.findall(text)),
        len(LATIN_LETTER_RE.findall(text)),
    )


def _looks_like_language_menu(block: str) -> bool:
    """True for a line such as ``Hindi (हिंदी) Tamil (தமிழ்)``."""
    names = {name.lower() for name in LANGUAGE_NAME_RE.findall(block)}
    if not names:
        return False
    # Three or more language names on one line is a switcher, not content.
    if len(names) >= 3:
        return True
    scripts = sum(
        1 for script_range in INDIC_SCRIPT_RANGES
        if re.search(f"[{script_range}]", block)
    )
    return scripts >= 2


def _non_latin_block_total(text: str) -> int:
    """Count non-Latin letters that sit in mostly non-Latin paragraphs.

    A single Hindi motto is small and does not matter, but several Hindi
    sentences (a notice, a news list, a table) add up quickly.  Language
    menus are ignored because they are boilerplate on every page.
    """
    total = 0
    for block in re.split(r"[\n\r]+", text):
        block = block.strip()
        if len(block) < 20 or _looks_like_language_menu(block):
            continue
        indic, other, latin = _script_counts(block)
        if indic >= 20 and indic / max(1, indic + latin) >= 0.6:
            total += indic
        if other >= 20 and other / max(1, other + latin) >= 0.6:
            total += other
    return total


def is_english_language_value(value: str) -> bool:
    """True for values such as ``en``, ``en-US`` or ``English``."""
    cleaned = unquote_plus(str(value or "")).strip().lower().replace("_", "-")
    if not cleaned:
        return False
    primary = re.split(r"[-;,_]", cleaned)[0]
    return cleaned in ENGLISH_LANG_VALUES or primary in ("en", "eng", "english")


def url_is_other_language(url: str) -> bool:
    """True when a URL clearly points at a non-English version of a site."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False

    for match in LANG_QUERY_RE.finditer(parts.query or ""):
        if not is_english_language_value(match.group(1)):
            return True

    segments = [
        unquote(segment).strip().lower()
        for segment in (parts.path or "").split("/")
        if segment
    ]
    for index, segment in enumerate(segments):
        if segment in LANG_PATH_SEGMENTS:
            return True
        if index > 0 and segments[index - 1] in LANG_CONTAINER_SEGMENTS:
            if not is_english_language_value(segment):
                return True
    return False


def is_non_english_text(text: str) -> bool:
    """True when the visible text is mostly written in a non-Latin script.

    Small snippets are kept: an English page may carry a Hindi motto, a
    language switcher or a short notice.  The page is dropped when a non-Latin
    script dominates the text, when there is a large non-Latin block even
    beside English boilerplate, or when several paragraphs are non-Latin.
    """
    sample = (text or "")[:120000]
    if not sample.strip():
        return False
    indic, other, latin = _script_counts(sample)
    if indic >= 40 and indic / max(1, indic + latin) >= 0.35:
        return True
    if other >= 40 and other / max(1, other + latin) >= 0.35:
        return True
    # English menus and footers can inflate the Latin count on a Hindi page
    # (cssda.cg.nic.in is 24% Hindi but its main text is Hindi).  A large
    # non-Latin count with a modest share is still non-English.
    if indic >= 200 and indic / max(1, indic + latin) >= 0.15:
        return True
    if other >= 200 and other / max(1, other + latin) >= 0.15:
        return True
    # Hindi/Marathi paragraphs next to a lot of English boilerplate.
    if _non_latin_block_total(sample) >= 200:
        return True
    return False


def is_non_english_title(title: str) -> bool:
    """True when the page title itself is written in a non-Latin script.

    A bilingual title such as ``Greater Hyderabad Municipal Corporation
    హైదరాబాద్ మహానగర పాలక సంస్థ`` is not English-only, even when the body
    happens to be English.
    """
    sample = (title or "")[:500]
    if not sample.strip():
        return False
    indic, other, latin = _script_counts(sample)
    if indic >= 20 and indic / max(1, indic + latin) >= 0.35:
        return True
    if other >= 20 and other / max(1, other + latin) >= 0.35:
        return True
    return False

SKIP_PATH_SEGMENTS = {
    "en", "index", "home", "pages", "page", "content", "node", "default",
    "files", "sites", "site", "web", "main", "www", "lang", "language",
    "wp-content", "wp-json", "wp-includes", "feed", "rss", "search",
    "category", "tag", "author", "print", "amp",
}

LANG_NAMES = {
    "en": "English", "hi": "Hindi", "ta": "Tamil", "te": "Telugu",
    "bn": "Bengali", "mr": "Marathi", "gu": "Gujarati", "kn": "Kannada",
    "ml": "Malayalam", "pa": "Punjabi", "or": "Odia", "ur": "Urdu",
    "as": "Assamese", "sa": "Sanskrit", "ne": "Nepali", "si": "Sinhala",
    "fr": "French", "de": "German", "es": "Spanish", "ar": "Arabic",
    "zh": "Chinese", "ja": "Japanese", "ru": "Russian", "pt": "Portuguese",
    "it": "Italian", "nl": "Dutch", "ko": "Korean", "tr": "Turkish",
}

SCRIPT_LANG = (
    (r"[\u0900-\u097F]", "Hindi"),
    (r"[\u0B80-\u0BFF]", "Tamil"),
    (r"[\u0C00-\u0C7F]", "Telugu"),
    (r"[\u0C80-\u0CFF]", "Kannada"),
    (r"[\u0D00-\u0D7F]", "Malayalam"),
    (r"[\u0980-\u09FF]", "Bengali"),
    (r"[\u0A80-\u0AFF]", "Gujarati"),
    (r"[\u0A00-\u0A7F]", "Punjabi"),
    (r"[\u0B00-\u0B7F]", "Odia"),
)

HOME_WORDS = {"home", "home page", "homepage", "start", "startseite", "होम", "முகப்பு"}

WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

REQUIREMENTS_TXT = """\
requests>=2.31
beautifulsoup4>=4.12
lxml>=5.0
tqdm>=4.66
pdfplumber>=0.11
PyMuPDF>=1.24
python-dateutil>=2.9
openpyxl>=3.1
# Optional: JavaScript rendering fallback
# playwright>=1.44
# Optional: YouTube/Vimeo video downloads (--download-youtube)
# yt-dlp>=2024.4.9
"""

logger = logging.getLogger("site_scraper")


def install_system_trust() -> bool:
    """Use the operating system's certificate store when available.

    Many government sites send an incomplete certificate chain. Browsers and
    the Windows store can repair that automatically, while Python's bundled
    ``certifi`` list rejects it with "unable to get local issuer certificate".
    ``truststore`` makes Python use the same authorities as the OS.
    """
    if truststore is None:
        return False
    try:
        truststore.inject_into_ssl()
        return True
    except Exception:  # pragma: no cover - keep the bundled store on failure
        return False


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    """Return a filesystem-safe UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def clean_ws(value: Any, limit: int = 0) -> str:
    """Collapse all whitespace in *value* to single spaces."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def clean_category_label(value: Any, limit: int = 70) -> str:
    """Normalise a menu/breadcrumb label used as a category name.

    Rejects language-switcher dumps (``English Hindi (हिंदी) Tamil ...``) and
    strips decorative leading slashes, so a page never lands in a folder such
    as ``English_Hindi_(Hindi)_Assamese``.
    """
    label = clean_ws(value, limit).strip(" /\\|-–—•·:,")
    if len(label) < 2 or _looks_like_language_menu(label):
        return ""
    return label


def safe_component(
    value: Any,
    max_len: int = 40,
    default: str = "item",
    seed: str = "",
) -> str:
    """Convert arbitrary text into a Windows-safe file/folder name.

    Unicode letters are preserved (Tamil/Hindi folder names work on NTFS),
    illegal characters and reserved device names are handled, and the result
    is truncated by character count.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", text)
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"_+", "_", text).strip(" ._-")
    if not text:
        digest = hashlib.sha1((seed or str(value)).encode("utf-8")).hexdigest()[:8]
        text = f"{default}_{digest}"
    if text.lower() in WINDOWS_RESERVED:
        text = f"_{text}"
    text = text[:max_len].strip(" ._-") or default
    return text


def safe_topic_stem(
    value: Any,
    max_len: int = 120,
    default: str = "document",
    seed: str = "",
) -> str:
    """Make a Windows-safe filename stem while keeping normal spaces.

    ``safe_component`` turns spaces into underscores, which is right for
    folders but wrong for a document name copied from a Topic column.  This
    helper only removes characters Windows forbids in filenames.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" .")
    if not text:
        digest = hashlib.sha1((seed or str(value)).encode("utf-8")).hexdigest()[:8]
        text = f"{default}_{digest}"
    if text.lower() in WINDOWS_RESERVED:
        text = f"_{text}"
    return text[:max_len].strip(" .") or default


def derive_site_name(start_url: str) -> str:
    """Derive ``india_gov_in`` style folder name from a URL's host."""
    try:
        host = (urlsplit(start_url).hostname or "").lower().rstrip(".")
    except Exception:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except Exception:
        pass
    name = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    return name or "site"


def pretty_site_name(host: str) -> str:
    """Return a human-readable site name such as ``India.gov.in``."""
    host = (host or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "Unknown site"
    return host[:1].upper() + host[1:]


def base_domain(host: str) -> str:
    """Strip a leading ``www.`` from a hostname."""
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_internal(url: str, site_host: str) -> bool:
    """True when *url* belongs to the site domain or one of its subdomains."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    base = base_domain(site_host)
    return host == base or host.endswith("." + base)


def host_matches(url: str, host: str) -> bool:
    """True when *url*'s host is *host* or one of its subdomains."""
    try:
        target = (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    host = (host or "").lower().rstrip(".")
    if not target or not host:
        return False
    return target == host or target.endswith("." + host)


def same_site(url_a: str, url_b: str) -> bool:
    """True when both URLs share the same host (subdomains included)."""
    try:
        host_a = (urlsplit(url_a).hostname or "").lower().rstrip(".")
        host_b = (urlsplit(url_b).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host_a or not host_b:
        return False
    return (
        host_a == host_b
        or host_a.endswith("." + host_b)
        or host_b.endswith("." + host_a)
    )


EXTERNAL_CATEGORY = "External Sites"
SERVICE_PAGES_CATEGORY = "Service Pages"
SERVICE_DETAILS_PREFIX = "/services/details/"

# Social networks, search engines, app stores and ad/tracking hosts produce
# nothing but login walls, share dialogs and duplicate links.  External
# crawling skips them entirely.
EXTERNAL_BLOCKED_HOSTS = (
    "facebook.com", "fb.com", "m.facebook.com", "instagram.com",
    "twitter.com", "x.com", "t.co", "threads.net", "linkedin.com",
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
    "pinterest.com", "pin.it", "tiktok.com", "snapchat.com",
    "telegram.me", "t.me", "whatsapp.com", "wa.me", "reddit.com",
    "quora.com", "medium.com",
    "google.com", "google.co.in", "googleusercontent.com", "bing.com",
    "duckduckgo.com", "yahoo.com", "yandex.com",
    "play.google.com", "apps.apple.com", "itunes.apple.com",
    "amazon.in", "amazon.com", "flipkart.com", "paytm.com", "phonepe.com",
    "doubleclick.net", "googletagmanager.com", "google-analytics.com",
    "addthis.com", "sharethis.com", "addtoany.com",
)


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    """Normalize a URL for dedupe while preserving directory and query semantics.

    Returns ``None`` for non-HTTP(S) or invalid URLs.
    """
    if not url:
        return None
    url = html_lib.unescape(str(url)).strip()
    lowered = url.lower()
    if lowered.startswith(
        ("javascript:", "mailto:", "tel:", "sms:", "data:", "blob:", "about:", "file:")
    ):
        return None
    try:
        if base:
            url = urljoin(base, url)
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return None
        try:
            host = host.encode("idna").decode("ascii")
        except Exception:
            pass
        try:
            port = parts.port
        except ValueError:
            return None
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        else:
            netloc = host
        path = parts.path or "/"
        path = re.sub(r"/{2,}", "/", path)
        kept: list[tuple[str, str]] = []
        for key, val in parse_qsl(parts.query, keep_blank_values=True):
            low = key.lower()
            if low in DROP_QUERY_KEYS or any(low.startswith(p) for p in DROP_QUERY_PREFIXES):
                continue
            kept.append((key, val))
        query = urlencode(kept, doseq=True)
        # An SPA route is a separate document state; an ordinary #heading is not.
        fragment = parts.fragment if parts.fragment.startswith(("/", "!/")) else ""
        return urlunsplit((scheme, netloc, path, query, fragment))
    except Exception:
        return None


def url_suffix(url: str) -> str:
    """Return the lower-case file extension of a URL path (``.pdf`` etc.)."""
    try:
        path = unquote(urlsplit(url).path)
    except Exception:
        return ""
    return Path(path).suffix.lower()


def classify_media(url: str) -> Optional[str]:
    """Classify a direct file URL as image/pdf/audio/video/document."""
    ext = url_suffix(url)
    if ext in IMAGE_EXT:
        return "image"
    if ext in PDF_EXT:
        return "pdf"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in DOC_EXT:
        return "document"
    if is_video_embed(url):
        return "video"
    return None


def is_video_embed(url: str) -> bool:
    """True for known video-host embed/watch URLs."""
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    host = (parts.hostname or "").lower()
    path = parts.path.lower()
    if host == "youtu.be":
        return True
    if host.endswith("youtube.com") and (
        "/watch" in path or "/embed/" in path or "/shorts/" in path or "/v/" in path
    ):
        return True
    if host.endswith("vimeo.com") and re.search(r"/\d+", path):
        return True
    if host.endswith("dailymotion.com") and "/video/" in path:
        return True
    return False


def url_to_prid(url: str) -> str:
    """Stable 16-character page id derived from a URL."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _squash(value: Any) -> str:
    """Collapse whitespace so cosmetic re-rendering does not look like a change."""
    try:
        return re.sub(r"\s+", " ", str(value or "")).strip()
    except Exception:
        return ""


def content_fields(parsed: Any) -> dict[str, str]:
    """Return one short hash per content field of a parsed page.

    Navigation links, headers and footers are deliberately excluded: they
    change on almost every page of a site at once, which would make every
    page look modified.
    """
    values = {
        "title": _squash(getattr(parsed, "title", "")),
        "text": _squash(getattr(parsed, "text", "")),
        "date_time": _squash(getattr(parsed, "date_time", "")),
        "images": "\n".join(sorted(url for url, _ in (getattr(parsed, "images", None) or []))),
        "pdfs": "\n".join(sorted(getattr(parsed, "pdfs", None) or [])),
        "audio": "\n".join(sorted(getattr(parsed, "audios", None) or [])),
        "video": "\n".join(sorted(getattr(parsed, "videos", None) or [])),
        "links": "\n".join(sorted(x["url"] for x in parsed.links)),
        "documents": "\n".join(sorted(getattr(parsed, "documents", None) or [])),
    }
    return {
        name: hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
        for name, value in values.items()
    }


def content_fingerprint(fields: dict[str, str]) -> str:
    """Combine per-field hashes into one cheap change-detection hash."""
    blob = "|".join(f"{name}={fields[name]}" for name in sorted(fields))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def iso_to_epoch(value: str) -> float:
    """Parse an ISO-8601 timestamp to seconds since the epoch (0 on failure)."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def files_identical(first: Path, second: Path, chunk_size: int = 1024 * 1024) -> bool:
    """Byte-for-byte comparison used when re-checking an already-downloaded file."""
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with open(first, "rb") as left, open(second, "rb") as right:
            while True:
                left_chunk = left.read(chunk_size)
                right_chunk = right.read(chunk_size)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def media_filename(
    url: str,
    directory: Path,
    kind: str,
    ext_hint: str = "",
    display_name: str = "",
) -> Path:
    """Build a path-length-safe filename for a media URL.

    With *display_name* the Topic-column text is used as the stem.  Without it
    the old URL-based hashed stem is kept, so non-document media and pages with
    no Topic column behave exactly as before.
    """
    ext = (url_suffix(url) or ext_hint or KIND_DEFAULT_EXT.get(kind, ".bin")).lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = KIND_DEFAULT_EXT.get(kind, ".bin")

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    if display_name:
        stem = safe_topic_stem(display_name, max_len=120, default=kind, seed=url)
        name = f"{stem}{ext}"
    else:
        parsed_name = ""
        try:
            parsed_name = Path(unquote(urlsplit(url).path)).name
        except Exception:
            parsed_name = ""
        raw_stem = Path(parsed_name).stem if parsed_name else kind
        stem = safe_component(raw_stem, max_len=60, default=kind, seed=url)
        name = f"{digest}_{stem}{ext}"

    budget = MAX_PATH_LEN - len(str(directory)) - 1
    if budget < 12:
        return directory / f"{digest}{ext}"
    if len(name) > budget:
        if display_name:
            keep = max(8, budget - len(ext))
            stem = stem[:keep].strip(" .") or kind
            name = f"{stem}{ext}"
        else:
            keep = max(8, budget - len(ext) - len(digest) - 1)
            name = f"{digest}_{stem[:keep]}{ext}"
    return directory / name


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically (UTF-8, indented)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def parse_date_value(value: str) -> Optional[datetime]:
    """Parse the many date formats found on government/legacy websites."""
    if not value:
        return None
    text = clean_ws(value)
    text = re.sub(
        r"^(?:on|published(?: on)?|updated(?: on)?|last updated(?: on)?|"
        r"posted(?: on)?|date(?:\s*:)?)\s*[:\-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(IST|GMT|UTC)\b", "", text, flags=re.IGNORECASE).strip(" ,;")
    if not text or len(text) > 80:
        return None
    iso_like = bool(re.match(r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", text))
    try:
        from dateutil import parser as date_parser  # type: ignore

        dt = date_parser.parse(text, fuzzy=True, dayfirst=not iso_like)
    except Exception:
        dt = None
        for fmt in (
            "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
            "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
            "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
            "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    now = datetime.now()
    if dt.year < 1980 or dt.year > now.year + 1:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class TqdmLoggingHandler(logging.Handler):
    """Logging handler that plays nicely with a tqdm progress bar."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record), file=sys.stderr)
        except Exception:  # pragma: no cover
            self.handleError(record)


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    """Configure UTF-8 file logging plus tqdm-safe console logging."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(threadName)-12s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(logs_dir / "scraper.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    console_handler = TqdmLoggingHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False


# ---------------------------------------------------------------------------
# Failure log
# ---------------------------------------------------------------------------

class FailureLog:
    """Thread-safe append-only failure log (logs/failed_urls.txt)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.count = 0

    def add(self, url: str, reason: str) -> None:
        reason = clean_ws(reason, limit=300).replace("\t", " ")
        line = f"{utc_now_iso()}\t{url}\t{reason}\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
            self.count += 1


# ---------------------------------------------------------------------------
# Politeness / HTTP
# ---------------------------------------------------------------------------

class RateLimiter:
    """Global (per-crawl) request spacer shared by all worker threads."""

    def __init__(self, delay: float) -> None:
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.delay = max(0.0, float(delay))

    def set_delay(self, delay: float) -> None:
        with self._lock:
            self.delay = max(0.0, float(delay))

    def slow_down(self, factor: float = 1.5, cap: float = 20.0) -> float:
        """Increase the spacing after the server shows signs of rate limiting.

        Returns the new delay so the caller can log it.  The delay only ever
        grows here; it is never reduced automatically, which keeps a crawl
        polite once a site has pushed back.
        """
        with self._lock:
            self.delay = min(cap, max(self.delay * factor, self.delay + 0.5))
            return self.delay

    def wait(self, stop_event: Optional[threading.Event] = None) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_at)
            # Small random jitter: perfectly even spacing looks robotic to WAFs.
            self._next_at = slot + self.delay * random.uniform(1.0, 1.35)
        sleep_for = slot - time.monotonic()
        if sleep_for > 0:
            if stop_event is not None:
                stop_event.wait(sleep_for)
            else:
                time.sleep(sleep_for)


class ProxyPool:
    """Small per-crawl proxy rotator.

    An empty pool means "use the machine's own IP".  With proxies configured,
    each worker keeps a sticky proxy until a block signal moves it to another
    one (``rotate_on_block``), or every request can take the next proxy
    (``rotate_per_page``).
    """

    MODES = {"rotate_on_block", "rotate_per_page", "sticky"}

    def __init__(self, entries: Iterable[str] | None = None,
                 mode: str = "rotate_on_block") -> None:
        seen: set[str] = set()
        self.proxies: list[str] = []
        for raw in entries or []:
            text = str(raw or "").strip()
            if not text or text.startswith("#") or text in seen:
                continue
            try:
                parts = urlsplit(text)
            except Exception:
                continue
            if parts.scheme not in ("http", "https", "socks5", "socks5h"):
                continue
            if not parts.hostname or not parts.port:
                continue
            seen.add(text)
            self.proxies.append(text)
        self.mode = mode if mode in self.MODES else "rotate_on_block"
        self._lock = threading.Lock()
        self._cursor = 0
        self._cooldown: dict[str, float] = {}
        self._assigned: dict[int, str] = {}
        self.rotations = 0

    def __bool__(self) -> bool:
        return bool(self.proxies)

    def _pick_locked(self, exclude: str = "") -> str | None:
        if not self.proxies:
            return None
        now = time.monotonic()
        if all(self._cooldown.get(p, 0) > now for p in self.proxies):
            self._cooldown.clear()
        for offset in range(len(self.proxies)):
            idx = (self._cursor + offset) % len(self.proxies)
            proxy = self.proxies[idx]
            if proxy == exclude or self._cooldown.get(proxy, 0) > now:
                continue
            self._cursor = (idx + 1) % len(self.proxies)
            return proxy
        return self.proxies[self._cursor % len(self.proxies)]

    def acquire(self) -> str | None:
        if not self.proxies:
            return None
        if self.mode == "rotate_per_page":
            with self._lock:
                return self._pick_locked()
        key = threading.get_ident()
        with self._lock:
            proxy = self._assigned.get(key)
            if not proxy:
                proxy = self._pick_locked()
                if proxy:
                    self._assigned[key] = proxy
            return proxy

    def mark_bad(self, proxy: str | None, reason: str = "") -> None:
        if not proxy or self.mode == "sticky" or not self.proxies:
            return
        with self._lock:
            self._cooldown[proxy] = time.monotonic() + 60.0
            self.rotations += 1
            if self.mode == "rotate_on_block":
                for key, current in list(self._assigned.items()):
                    if current == proxy:
                        new = self._pick_locked(exclude=proxy)
                        if new:
                            self._assigned[key] = new
        if reason:
            logger.warning("Proxy %s marked unavailable (%s); rotating.",
                           mask_proxy(proxy), reason)

    def mark_good(self, proxy: str | None) -> None:
        if not proxy:
            return
        with self._lock:
            self._cooldown.pop(proxy, None)

    def stats(self) -> dict:
        return {
            "configured": len(self.proxies),
            "mode": self.mode,
            "rotations": self.rotations,
        }


def mask_proxy(proxy: str | None) -> str:
    """Hide credentials and most of the host when writing proxies to logs."""
    if not proxy:
        return "direct"
    try:
        parts = urlsplit(proxy)
        host = parts.hostname or ""
        hidden = (host[:3] + "***") if len(host) > 3 else "***"
        return f"{parts.scheme}://{hidden}:{parts.port}"
    except Exception:
        return "***"


def requests_proxies(proxy: str | None) -> dict[str, str] | None:
    """Proxy mapping for requests; None means use the real connection."""
    return {"http": proxy, "https": proxy} if proxy else None


def playwright_proxy(proxy: str | None) -> dict[str, str] | None:
    """Proxy mapping for a Playwright browser context."""
    if not proxy:
        return None
    try:
        parts = urlsplit(proxy)
        out = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
        if parts.username:
            out["username"] = unquote(parts.username)
        if parts.password:
            out["password"] = unquote(parts.password)
        return out
    except Exception:
        return None


class RobotsGate:
    """robots.txt cache / policy checker (per host, thread-safe)."""

    def __init__(self, user_agent: str, timeout: float = 10.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, Optional[RobotFileParser]] = {}
        self._lock = threading.Lock()

    def _get(self, url: str) -> Optional[RobotFileParser]:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            scheme = parts.scheme or "https"
        except Exception:
            return None
        if not host:
            return None
        key = f"{scheme}://{parts.netloc}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        robots_url = f"{key}/robots.txt"
        parser: Optional[RobotFileParser] = None
        try:
            # A robots.txt redirect (dof.gov.in -> www.dof.gov.in, for
            # example) is normal.  Follow it; rejecting the whole origin
            # because of a 302 makes the crawler refuse sites that explicitly
            # allow crawling.
            resp = requests.get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
                allow_redirects=True,
            )
            status = resp.status_code
            if status >= 500:
                # RFC 9309: an unreachable robots file means "disallow all".
                logger.warning(
                    "robots.txt returned HTTP %d for %s; skipping this origin.",
                    status, robots_url,
                )
                parser = RobotFileParser()
                parser.disallow_all = True
            elif status == 200:
                parser = RobotFileParser()
                parser.set_url(resp.url or robots_url)
                parser.parse(resp.text.splitlines())
            else:
                # RFC 9309: 4xx means no restrictions.  A WAF may answer 403
                # for robots.txt while the site itself is crawlable.
                parser = None
        except requests.RequestException as exc:
            parser = RobotFileParser()
            parser.disallow_all = True
            logger.debug("robots.txt fetch failed for %s: %s", robots_url, exc)
        with self._lock:
            self._cache[key] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._get(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def crawl_delay(self, url: str) -> float:
        parser = self._get(url)
        if parser is None:
            return 0.0
        try:
            value = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*")
            return float(value or 0.0)
        except Exception:
            return 0.0


class HttpClient:
    """Thread-safe-ish HTTP client: per-thread sessions, shared politeness."""

    def __init__(
        self,
        user_agent: str,
        limiter: RateLimiter,
        robots: RobotsGate,
        failures: FailureLog,
        stop_event: threading.Event,
        retries: int = 3,
        timeout: tuple[float, float] = (15.0, 90.0),
        verify_ssl: bool = True,
        ssl_fallback: bool = True,
        proxy_pool: Optional[ProxyPool] = None,
    ) -> None:
        self.user_agent = user_agent
        self.limiter = limiter
        self.robots = robots
        self.failures = failures
        self.stop_event = stop_event
        self.retries = max(1, retries)
        self.timeout = timeout
        # verify_ssl=False (--insecure) never checks certificates at all.
        # ssl_fallback=True retries a host without verification only when the
        # certificate chain itself is rejected, so an incomplete chain on a
        # government site cannot kill a whole crawl.
        self.verify_ssl = bool(verify_ssl)
        self.ssl_fallback = bool(ssl_fallback) and self.verify_ssl
        self.proxy_pool = proxy_pool
        self._insecure_hosts: set[str] = set()
        self._insecure_lock = threading.Lock()
        # One cookie jar for every thread.  Sites such as dare.gov.in keep the
        # chosen language in a session cookie, so the language switch must be
        # visible to all workers, not just the thread that clicked it.
        self.cookies = requests.cookies.RequestsCookieJar()
        if not self.verify_ssl:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        self._local = threading.local()
        self.request_count = 0
        self._count_lock = threading.Lock()
        # Adaptive politeness: when a server answers 429/503 or an "Access
        # Denied" page, the crawl slows itself down instead of hammering on.
        self._throttle_lock = threading.Lock()
        self._last_throttle = 0.0
        self.throttle_events = 0
        # Ban handling shared by every worker thread.  When the site starts
        # refusing us (429/503, or 403 while a known-good page still loads) ALL
        # requests pause together with an escalating back-off, then continue
        # more slowly.  Pages are retried after the pause instead of being lost.
        self._ban_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._ban_level = 0
        self._ok_streak = 0
        self._probe_urls: dict[str, str] = {}
        self.block_retries = 4
        self.cooldowns = 0

    @staticmethod
    def _host_of(url: str) -> str:
        try:
            return (urlsplit(url).hostname or "").lower()
        except Exception:
            return ""

    def note_pressure(self, url: str, reason: str, cooldown: bool = False) -> None:
        """Slow the whole crawl down after a rate-limit / block signal.

        The delay increase is applied at most once every 15 seconds so a burst
        of 429s seen by many worker threads cannot multiply it out of control.
        ``cooldown=True`` additionally pauses every worker (see start_cooldown).
        """
        if cooldown:
            self.start_cooldown(url, reason)
        now = time.monotonic()
        with self._throttle_lock:
            if now - self._last_throttle < 15.0:
                return
            self._last_throttle = now
            self.throttle_events += 1
        new_delay = self.limiter.slow_down(factor=2.0)
        logger.warning(
            "Politeness: %s on %s -- slowing down to %.1fs between requests.",
            reason, url, new_delay,
        )

    # -- ban handling -----------------------------------------------------
    def wait_if_cooling(self) -> None:
        """Block the calling worker while a site-wide cool-down is active."""
        while not self.stop_event.is_set():
            with self._ban_lock:
                remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(remaining, 5.0))

    def start_cooldown(self, url: str, reason: str, retry_after: float = 0.0) -> float:
        """Pause all requests (30s, 60s, 120s ... up to 15 min, or Retry-After)."""
        now = time.monotonic()
        with self._ban_lock:
            if now < self._cooldown_until:      # another worker already did it
                return self._cooldown_until - now
            self._ban_level = min(self._ban_level + 1, 6)
            pause = min(900.0, max(float(retry_after or 0.0),
                                   30.0 * 2 ** (self._ban_level - 1)))
            self._cooldown_until = now + pause
            self._ok_streak = 0
            self.cooldowns += 1
            level = self._ban_level
        logger.warning(
            "Block suspected (%s) at %s. Pausing ALL requests for %.0fs "
            "(level %d), then continuing more slowly; the page will be retried.",
            reason, url, pause, level,
        )
        return pause

    def _note_ok(self, url: str, host: str) -> None:
        with self._ban_lock:
            self._ok_streak += 1
            if self._ok_streak >= 20:
                self._ban_level = 0
            if host and host not in self._probe_urls and url.rstrip("/").count("/") <= 3:
                self._probe_urls[host] = url    # a shallow known-good page

    def _host_is_banned(self, host: str, failing_url: str) -> bool:
        """After a 403: does a page that worked before still load?

        If it does, only *this* URL is forbidden (a permanent per-page 403) and
        pausing the whole crawl would be wrong.  If it does not (or nothing has
        ever worked), the site is refusing us and a cool-down is the right move.
        """
        with self._ban_lock:
            probe = self._probe_urls.get(host)
        if not probe or probe == failing_url:
            return True
        try:
            self.limiter.wait(self.stop_event)
            resp = self._session(self._is_insecure(host)).get(
                probe, timeout=(10, 25), allow_redirects=False,
            )
            ok = resp.status_code == 200 and not looks_blocked_page(resp.text[:8000])
            resp.close()
            return not ok
        except requests.RequestException:
            return True

    @staticmethod
    def _origin_of(url: str) -> str:
        try:
            parts = urlsplit(url)
            if parts.path in ("", "/") and not parts.query:
                return ""                       # a first visit has no referer
            return f"{parts.scheme}://{parts.netloc}/"
        except Exception:
            return ""

    @staticmethod
    def _is_cert_error(exc: Exception) -> bool:
        # requests raises SSLError for certificate problems; some urllib3
        # versions wrap the same cause in ConnectionError, so check the text
        # too. A protocol-only SSLError simply fails again after the retry.
        if isinstance(exc, requests.exceptions.SSLError):
            return True
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "certificate verify failed",
                "certificate_verify_failed",
                "unable to get local issuer certificate",
                "unable to verify the first certificate",
                "certificate is not trusted",
                "not trusted by the trust provider",
                "self-signed certificate",
                "sslerror",
            )
        )

    def _is_insecure(self, host: str) -> bool:
        if not self.verify_ssl:
            return True
        if not host:
            return False
        with self._insecure_lock:
            return host in self._insecure_hosts

    def _allow_insecure(self, host: str, url: str, exc: Exception) -> bool:
        """Remember one host whose certificate chain cannot be verified."""
        if not self.ssl_fallback or not host:
            return False
        with self._insecure_lock:
            first = host not in self._insecure_hosts
            self._insecure_hosts.add(host)
        if first:
            logger.warning(
                "TLS certificate for %s could not be verified (%s). Retrying "
                "this host without certificate verification; use --strict-ssl "
                "to disable this fallback.",
                host, exc,
            )
        return True

    def _session(self, insecure: bool = False) -> requests.Session:
        attr = "insecure_session" if insecure else "session"
        session = getattr(self._local, attr, None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                              "image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-User": "?1",
                }
            )
            if insecure:
                session.verify = False
            # Share one jar so a language switch (or any site preference kept
            # in a cookie) applies to every worker thread.
            session.cookies = self.cookies
            adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            setattr(self._local, attr, session)
        return session

    def get(
        self,
        url: str,
        stream: bool = False,
        check_robots: bool = True,
        referer: str = "",
        validators: Optional[dict[str, str]] = None,
        _redirects: int = 0,
    ) -> Optional[requests.Response]:
        """GET a URL with retries, back-off and ban handling.

        Returns ``None`` on failure.
        """
        if self.stop_event.is_set():
            return None
        if check_robots and not self.robots.allowed(url):
            logger.info("Blocked by robots.txt: %s", url)
            return None
        ref = referer or self._origin_of(url)
        headers = {"Referer": ref, "Sec-Fetch-Site": "same-origin"} if ref else {
            "Sec-Fetch-Site": "none"}
        host = self._host_of(url)
        headers.update(validators or {})
        last_error = ""
        attempt = 0
        block_cycles = 0
        while attempt < self.retries:
            attempt += 1
            if self.stop_event.is_set():
                return None
            self.wait_if_cooling()
            self.limiter.wait(self.stop_event)
            if self.stop_event.is_set():
                return None
            proxy = self.proxy_pool.acquire() if self.proxy_pool else None
            try:
                with self._count_lock:
                    self.request_count += 1
                resp = self._session(self._is_insecure(host)).get(
                    url,
                    timeout=self.timeout,
                    stream=stream,
                    allow_redirects=False,
                    headers=headers,
                    proxies=requests_proxies(proxy),
                )
                if resp.is_redirect:
                    target = normalize_url(resp.headers["Location"], base=url)
                    resp.close()
                    if (not target or _redirects >= 10 or
                            not getattr(self, "allow_redirect", lambda a, b: True)(url, target)):
                        self.failures.add(url, "redirect out of scope or redirect limit")
                        return None
                    return self.get(target, stream=stream, referer=url,
                                    validators=validators if same_site(url, target) else None,
                                    _redirects=_redirects + 1)
                if resp.status_code in (403, 429, 503):
                    self.note_pressure(url, f"HTTP {resp.status_code}")
                    if self.proxy_pool and proxy:
                        self.proxy_pool.mark_bad(proxy, f"HTTP {resp.status_code}")
                        if attempt < self.retries:
                            resp.close()
                            wait = 2 ** (attempt - 1) + random.uniform(0, 0.6)
                            logger.warning(
                                "Blocked (%s) for %s (attempt %d/%d); retrying on "
                                "another proxy in %.1fs",
                                resp.status_code, url, attempt, self.retries, wait,
                            )
                            self.stop_event.wait(wait)
                            continue
                    elif block_cycles < self.block_retries:
                        code = resp.status_code
                        retry_after = resp.headers.get("Retry-After", "")
                        # 429/503 always mean "slow down".  A 403 is a site-wide
                        # ban only when a page that loaded earlier now fails too.
                        if code in (429, 503) or self._host_is_banned(host, url):
                            block_cycles += 1
                            resp.close()
                            self.start_cooldown(
                                url, f"HTTP {code}",
                                float(retry_after) if retry_after.isdigit() else 0.0,
                            )
                            last_error = f"HTTP {code}"
                            attempt -= 1        # a ban retry is not a normal attempt
                            continue
                if resp.status_code in (408, 425, 429, 500, 502, 503, 504) and attempt < self.retries:
                    retry_after = resp.headers.get("Retry-After", "")
                    resp.close()
                    wait = 2 ** (attempt - 1) + random.uniform(0, 0.6)
                    if retry_after.isdigit():
                        wait = max(wait, min(float(retry_after), 30.0))
                    logger.warning(
                        "HTTP %s for %s (attempt %d/%d), retrying in %.1fs",
                        resp.status_code, url, attempt, self.retries, wait,
                    )
                    self.stop_event.wait(wait)
                    continue
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}"
                    resp.close()
                    if 400 <= resp.status_code < 500 and resp.status_code not in (408, 425, 429):
                        break
                    continue
                if self.proxy_pool and proxy:
                    self.proxy_pool.mark_good(proxy)
                self._note_ok(url, host)
                return resp
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("Request error (%d/%d) %s: %s", attempt, self.retries, url, exc)
                if self.proxy_pool and proxy and isinstance(exc, requests.exceptions.ProxyError):
                    self.proxy_pool.mark_bad(proxy, "proxy connection failed")
                if self._is_cert_error(exc) and self._allow_insecure(host, url, exc):
                    continue  # retry this host immediately without verification
                if attempt < self.retries:
                    self.stop_event.wait(2 ** (attempt - 1) + random.uniform(0, 0.6))
        if last_error:
            self.failures.add(url, last_error)
            logger.warning("Giving up on %s (%s)", url, last_error)
        return None

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        check_robots: bool = True,
        timeout: Optional[tuple[float, float]] = None,
    ) -> Optional[Any]:
        """POST a JSON body and return the decoded response (``None`` on failure).

        Used for site APIs that expose content the HTML never links to (for
        example india.gov.in's service catalogue).  It follows the same
        politeness/retry/failure rules as :meth:`get`.
        """
        if self.stop_event.is_set():
            return None
        if check_robots and not self.robots.allowed(url):
            logger.info("Blocked by robots.txt: %s", url)
            return None
        last_error = ""
        host = self._host_of(url)
        for attempt in range(1, self.retries + 1):
            if self.stop_event.is_set():
                return None
            self.wait_if_cooling()
            self.limiter.wait(self.stop_event)
            if self.stop_event.is_set():
                return None
            proxy = self.proxy_pool.acquire() if self.proxy_pool else None
            try:
                with self._count_lock:
                    self.request_count += 1
                resp = self._session(self._is_insecure(host)).post(
                    url,
                    json=payload,
                    timeout=timeout or (30.0, 120.0),
                    allow_redirects=True,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    proxies=requests_proxies(proxy),
                )
                if resp.status_code in (403, 429, 503):
                    self.note_pressure(url, f"HTTP {resp.status_code}")
                    if self.proxy_pool and proxy:
                        self.proxy_pool.mark_bad(proxy, f"HTTP {resp.status_code}")
                        if attempt < self.retries:
                            resp.close()
                            wait = 2 ** (attempt - 1) + random.uniform(0, 0.6)
                            logger.warning(
                                "Blocked (%s) for %s (attempt %d/%d); retrying on "
                                "another proxy in %.1fs",
                                resp.status_code, url, attempt, self.retries, wait,
                            )
                            self.stop_event.wait(wait)
                            continue
                if resp.status_code in (408, 425, 429, 500, 502, 503, 504) and attempt < self.retries:
                    retry_after = resp.headers.get("Retry-After", "")
                    resp.close()
                    wait = 2 ** (attempt - 1) + random.uniform(0, 0.6)
                    if retry_after.isdigit():
                        wait = max(wait, min(float(retry_after), 30.0))
                    logger.warning(
                        "HTTP %s for %s (attempt %d/%d), retrying in %.1fs",
                        resp.status_code, url, attempt, self.retries, wait,
                    )
                    self.stop_event.wait(wait)
                    continue
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}"
                    resp.close()
                    if 400 <= resp.status_code < 500 and resp.status_code not in (408, 425, 429):
                        break
                    continue
                if self.proxy_pool and proxy:
                    self.proxy_pool.mark_good(proxy)
                try:
                    return resp.json()
                except ValueError as exc:
                    last_error = f"invalid JSON: {exc}"
                    resp.close()
                    break
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("POST error (%d/%d) %s: %s", attempt, self.retries, url, exc)
                if self.proxy_pool and proxy and isinstance(exc, requests.exceptions.ProxyError):
                    self.proxy_pool.mark_bad(proxy, "proxy connection failed")
                if self._is_cert_error(exc) and self._allow_insecure(host, url, exc):
                    continue  # retry this host immediately without verification
                if attempt < self.retries:
                    self.stop_event.wait(2 ** (attempt - 1) + random.uniform(0, 0.6))
        if last_error:
            self.failures.add(url, last_error)
            logger.warning("Giving up on %s (%s)", url, last_error)
        return None

    def close(self) -> None:
        for attr in ("session", "insecure_session"):
            session = getattr(self._local, attr, None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Site folder layout
# ---------------------------------------------------------------------------

CONFIG_FILENAME = "config.json"


class SiteFolderManager:
    """Owns every path inside ``<root>/<site_name>`` and creates the layout."""

    def __init__(self, root_dir: str, start_url: str) -> None:
        self.start_url = start_url
        self.root_dir = Path(root_dir).expanduser()
        self.site_name = derive_site_name(start_url)
        self.site_dir = self.root_dir / self.site_name
        self.scraper_dir = self.site_dir / "scraper"
        self.categories_dir = self.site_dir / "categories"
        self.data_dir = self.site_dir / "data"
        self.logs_dir = self.site_dir / "logs"
        parsed = urlsplit(start_url)
        self.host = (parsed.hostname or "").lower()
        self.domain = base_domain(self.host)
        self.website_name = pretty_site_name(self.host)
        self.source_path = Path(__file__).resolve()
        self.config_path = self.scraper_dir / CONFIG_FILENAME
        self.state_path = self.data_dir / "state.json"
        self.index_path = self.data_dir / "category_index.json"
        self.failed_path = self.logs_dir / "failed_urls.txt"

    def ensure_layout(self) -> None:
        """Create root/site/scraper/categories/data/logs directories."""
        self.root_dir.mkdir(parents=True, exist_ok=True)
        for folder in (
            self.site_dir, self.scraper_dir, self.categories_dir,
            self.data_dir, self.logs_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        target = self.scraper_dir / "scraper.py"
        try:
            if self.source_path != target.resolve():
                shutil.copy2(self.source_path, target)
                for name in ("translator.py", "navigation.py", "hyperlinks.py", "crawl_modes.py", "crawl_queue.py"):
                    shutil.copy2(self.source_path.with_name(name), target.with_name(name))
        except Exception as exc:  # pragma: no cover - non fatal
            logger.debug("Could not copy scraper source: %s", exc)
        req = self.scraper_dir / "requirements.txt"
        if not req.exists():
            req.write_text(REQUIREMENTS_TXT, encoding="utf-8")

    def write_config(self, settings: dict[str, Any]) -> None:
        """Write/refresh scraper/config.json, preserving the creation time."""
        created_at = utc_now_iso()
        if self.config_path.exists():
            try:
                old = json.loads(self.config_path.read_text(encoding="utf-8"))
                created_at = old.get("created_at", created_at)
            except Exception:
                pass
        payload = {
            "version": VERSION,
            "start_url": self.start_url,
            "domain": self.domain,
            "site_name": self.site_name,
            "root_dir": str(self.root_dir),
            "settings": settings,
            "created_at": created_at,
            "updated_at": utc_now_iso(),
        }
        atomic_write_json(self.config_path, payload)

    def category_dir(self, folder: str) -> Path:
        return self.categories_dir / folder

    def media_dir(self, folder: str, kind: str, page_id: str) -> Path:
        dirname = KIND_DIRS.get(kind, "documents")
        return self.category_dir(folder) / "media" / dirname / page_id

    def rel(self, path: Path) -> str:
        """Return a POSIX path relative to the site folder (portable JSON)."""
        try:
            return path.resolve().relative_to(self.site_dir.resolve()).as_posix()
        except Exception:
            return path.as_posix()


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@dataclass
class CategoryInfo:
    """Runtime metadata and counters for one category."""

    folder: str
    display: str
    path: Path
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "pages": 0, "images": 0, "pdfs": 0, "audio": 0,
            "video": 0, "documents": 0,
        }
    )


@dataclass
class CategoryMatch:
    """Result of category detection for one page."""

    primary: str
    categories: list[str]
    section: str


class CategoryManager:
    """Detects categories and manages their folders, files and counters."""

    def __init__(self, site: SiteFolderManager) -> None:
        self.site = site
        self._lock = threading.RLock()
        self._categories: dict[str, CategoryInfo] = {}
        self._by_display: dict[str, CategoryInfo] = {}
        self._nav: dict[str, str] = {}
        self._nav_lock = threading.Lock()
        self._load_index()

    # -- persistence ------------------------------------------------------
    def _load_index(self) -> None:
        if not self.site.index_path.exists():
            return
        try:
            payload = json.loads(self.site.index_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for folder, meta in (payload.get("categories") or {}).items():
            display = clean_ws(meta.get("display_name") or folder, 80) or folder
            counts = {
                key: int(meta.get(key, 0) or 0)
                for key in ("pages", "images", "pdfs", "audio", "video", "documents")
            }
            info = CategoryInfo(
                folder=folder, display=display,
                path=self.site.category_dir(folder), counts=counts,
            )
            self._categories[folder] = info
            self._by_display[display.lower()] = info

    def write_index(self) -> None:
        """Persist category -> page/media counters to data/category_index.json."""
        with self._lock:
            payload = {
                "site_name": self.site.site_name,
                "website_name": self.site.website_name,
                "updated_at": utc_now_iso(),
                "categories": {
                    info.folder: {
                        "display_name": info.display,
                        "folder": f"categories/{info.folder}",
                        "pages": info.counts["pages"],
                        "images": info.counts["images"],
                        "pdfs": info.counts["pdfs"],
                        "audio": info.counts["audio"],
                        "video": info.counts["video"],
                        "documents": info.counts["documents"],
                    }
                    for info in sorted(self._categories.values(), key=lambda c: c.folder)
                },
            }
        atomic_write_json(self.site.index_path, payload)

    # -- resolution -------------------------------------------------------
    def resolve(self, display_name: str) -> CategoryInfo:
        """Return (creating if needed) the folder info for a display category."""
        display = clean_ws(display_name, 80) or "Uncategorized"
        key = display.lower()
        with self._lock:
            if key in self._by_display:
                return self._by_display[key]
            folder = safe_component(
                display.lower(), 40, default="uncategorized", seed=display
            )
            if folder in self._categories:
                suffix = hashlib.sha1(display.encode("utf-8")).hexdigest()[:6]
                folder = f"{folder}_{suffix}"
            info = CategoryInfo(
                folder=folder, display=display,
                path=self.site.category_dir(folder),
            )
            info.path.mkdir(parents=True, exist_ok=True)
            (info.path / "media").mkdir(exist_ok=True)
            for dirname in KIND_DIRS.values():
                (info.path / "media" / dirname).mkdir(exist_ok=True)
            pages_file = info.path / "pages.jsonl"
            if not pages_file.exists():
                pages_file.touch()
            self._categories[folder] = info
            self._by_display[key] = info
            logger.info("New category discovered: %s -> categories/%s", display, folder)
            return info

    def get(self, folder: str) -> Optional[CategoryInfo]:
        with self._lock:
            return self._categories.get(folder)

    def all(self) -> list[CategoryInfo]:
        with self._lock:
            return sorted(self._categories.values(), key=lambda c: c.folder)

    # -- counters ---------------------------------------------------------
    def increment_pages(self, folder: str, amount: int = 1) -> None:
        with self._lock:
            if folder in self._categories:
                self._categories[folder].counts["pages"] += amount

    def increment_media(self, folder: str, kind: str, amount: int = 1) -> None:
        key = KIND_COUNT_KEYS.get(kind, "documents")
        with self._lock:
            if folder in self._categories:
                self._categories[folder].counts[key] += amount

    def recount(self) -> None:
        """Recompute counters from files on disk (used for the final summary)."""
        for info in self.all():
            pages_file = info.path / "pages.jsonl"
            pages = 0
            if pages_file.exists():
                try:
                    with open(pages_file, "r", encoding="utf-8") as fh:
                        pages = sum(1 for line in fh if line.strip())
                except Exception:
                    pages = info.counts["pages"]
            media_counts: dict[str, int] = {}
            for kind, dirname in KIND_DIRS.items():
                base = info.path / "media" / dirname
                count = 0
                if base.exists():
                    for child in base.rglob("*"):
                        if not child.is_file():
                            continue
                        # The extracted PDF text sidecar (.txt) is not a PDF.
                        if dirname == "pdfs" and child.suffix.lower() != ".pdf":
                            continue
                        count += 1
                media_counts[kind] = count
            with self._lock:
                info.counts["pages"] = pages
                info.counts["images"] = media_counts["image"]
                info.counts["pdfs"] = media_counts["pdf"]
                info.counts["audio"] = media_counts["audio"]
                info.counts["video"] = media_counts["video"]
                info.counts["documents"] = media_counts["document"]

    # -- navigation map ---------------------------------------------------
    def learn_nav(self, entries: Iterable[tuple[str, str]]) -> None:
        """Learn top-level menu paths from any crawled page."""
        with self._nav_lock:
            for path, display in entries:
                path = path or "/"
                display = clean_ws(display, 70)
                if path == "/" or len(display) < 2 or path in self._nav:
                    continue
                if _looks_like_language_menu(display):
                    continue
                self._nav[path] = display

    def _nav_match(self, url: str) -> str:
        try:
            parts = urlsplit(url)
            path = parts.path.rstrip("/") or "/"
        except Exception:
            return ""
        with self._nav_lock:
            route = path + ("?" + parts.query if parts.query else "")
            if parts.fragment and (label := self._nav.get(route + "#" + parts.fragment)):
                return label
            if parts.query and (label := self._nav.get(path + "?" + parts.query)):
                return label
            while path and path != "/":
                if path in self._nav:
                    return self._nav[path]
                path = path.rpartition("/")[0]
        return ""

    # -- detection --------------------------------------------------------
    @staticmethod
    def _segment_category(url: str) -> str:
        try:
            path = unquote(urlsplit(url).path)
        except Exception:
            return ""
        for segment in path.split("/"):
            segment = segment.strip()
            if not segment:
                continue
            low = segment.lower()
            if low in SKIP_PATH_SEGMENTS:
                continue
            if re.fullmatch(r"(?:p|page|pg)?\d+", low):
                continue
            if "." in low:
                base, ext = low.rsplit(".", 1)
                if ext in ("html", "htm", "php", "aspx", "jsp", "asp", "shtml"):
                    segment = segment[: -(len(ext) + 1)]
                elif ext in ("jpg", "jpeg", "png", "gif", "pdf"):
                    continue
            display = clean_ws(re.sub(r"[-_+]+", " ", segment), 70)
            label = clean_category_label(display)
            if label:
                return label.title()
        return ""

    def detect(self, parsed: Any, url: str) -> CategoryMatch:
        """Detect page category (breadcrumb -> nav parent -> URL -> fallback)."""
        crumbs = [
            label for label in (
                clean_category_label(c) for c in (parsed.breadcrumbs or [])
            ) if label
        ]
        nav_parent = clean_category_label(parsed.nav_parent)
        nav_global = clean_category_label(self._nav_match(url))

        primary = ""
        extras: list[str] = []
        section = ""
        if crumbs:
            primary = crumbs[0]
            section = crumbs[-1]
            extras.extend(crumbs[1:])
        elif nav_parent:
            primary = nav_parent
            section = nav_parent
        elif nav_global:
            primary = nav_global
            section = nav_global
        else:
            segment = self._segment_category(url)
            primary = segment or "uncategorized"
            section = primary

        for extra in (nav_parent, nav_global):
            if extra:
                extras.append(extra)

        cleaned_extras: list[str] = []
        seen = {primary.lower()}
        for extra in extras:
            key = extra.lower()
            if key and key not in seen:
                seen.add(key)
                cleaned_extras.append(extra)

        return CategoryMatch(
            primary=primary[:1].upper() + primary[1:] if primary else "uncategorized",
            categories=cleaned_extras,
            section=section or primary,
        )


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

@dataclass
class ParsedPage:
    """Everything extracted from one HTML document."""

    title: str = ""
    text: str = ""
    date_time: str = ""
    language: str = "English"
    images: list[tuple[str, str]] = field(default_factory=list)
    pdfs: list[str] = field(default_factory=list)
    audios: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    # URL -> human name taken from a table's Topic column, for file naming.
    download_names: dict[str, str] = field(default_factory=dict)
    interactive: bool = False
    breadcrumbs: list[str] = field(default_factory=list)
    nav_entries: list[tuple[str, str]] = field(default_factory=list)
    nav_parent: str = ""
    # Filled in when a non-English page is translated to English.
    translated: bool = False
    translation_failed: bool = False
    source_language: str = ""
    original_title: str = ""
    original_text: str = ""

    def retain_discovery(self, earlier):
        """Hydration may remove server-rendered links and attachments."""
        links = {link["url"]: link for link in earlier.links}
        links.update((link["url"], link) for link in self.links)
        self.links = list(links.values())
        images = dict(earlier.images)
        images.update(self.images)
        self.images = list(images.items())
        for key in ("pdfs", "documents", "audios", "videos", "nav_entries"):
            setattr(self, key, list(dict.fromkeys(getattr(earlier, key) + getattr(self, key))))
        self.download_names = {**earlier.download_names, **self.download_names}


HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "header", "hr", "li",
    "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot",
    "thead", "tr", "ul",
}
SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "object", "embed", "button", "input", "select", "textarea", "map", "area",
}
BOILERPLATE_SELECTORS = (
    ".breadcrumb", "[class*='breadcrumb']", "[id*='breadcrumb']",
    "[class*='menu']", "[class*='navbar']", "[class*='nav-']",
    "[role='navigation']", "[class*='sidebar']", "[class*='widget']",
    "[class*='cookie']", "[class*='consent']", "[class*='advert']",
    "[class*='ads-']", "[class*='sponsor']", "[class*='social']",
    "[class*='share']", "[class*='related']", "[class*='comments']",
    "#comments", "[class*='newsletter']", "[class*='skip-link']",
    "[class*='pagination']", "[class*='print-link']",
    # Language switchers list every script (हिन्दी, தமிழ், ...).  Removing
    # them keeps that boilerplate out of the text and out of language
    # detection, so an English page is not mistaken for a Hindi one.
    "[class*='language']", "[id*='language']",
    "[class*='lang-']", "[id*='lang-']",
    "[class*='lang_']", "[id*='lang_']",
    "[class*='translat']", "[id*='translat']",
    ".goog-te-gadget", "#google_translate_element",
)


def extract_topic_download_names(soup, page_url: str) -> dict[str, str]:
    """Map PDF/document URLs to the text in the table's ``Topic`` column.

    Many government listing pages show a table like::

        Topic                         Download
        Annual Report 2023            [PDF]

    The scraper normally keeps only the anchor text, which is often just
    "Download" or an icon.  This helper reads the named column instead so the
    saved file can use the real topic name.
    """
    names: dict[str, str] = {}

    def absolute(raw: str) -> str:
        try:
            return urljoin(page_url, str(raw or "").strip())
        except Exception:
            return ""

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        topic_index: int | None = None
        for row in rows[:4]:
            cells = row.find_all(["th", "td"])
            for index, cell in enumerate(cells):
                label = clean_ws(cell.get_text(" ", strip=True), 80)
                label = label.lower().strip(" :")
                if label in (
                    "topic", "topic name", "topics", "topic/title",
                    "विषय", "विषय नाम",
                ):
                    topic_index = index
                    break
            if topic_index is not None:
                break
        if topic_index is None:
            continue

        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) <= topic_index:
                cells = row.find_all(["th", "td"])
            if len(cells) <= topic_index:
                continue
            topic = clean_ws(cells[topic_index].get_text(" ", strip=True), 200)
            if len(topic) < 2:
                continue
            for anchor in row.find_all("a", href=True):
                url = absolute(anchor.get("href"))
                if not url:
                    continue
                names.setdefault(url, topic)
    return names


GENERIC_DOWNLOAD_WORDS = {
    "download", "downloads", "download pdf", "download file", "click here",
    "here", "link", "pdf", "doc", "document", "view", "open", "more",
    "read more", "details", "file", "attach", "attachment", "go", "submit",
}
ICON_WORDS = {"visibility", "visible", "eye", "icon", "fa", "bi"}
TEXT_LABEL_PREFIXES = (
    "published date", "due date", "type/size", "tender id",
    "posted on", "uploaded on", "date",
)


def _clean_display_text(value: Any) -> str:
    """Remove size/type decorations such as '(PDF)|151.35 KB'."""
    text = clean_ws(value, 200).strip()
    text = re.sub(
        r"\s*[|]?\s*\d+(?:[.,]\d+)?\s*(?:KB|MB|GB)\s*$",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*\((?:pdf|doc|document)\)\s*$", "", text, flags=re.IGNORECASE
    )
    return text.strip(" |:-")


def _useful_link_text(links: Any, url: str) -> str:
    """Strip icon/generic words such as 'visibility View' from anchor text."""
    ignored = GENERIC_DOWNLOAD_WORDS | ICON_WORDS
    for link in (links or []):
        if str(link.get("url") or "") != url:
            continue
        words = clean_ws(link.get("text"), 150).split()
        kept = [
            word for word in words
            if word.strip(" .:-").lower() not in ignored
        ]
        text = _clean_display_text(" ".join(kept))
        if len(text) >= 3:
            return text
    return ""


def _opaque_name(value: Any) -> bool:
    """True when a URL filename is an ID/hash and not a human name."""
    text = str(value or "").strip()
    if not text or text.lower() in GENERIC_DOWNLOAD_WORDS:
        return True
    if re.fullmatch(r"[0-9a-fA-F]{12,}", text):
        return True
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        text,
    ):
        return True
    if re.fullmatch(r"\d{8,}", text):
        return True
    if (
        len(text) >= 20
        and " " not in text and "_" not in text and "-" not in text
        and len(re.findall(r"[A-Za-z]", text)) <= 2
    ):
        return True
    return False


def scrape_title_name(text: Any, index: int = 0) -> str:
    """Get the Nth value shown after a 'Title:' line in scraped page text."""
    lines = str(text or "").splitlines()
    titles: list[str] = []
    labels = ("title", "शीर्षक")
    for position, line in enumerate(lines):
        if clean_ws(line, 80).strip().rstrip(":").lower() not in labels:
            continue
        for candidate_line in lines[position + 1: position + 4]:
            candidate = clean_ws(candidate_line, 200).strip()
            if not candidate:
                continue
            low = candidate.lower().rstrip(":")
            if low in GENERIC_DOWNLOAD_WORDS or low in labels:
                break
            if low.startswith(TEXT_LABEL_PREFIXES):
                break
            candidate = _clean_display_text(candidate)
            if candidate:
                titles.append(candidate)
            break
    if 0 <= index < len(titles):
        return titles[index]
    return ""


def fallback_download_name(
    title: Any,
    links: Any,
    url: str,
    kind: str,
    page_id: str = "",
    text: Any = "",
    index: int = 0,
) -> str:
    """Build a meaningful filename when no Topic column is available.

    Order: scraped ``Title:`` value, readable URL filename, useful link text,
    page title, then a safe ``kind_pageid`` last resort.  Hash-like filenames
    such as ``0ad135d10faed...pdf`` are never used as a name.
    """
    scraped = scrape_title_name(text, index)
    if scraped:
        return scraped

    page_title = clean_ws(title, 150).strip()
    if page_title.lower() in GENERIC_DOWNLOAD_WORDS:
        page_title = ""

    original = ""
    try:
        original = Path(unquote(urlsplit(url).path)).name
    except Exception:
        original = ""
    original_stem = clean_ws(Path(original).stem if original else "", 150).strip()
    opaque = _opaque_name(original_stem)

    if original_stem and not opaque:
        if page_title:
            return f"{page_title} - {original_stem}"
        return original_stem

    link_text = _useful_link_text(links, url)
    if link_text:
        return link_text
    if page_title:
        return page_title
    return f"{kind}_{page_id or 'file'}"


class Parser:
    """Extract title, text, dates, links and media from HTML."""

    NAV_SELECTORS = (
        "header nav", "nav[aria-label*='main' i]", "nav[aria-label*='primary' i]",
        "[role='navigation']", "#main-navigation", ".main-navigation",
        "#main-menu", ".main-menu", ".navbar", "#navbar", "nav",
    )

    def __init__(self, site_host: str) -> None:
        self.site_host = site_host
        self.buttons = self.hyperlinks = self.collect_text = True

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _abs(href: Optional[str], base: str) -> Optional[str]:
        if not href:
            return None
        return normalize_url(href, base=base)

    @staticmethod
    def _srcset_best(srcset: str) -> str:
        best_url = ""
        best_score = -1.0
        for part in (srcset or "").split(","):
            bits = part.strip().split()
            if not bits:
                continue
            score = 1.0
            if len(bits) > 1:
                desc = bits[1].strip().lower()
                try:
                    if desc.endswith("w"):
                        score = float(desc[:-1])
                    elif desc.endswith("x"):
                        score = float(desc[:-1]) * 1000.0
                except ValueError:
                    score = 1.0
            if score >= best_score:
                best_score = score
                best_url = bits[0]
        return best_url

    @staticmethod
    def _placeholder_image(url: str) -> bool:
        low = url.lower()
        needles = (
            "blank.gif", "spacer.gif", "1x1.", "pixel.gif", "transparent.gif",
            "placeholder", "lazy-loading", "loading.gif", "default-image",
        )
        return any(n in low for n in needles)

    # -- public -----------------------------------------------------------
    def parse(self, content: bytes | str, page_url: str) -> ParsedPage:
        """Parse one HTML document (never raises for bad markup)."""
        if isinstance(content, bytes):
            soup = BeautifulSoup(content, "lxml")
        else:
            soup = BeautifulSoup(content, "lxml")

        parsed = ParsedPage(interactive=bool(
            (self.buttons and soup.select_one(navigation.CONTROLS)) or
            soup.select_one("script[src],script:not([type]),[loading=lazy],[data-src]")))
        base = soup.find("base", href=True)
        base_url = (self._abs(base["href"], page_url) if base else None) or page_url
        parsed.title = self._extract_title(soup)
        parsed.breadcrumbs = self._extract_breadcrumbs(soup, parsed.title)
        parsed.nav_entries = self._extract_nav_entries(soup, base_url)
        parsed.nav_parent = self._extract_nav_parent(soup, page_url)
        parsed.download_names = extract_topic_download_names(soup, base_url)
        self._extract_links_and_media(soup, base_url, parsed)
        parsed.date_time = self._extract_date(soup, page_url)
        parsed.language = self._extract_language(soup)
        parsed.text = self._extract_text(soup)
        # Check the visible text on every page, not just short ones.  A page
        # can declare lang="en" (or omit the attribute) while the body is in
        # Hindi, so the script ratio is the more reliable signal.
        parsed.language = self._detect_language_from_text(
            f"{parsed.title}\n{parsed.text}", parsed.language
        )
        return parsed

    # -- title ------------------------------------------------------------
    def _extract_title(self, soup: BeautifulSoup) -> str:
        for selector in (
            "meta[property='og:title']", "meta[name='twitter:title']",
            "meta[name='title']", "meta[itemprop='name']",
        ):
            node = soup.select_one(selector)
            if node and clean_ws(node.get("content")):
                return clean_ws(node.get("content"), 500)
        h1 = soup.find("h1")
        if h1:
            text = clean_ws(h1.get_text(" ", strip=True), 500)
            if text:
                return text
        if soup.title and clean_ws(soup.title.get_text()):
            return clean_ws(soup.title.get_text(), 500)
        return ""

    # -- breadcrumbs ------------------------------------------------------
    def _extract_breadcrumbs(self, soup: BeautifulSoup, page_title: str) -> list[str]:
        selectors = (
            "nav[aria-label*='breadcrumb' i]", "[class*='breadcrumb']",
            "[id*='breadcrumb']", "[itemtype*='BreadcrumbList']",
            "ol.breadcrumb", "[role='navigation'][aria-label*='you are here' i]",
        )
        for selector in selectors:
            for node in soup.select(selector):
                texts: list[str] = []
                anchors = node.find_all("a")
                if anchors:
                    for anchor in anchors:
                        value = clean_ws(
                            anchor.get_text(" ", strip=True)
                            or anchor.get("title", "")
                            or anchor.get("aria-label", ""),
                            80,
                        )
                        if value:
                            texts.append(value)
                leaf_lis = [li for li in node.find_all("li") if not li.find("li")]
                if leaf_lis:
                    li_texts = [
                        clean_ws(li.get_text(" ", strip=True), 80) for li in leaf_lis
                    ]
                    li_texts = [t for t in li_texts if t]
                    if len(li_texts) >= len(texts):
                        texts = li_texts
                if not texts:
                    raw = clean_ws(node.get_text(" ", strip=True), 400)
                    if raw:
                        texts = re.split(r"\s*(?:>|»|›|/|\||→|::|•)\s*", raw)

                cleaned: list[str] = []
                for text in texts:
                    for piece in re.split(r"\s*(?:>|»|›|\||→|::)\s*", text or ""):
                        piece = clean_ws(piece, 80).strip(" .›»>|-–—")
                        low = piece.lower()
                        if not piece or low in HOME_WORDS:
                            continue
                        if low in ("you are here", "skip to main content", "menu"):
                            continue
                        if len(piece) < 2:
                            continue
                        if not cleaned or cleaned[-1].lower() != low:
                            cleaned.append(piece)
                if len(cleaned) > 1 and page_title:
                    last = re.sub(r"\W+", "", cleaned[-1].lower())
                    title = re.sub(r"\W+", "", page_title.lower())
                    if last and (
                        last == title or (len(last) > 15 and title.startswith(last))
                    ):
                        cleaned.pop()
                if cleaned:
                    return cleaned[:8]
        return []

    # -- navigation -------------------------------------------------------
    def _extract_nav_entries(
        self, soup: BeautifulSoup, page_url: str
    ) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        selectors = (*self.NAV_SELECTORS, "header", "[role=menu]", "[role=menubar]")
        groups = [(menu, "") for menu in soup.select(",".join(selectors))]
        # Directory pages may use a heading and list for each section rather
        # than a navigation landmark. Use only actual grouped destinations.
        for listing in soup.select("section > ul, section > ol, [role=region] > ul"):
            heading = listing.parent.find(re.compile("^h[1-6]$"), recursive=False)
            label = listing.parent.get("aria-label") or (heading.get_text(" ", strip=True) if heading else "")
            if label:
                groups.append((listing, label))
        for container, group_label in groups:
            for raw, label in navigation.discover(container, self.hyperlinks, self.buttons):
                display = clean_ws(group_label or label, 70)
                if len(display) < 2 or display.lower() in HOME_WORDS:
                    continue
                href = self._abs(raw, page_url)
                if not href or not is_internal(href, self.site_host):
                    continue
                parts = urlsplit(href)
                path = parts.path.rstrip("/") or "/"
                if parts.query:
                    path += "?" + parts.query
                if parts.fragment:
                    path += "#" + parts.fragment
                if path == "/" or path in seen or len(path) < 2:
                    continue
                seen.add(path)
                entries.append((path, display))
        return entries

    def _extract_nav_parent(self, soup: BeautifulSoup, page_url: str) -> str:
        target = normalize_url(page_url)
        if not target:
            return ""
        target_path = urlsplit(target).path.rstrip("/") or "/"
        for anchor in soup.select(
            "nav a[href], header a[href], [role='navigation'] a[href], .navbar a[href]"
        ):
            href = self._abs(anchor.get("href"), page_url)
            if not href:
                continue
            path = urlsplit(href).path.rstrip("/") or "/"
            if href != target and path != target_path:
                continue
            li = anchor.find_parent("li")
            if li is None:
                continue
            top = li
            while top.find_parent("li") is not None:
                top = top.find_parent("li")
            top_anchor = top.find("a", href=True)
            text = clean_ws(
                (top_anchor.get_text(" ", strip=True) if top_anchor else "")
                or top.get_text(" ", strip=True),
                70,
            )
            if len(text) >= 2 and text.lower() not in HOME_WORDS:
                return text
        return ""

    # -- links & media ----------------------------------------------------
    def _extract_links_and_media(
        self, soup: BeautifulSoup, page_url: str, parsed: ParsedPage
    ) -> None:
        image_seen: set[str] = set()
        pdf_seen: set[str] = set()
        audio_seen: set[str] = set()
        video_seen: set[str] = set()
        doc_seen: set[str] = set()
        link_seen: set[str] = set()
        alt_by_url: dict[str, str] = {}

        def add_image(url: str, alt: str = "") -> None:
            if len(image_seen) >= MAX_IMAGES_PER_PAGE or url in image_seen:
                return
            image_seen.add(url)
            alt_by_url.setdefault(url, clean_ws(alt, 300))
            parsed.images.append((url, alt_by_url[url]))

        def add_pdf(url: str) -> None:
            if url not in pdf_seen:
                pdf_seen.add(url)
                parsed.pdfs.append(url)

        def add_audio(url: str) -> None:
            if url not in audio_seen:
                audio_seen.add(url)
                parsed.audios.append(url)

        def add_video(url: str) -> None:
            if url not in video_seen:
                video_seen.add(url)
                parsed.videos.append(url)

        def add_doc(url: str) -> None:
            if url not in doc_seen:
                doc_seen.add(url)
                parsed.documents.append(url)

        def add_by_kind(url: str, hint: str = "", alt: str = "") -> None:
            kind = classify_media(url) or hint
            if kind == "image":
                add_image(url, alt)
            elif kind == "pdf":
                add_pdf(url)
            elif kind == "audio":
                add_audio(url)
            elif kind == "video":
                add_video(url)
            elif kind == "document":
                add_doc(url)

        # <img> + lazy loading attributes + srcset
        for img in soup.find_all("img"):
            alt = clean_ws(img.get("alt") or img.get("title") or "", 300)
            candidates: list[str] = []
            for attr in (
                "data-src", "data-original", "data-lazy-src", "data-echo",
                "data-url", "data-image", "src",
            ):
                value = img.get(attr)
                if value and not str(value).startswith("data:"):
                    candidates.append(str(value))
            for attr in ("data-srcset", "srcset"):
                value = img.get(attr)
                if value:
                    best = self._srcset_best(str(value))
                    if best:
                        candidates.append(best)
            for candidate in candidates:
                absolute = self._abs(candidate, page_url)
                if absolute and not self._placeholder_image(absolute):
                    add_image(absolute, alt)
                    break

        for source in soup.select("picture source[srcset], picture source[data-srcset]"):
            srcset = source.get("srcset") or source.get("data-srcset") or ""
            absolute = self._abs(self._srcset_best(str(srcset)), page_url)
            if absolute and not self._placeholder_image(absolute):
                parent_img = source.find_parent("picture")
                alt = clean_ws(
                    (parent_img.find("img").get("alt") if parent_img and parent_img.find("img") else ""),
                    300,
                )
                add_image(absolute, alt)

        # <video>/<audio>/<source>/<embed>/<object>/poster/iframes
        for tag in soup.find_all(["video", "audio", "source", "embed", "object", "iframe"]):
            attrs = ("src", "data-src") if tag.name != "object" else ("data", "src")
            for attr in attrs:
                value = tag.get(attr)
                absolute = self._abs(value, page_url) if value else None
                if not absolute:
                    continue
                if tag.name == "iframe":
                    if is_video_embed(absolute):
                        add_video(absolute)
                    else:
                        add_by_kind(absolute)
                    break
                if tag.name == "source":
                    parent = tag.find_parent(["video", "audio"])
                    if parent is not None and parent.name == "video":
                        add_by_kind(absolute, hint="video")
                    elif parent is not None:
                        add_by_kind(absolute, hint="audio")
                    else:
                        add_by_kind(absolute)
                elif tag.name == "video":
                    add_by_kind(absolute, hint="video")
                elif tag.name == "audio":
                    add_by_kind(absolute, hint="audio")
                else:
                    add_by_kind(absolute)
                break
            poster = tag.get("poster") if tag.name == "video" else None
            if poster:
                absolute = self._abs(poster, page_url)
                if absolute:
                    add_image(absolute, "video poster")

        # Media selection is independent of following ordinary hyperlinks.
        for anchor in soup.select("a[href]"):
            absolute = self._abs(anchor["href"], page_url)
            if absolute:
                add_by_kind(absolute, alt=anchor.get_text(" ", strip=True))
        for raw, label in navigation.discover(soup, self.hyperlinks, self.buttons):
            absolute = self._abs(raw, page_url)
            if not absolute:
                continue
            text = clean_ws(label, 300)
            if absolute not in link_seen:
                link_seen.add(absolute)
                parsed.links.append(
                    {
                        "text": text,
                        "url": absolute,
                        "type": "internal" if is_internal(absolute, self.site_host) else "external",
                    }
                )
            add_by_kind(absolute, alt=text)

        parsed.images.sort(key=lambda item: item[0])
        parsed.pdfs = sorted(pdf_seen)
        parsed.audios = sorted(audio_seen)
        parsed.videos = sorted(video_seen)
        parsed.documents = sorted(doc_seen)

    # -- date -------------------------------------------------------------
    def _extract_date(self, soup: BeautifulSoup, page_url: str) -> str:
        candidates: list[str] = []
        meta_pairs = (
            ("property", "article:published_time"),
            ("property", "article:modified_time"),
            ("property", "og:updated_time"),
            ("name", "date"),
            ("name", "pubdate"),
            ("name", "publish-date"),
            ("name", "publish_date"),
            ("name", "dc.date"),
            ("name", "dcterms.date"),
            ("name", "dcterms.created"),
            ("name", "last-modified"),
            ("name", "revised"),
            ("itemprop", "datePublished"),
            ("itemprop", "dateModified"),
            ("http-equiv", "last-modified"),
        )
        for meta in soup.find_all("meta"):
            for attr, key in meta_pairs:
                if (meta.get(attr) or "").strip().lower() == key.lower():
                    value = meta.get("content")
                    if value:
                        candidates.append(str(value))

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or script.get_text() or "{}")
            except Exception:
                continue

            def walk(node: Any) -> None:
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key in (
                            "datePublished", "dateModified", "dateCreated",
                            "uploadDate", "datePosted",
                        ) and isinstance(value, str):
                            candidates.append(value)
                        else:
                            walk(value)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(data)

        for time_tag in soup.find_all("time"):
            value = time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
            if value:
                candidates.append(str(value))

        try:
            path = urlsplit(page_url).path
            match = re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)", path)
            if match:
                candidates.append(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
        except Exception:
            pass

        for candidate in candidates:
            parsed = parse_date_value(candidate)
            if parsed is not None:
                return parsed.strftime("%Y-%m-%d %H:%M")

        sample = clean_ws(soup.get_text(" ", strip=True)[:6000])
        date_pattern = re.compile(
            r"(?:last\s+updated|updated|published|posted|date)\s*(?:on|:)?\s*"
            r"([0-9]{1,2}[\s./-](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s./-][0-9]{2,4}"
            r"|[0-9]{4}-[0-9]{2}-[0-9]{2}"
            r"|[0-9]{1,2}[/.-][0-9]{1,2}[/.-][0-9]{2,4})",
            re.IGNORECASE,
        )
        match = date_pattern.search(sample)
        if match:
            parsed = parse_date_value(match.group(1))
            if parsed is not None:
                return parsed.strftime("%Y-%m-%d %H:%M")
        return ""

    # -- language ---------------------------------------------------------
    def _extract_language(self, soup: BeautifulSoup) -> str:
        raw = ""
        if soup.html and soup.html.get("lang"):
            raw = str(soup.html.get("lang"))
        if not raw:
            for meta in soup.find_all("meta"):
                name = (meta.get("name") or "").lower()
                equiv = (meta.get("http-equiv") or "").lower()
                if name in ("language", "dc.language", "dcterms.language") or equiv == "content-language":
                    raw = str(meta.get("content") or "")
                    if raw:
                        break
        if raw:
            primary = re.split(r"[-_,;]", raw.strip().lower())[0]
            if primary in LANG_NAMES:
                return LANG_NAMES[primary]
            if primary:
                return raw.strip().split("-")[0].split(",")[0].title()
        return "English"

    @staticmethod
    def _detect_language_from_text(text: str, fallback: str) -> str:
        sample = (text or "")[:120000]
        if not sample.strip():
            return fallback or "English"
        latin = len(LATIN_LETTER_RE.findall(sample))
        for pattern, name in SCRIPT_LANG:
            count = len(re.findall(pattern, sample))
            if count >= 40 and count / max(1, count + latin) >= 0.35:
                return name
        # No non-Latin script dominates the visible text, so the page reads as
        # English even when <html lang="hi"> or a stale meta tag says
        # otherwise (common on /hi/ URLs that serve the English table).
        if latin >= 20:
            return "English"
        return fallback or "English"

    # -- main text --------------------------------------------------------
    def _extract_text(self, soup: BeautifulSoup) -> str:
        for tag in soup.find_all(
            ["script", "style", "noscript", "template", "svg", "canvas"]
        ):
            tag.decompose()
        for tag in soup.find_all(["header", "footer", "nav", "aside"]):
            tag.decompose()

        main: Optional[Tag] = None
        best: Optional[Tag] = None
        best_len = 0
        body = soup.body or soup
        for selector in (
            "main", "[role='main']", "#main-content", "#content", "#main",
            "article", ".main-content", ".content-area", "#content-area",
            "div.content",
        ):
            for node in soup.select(selector):
                text_len = len(node.get_text(" ", strip=True))
                if text_len > best_len:
                    best, best_len = node, text_len
                if main is None and text_len >= 150:
                    main = node
            if main is not None:
                break
        if main is None:
            main = best if best is not None else body

        main_len = len(main.get_text(" ", strip=True))
        for selector in BOILERPLATE_SELECTORS:
            for node in main.select(selector):
                if node is main or node.name in ("main", "article", "body"):
                    continue
                node_len = len(node.get_text(" ", strip=True))
                if main_len > 1500 and node_len > 0.6 * main_len:
                    continue
                node.decompose()

        text = self._render_text(main)
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n\n[text truncated]"
        return text

    def _render_text(self, root: Tag) -> str:
        chunks: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, Comment):
                return
            if isinstance(node, NavigableString):
                chunks.append(str(node))
                return
            if not isinstance(node, Tag):
                return
            name = node.name.lower()
            if name in SKIP_TAGS:
                return
            if node.has_attr("hidden"):
                return
            style = (node.get("style") or "").replace(" ", "").lower()
            if "display:none" in style or "visibility:hidden" in style:
                return
            if name == "br":
                chunks.append("\n")
                return
            if name in HEADING_TAGS:
                chunks.append("\n")
                chunks.append("#" * int(name[1]) + " ")
                for child in node.children:
                    walk(child)
                chunks.append("\n")
                return
            if name in ("td", "th"):
                for child in node.children:
                    walk(child)
                chunks.append(" | ")
                return
            if name in BLOCK_TAGS:
                chunks.append("\n")
                for child in node.children:
                    walk(child)
                chunks.append("\n")
                return
            for child in node.children:
                walk(child)

        walk(root)
        raw = "".join(chunks).replace("\xa0", " ").replace("\u200b", "")
        lines: list[str] = []
        for line in raw.splitlines():
            line = re.sub(r"[ \t\f\v]+", " ", line).strip()
            line = re.sub(r"(?:\s*\|\s*)+$", "", line).strip()
            line = re.sub(r"^(?:\|\s*)+", "", line).strip()
            if line and line not in ("|", "-", "--"):
                lines.append(line)
        output: list[str] = []
        for line in lines:
            if line.startswith("#"):
                if output and output[-1] != "":
                    output.append("")
                output.append(line)
            else:
                output.append(line)
        return "\n".join(output).strip()


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    """A successfully downloaded media file."""

    kind: str
    relative_path: str
    size: int
    text_relative_path: Optional[str] = None
    replaced: bool = False


CTYPE_EXT_FIX = {
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}

DOCUMENT_CTYPES = (
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument",
    "application/rtf",
    "text/csv",
    "application/epub+zip",
    "application/zip",
)


class Downloader:
    """Downloads media into category/page folders and extracts PDF text."""

    def __init__(
        self,
        http: HttpClient,
        site: SiteFolderManager,
        categories: CategoryManager,
        failures: FailureLog,
        stop_event: threading.Event,
        max_file_bytes: int = 250 * 1024 * 1024,
        youtube_enabled: bool = False,
        pdf_text: bool = True,
    ) -> None:
        self.http = http
        self.site = site
        self.categories = categories
        self.failures = failures
        self.stop_event = stop_event
        self.max_file_bytes = max_file_bytes
        self.youtube_enabled = youtube_enabled
        # When False, PDFs are downloaded without extracting a .txt sidecar.
        self.pdf_text = bool(pdf_text)
        self.cache_file = site.data_dir / "download_index.json"
        self.cache_lock = threading.Lock()
        self.url_locks = [threading.Lock() for _ in range(64)]
        try:
            self.cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self.owners = {v["path"].casefold(): u for u, v in self.cache.items()}
        self.checked = set()

    def save_index(self):
        with self.cache_lock:
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.cache_file)

    @staticmethod
    def _ext_from_ctype(content_type: str) -> str:
        ctype = content_type.split(";")[0].strip().lower()
        if ctype in CTYPE_EXT_FIX:
            return CTYPE_EXT_FIX[ctype]
        guessed = mimetypes.guess_extension(ctype) if ctype else None
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
        return ""

    @staticmethod
    def _content_type_ok(kind: str, ctype: str) -> bool:
        ctype = (ctype or "").split(";")[0].strip().lower()
        if not ctype or ctype in ("application/octet-stream", "binary/octet-stream"):
            return True
        if ctype.startswith("text/html") or ctype.startswith("application/xhtml"):
            return False
        if kind == "image":
            return ctype.startswith("image/")
        if kind == "pdf":
            return ctype in ("application/pdf", "application/x-pdf") or ctype.startswith("application/pdf")
        if kind == "audio":
            return ctype.startswith("audio/") or ctype == "application/ogg"
        if kind == "video":
            return ctype.startswith("video/") or ctype in ("application/mp4", "application/ogg")
        if kind == "document":
            return ctype.startswith("text/") or any(ctype.startswith(c) for c in DOCUMENT_CTYPES)
        return True

    def _existing_result(self, path: Path, kind: str) -> Optional[DownloadResult]:
        if not path.exists() or path.stat().st_size <= 0:
            return None
        text_rel = None
        if kind == "pdf" and self.pdf_text:
            txt = path.with_name(path.stem + ".txt")
            if txt.exists():
                text_rel = self.site.rel(txt)
        return DownloadResult(
            kind=kind,
            relative_path=self.site.rel(path),
            size=path.stat().st_size,
            text_relative_path=text_rel,
        )

    def download(self, url, category, page_id, kind, referrer="", force=False, display_name=""):
        with self.url_locks[hash(url) % len(self.url_locks)]:
            return self._download(url, category, page_id, kind, referrer, force, display_name)

    def _download(
        self,
        url: str,
        category: CategoryInfo,
        page_id: str,
        kind: str,
        referrer: str = "",
        force: bool = False,
        display_name: str = "",
    ) -> Optional[DownloadResult]:
        """Download one URL into its category/page folder (skip if present).

        With *force* an existing file is re-downloaded and replaced only when
        its bytes differ, which is how ``--refresh-media`` stays cheap.  When
        *display_name* is supplied (the Topic-column text), it becomes the
        saved filename instead of the URL-based hashed name.
        """
        if self.stop_event.is_set():
            return None
        if kind == "video" and is_video_embed(url):
            return self._download_youtube(url, category, page_id)

        directory = self.site.media_dir(category.folder, kind, page_id)
        directory.mkdir(parents=True, exist_ok=True)
        tentative_ext = url_suffix(url) or KIND_DEFAULT_EXT.get(kind, ".bin")
        tentative = media_filename(
            url, directory, kind, tentative_ext, display_name=display_name
        )
        with self.cache_lock:
            entry = self.cache.get(url, {})
        cached = self.site.site_dir / entry["path"] if entry else None
        existing = self._existing_result(cached, kind) if cached else None
        if existing and (not force or url in self.checked):
            return existing
        conditional = {}
        if existing:
            conditional = entry.get("validators", {})
        response = self.http.get(url, stream=True, referer=referrer, validators=conditional)
        if response is None:
            return existing
        tmp_path: Optional[Path] = None
        try:
            if response.status_code == 304 and existing:
                self.checked.add(url)
                return existing
            ctype = response.headers.get("Content-Type", "")
            if not self._content_type_ok(kind, ctype):
                self.failures.add(url, f"unexpected content-type: {ctype or 'unknown'}")
                return None
            length = int(response.headers.get("Content-Length") or 0)
            if length and length > self.max_file_bytes:
                self.failures.add(url, f"too large: {length} bytes")
                return None

            ext = url_suffix(url) or self._ext_from_ctype(ctype) or KIND_DEFAULT_EXT.get(kind, ".bin")
            path = media_filename(
                url, directory, kind, ext, display_name=display_name
            )
            if existing:
                path = cached
            else:
                with self.cache_lock:
                    number = 0
                    base_path = path
                    while (self.owners.get(self.site.rel(path).casefold(), url) != url or
                           (path.exists() and self.site.rel(path).casefold() not in self.owners)):
                        number += 1
                        suffix = f"-{number}{base_path.suffix}"
                        room = max(1, MAX_PATH_LEN - len(str(directory)) - 1 - len(suffix))
                        path = directory / (base_path.stem[:room] + suffix)
                    self.owners[self.site.rel(path).casefold()] = url

            tmp_path = path.with_name(path.name + ".part")
            written = 0
            with open(tmp_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if self.stop_event.is_set():
                        raise RuntimeError("crawl interrupted")
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > self.max_file_bytes:
                        raise RuntimeError("file exceeds size limit")
                    fh.write(chunk)
            if written <= 0:
                self.failures.add(url, "empty response body")
                return None

            had_existing = path.exists()
            validators = {header: response.headers[name] for name, header in
                          (("ETag", "If-None-Match"), ("Last-Modified", "If-Modified-Since"))
                          if response.headers.get(name)}
            if had_existing and files_identical(path, tmp_path):
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                tmp_path = None
                logger.debug("Refreshed media is unchanged, kept: %s", path)
                with self.cache_lock:
                    self.cache[url] = {"path": self.site.rel(path), "validators": validators}
                    self.checked.add(url)
                return self._existing_result(path, kind)

            os.replace(tmp_path, path)
            tmp_path = None
            with self.cache_lock:
                self.cache[url] = {"path": self.site.rel(path), "validators": validators}
                self.checked.add(url)

            text_rel = None
            if kind == "pdf" and self.pdf_text:
                txt = self._extract_pdf_text(path)
                if txt is not None:
                    text_rel = self.site.rel(txt)
            self.categories.increment_media(category.folder, kind)
            logger.debug("Saved %s (%d bytes) -> %s", kind, written, path)
            return DownloadResult(
                kind=kind,
                relative_path=self.site.rel(path),
                size=written,
                text_relative_path=text_rel,
                replaced=bool(force and had_existing),
            )
        except Exception as exc:
            self.failures.add(url, f"download failed: {type(exc).__name__}: {exc}")
            logger.warning("Download failed for %s: %s", url, exc)
            return None
        finally:
            try:
                response.close()
            except Exception:
                pass
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    def _extract_pdf_text(self, pdf_path: Path) -> Optional[Path]:
        """Extract PDF text with pdfplumber (fallback PyMuPDF) -> .txt file."""
        text = ""
        if pdfplumber is not None:
            try:
                with pdfplumber.open(str(pdf_path)) as pdf:
                    for page in pdf.pages[:250]:
                        text += (page.extract_text() or "") + "\n"
            except Exception as exc:
                logger.debug("pdfplumber failed for %s: %s", pdf_path.name, exc)
                text = ""
        if not text.strip() and fitz is not None:
            try:
                with fitz.open(str(pdf_path)) as doc:
                    for page in list(doc)[:250]:
                        text += page.get_text() + "\n"
            except Exception as exc:
                logger.debug("PyMuPDF failed for %s: %s", pdf_path.name, exc)
        if not text.strip():
            logger.debug("No extractable text in PDF %s", pdf_path.name)
            return None
        txt_path = pdf_path.with_name(pdf_path.stem + ".txt")
        try:
            txt_path.write_text(text[:PDF_TEXT_LIMIT], encoding="utf-8")
            return txt_path
        except Exception as exc:
            logger.debug("Could not write PDF text for %s: %s", pdf_path.name, exc)
            return None

    def _download_youtube(
        self, url: str, category: CategoryInfo, page_id: str
    ) -> Optional[DownloadResult]:
        """Optional YouTube/Vimeo download via yt-dlp."""
        if not self.youtube_enabled:
            logger.info("Recorded video link (use --download-youtube to fetch): %s", url)
            return None
        if yt_dlp is None:
            self.failures.add(url, "yt-dlp is not installed")
            return None
        directory = self.site.media_dir(category.folder, "video", page_id)
        directory.mkdir(parents=True, exist_ok=True)
        options = {
            "outtmpl": str(directory / "%(id)s.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "retries": 2,
            "socket_timeout": 30,
            "max_filesize": self.max_file_bytes,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    return None
                video_id = info.get("id") or ""
            matches = list(directory.glob(f"{video_id}.*")) if video_id else []
            matches = [m for m in matches if m.is_file() and not m.name.endswith(".part")]
            if not matches:
                return None
            path = max(matches, key=lambda m: m.stat().st_size)
            self.categories.increment_media(category.folder, "video")
            return DownloadResult(
                kind="video", relative_path=self.site.rel(path), size=path.stat().st_size
            )
        except Exception as exc:
            self.failures.add(url, f"yt-dlp failed: {type(exc).__name__}: {exc}")
            logger.info("Could not download video %s: %s", url, exc)
            return None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class Storage:
    """Thread-safe append-only JSONL writer, one file per category."""

    def __init__(self, site: SiteFolderManager, categories: CategoryManager) -> None:
        self.site = site
        self.categories = categories
        self._meta_lock = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}
        self._handles: dict[str, Any] = {}
        self._seen: dict[str, set[str]] = {}

    def _lock_for(self, folder: str) -> threading.Lock:
        with self._meta_lock:
            if folder not in self._locks:
                self._locks[folder] = threading.Lock()
            return self._locks[folder]

    def _paths(self, folder: str) -> tuple[Path, Path]:
        cat = self.categories.get(folder)
        path = cat.path if cat else self.site.category_dir(folder)
        path.mkdir(parents=True, exist_ok=True)
        return path / "pages.jsonl", path

    def _load_seen(self, folder: str, pages_file: Path) -> set[str]:
        seen: set[str] = set()
        if pages_file.exists():
            try:
                with open(pages_file, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            seen.add(json.loads(line).get("prid", ""))
                        except Exception:
                            continue
            except Exception as exc:
                logger.warning("Could not read existing %s: %s", pages_file, exc)
        seen.discard("")
        return seen

    def _ensure_seen(self, folder: str, pages_file: Path) -> set[str]:
        if folder not in self._seen:
            self._seen[folder] = self._load_seen(folder, pages_file)
        return self._seen[folder]

    def _close_handle(self, folder: str) -> None:
        handle = self._handles.pop(folder, None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _append_locked(
        self, record: dict[str, Any], folder: str, pages_file: Path, seen: set[str]
    ) -> bool:
        """Write one JSONL row; the caller already holds the folder lock."""
        handle = self._handles.get(folder)
        if handle is None:
            handle = open(pages_file, "a", encoding="utf-8", buffering=1)
            self._handles[folder] = handle
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        seen.add(str(record.get("prid") or ""))
        self.categories.increment_pages(folder)
        return True

    def append(self, record: dict[str, Any], folder: str) -> bool:
        """Append a record unless its prid already exists in that category."""
        pages_file, _ = self._paths(folder)
        prid = str(record.get("prid") or "")
        if not prid:
            return False
        with self._lock_for(folder):
            seen = self._ensure_seen(folder, pages_file)
            if prid in seen:
                return False
            return self._append_locked(record, folder, pages_file, seen)

    def upsert(self, record: dict[str, Any], folder: str) -> str:
        """Insert *record* or replace the row with the same ``prid`` in place.

        Returns ``"new"``, ``"updated"`` or ``"skipped"``.  Unlike ``append``
        this rewrites the row, which is what ``--refresh-existing`` needs so a
        changed page is not silently dropped as a duplicate.
        """
        pages_file, _ = self._paths(folder)
        prid = str(record.get("prid") or "")
        if not prid:
            return "skipped"
        with self._lock_for(folder):
            seen = self._ensure_seen(folder, pages_file)
            if prid not in seen:
                self._append_locked(record, folder, pages_file, seen)
                return "new"
            if self._replace_locked(pages_file, folder, record, prid):
                return "updated"
            return "skipped"

    def _replace_locked(
        self, pages_file: Path, folder: str, record: dict[str, Any], prid: str
    ) -> bool:
        """Rewrite a category file with one row replaced (atomic)."""
        self._close_handle(folder)
        tmp_path = pages_file.with_name(pages_file.name + ".tmp")
        replaced = False
        try:
            with open(pages_file, "r", encoding="utf-8") as src, open(
                tmp_path, "w", encoding="utf-8", newline="\n"
            ) as dst:
                for line in src:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if not replaced:
                        try:
                            previous = json.loads(stripped)
                            if previous.get("prid") == prid:
                                dst.write(json.dumps({**previous, **record}, ensure_ascii=False) + "\n")
                                replaced = True
                                continue
                        except Exception:
                            pass
                    dst.write(stripped + "\n")
            if not replaced:
                tmp_path.unlink(missing_ok=True)
                return False
            os.replace(tmp_path, pages_file)
            return True
        except OSError as exc:
            logger.warning("Could not rewrite %s: %s", pages_file, exc)
            tmp_path.unlink(missing_ok=True)
            return False

    def remove(self, prid: str, folder: str) -> bool:
        """Delete one row from a category file (used when a page moves)."""
        prid = str(prid or "")
        if not prid:
            return False
        pages_file, _ = self._paths(folder)
        with self._lock_for(folder):
            seen = self._ensure_seen(folder, pages_file)
            if prid not in seen:
                return False
            self._close_handle(folder)
            tmp_path = pages_file.with_name(pages_file.name + ".tmp")
            removed = False
            try:
                with open(pages_file, "r", encoding="utf-8") as src, open(
                    tmp_path, "w", encoding="utf-8", newline="\n"
                ) as dst:
                    for line in src:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        if not removed:
                            try:
                                if json.loads(stripped).get("prid") == prid:
                                    removed = True
                                    continue
                            except Exception:
                                pass
                        dst.write(stripped + "\n")
                if not removed:
                    tmp_path.unlink(missing_ok=True)
                    return False
                os.replace(tmp_path, pages_file)
                seen.discard(prid)
                return True
            except OSError as exc:
                logger.warning("Could not rewrite %s: %s", pages_file, exc)
                tmp_path.unlink(missing_ok=True)
                return False

    def close(self) -> None:
        with self._meta_lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self._handles.clear()


# ---------------------------------------------------------------------------
# Incremental scrape index (what has already been scraped)
# ---------------------------------------------------------------------------

INDEX_FILENAME = "scraped_index.json"


class ScrapedIndex:
    """Remembers every page that was already scraped so reruns can skip it.

    The small JSON file (``data/scraped_index.json``) is the fast skip list.
    ``data/scraped_pages.xlsx`` / ``.csv`` are human-friendly exports that are
    rebuilt from the category JSONL files at the end of every run.

    On first use the JSON is rebuilt from ``categories/*/pages.jsonl`` so an
    existing scraped folder becomes incremental immediately.
    """

    def __init__(self, site: SiteFolderManager) -> None:
        self.site = site
        self.path = site.data_dir / INDEX_FILENAME
        self._lock = threading.RLock()
        self._pages: dict[str, str] = {}

    # -- loading ----------------------------------------------------------
    def load(self) -> int:
        """Load the skip list (rebuilding it from JSONL when missing)."""
        started = time.monotonic()
        pages: dict[str, str] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                raw_pages = payload.get("pages") or {}
                if isinstance(raw_pages, dict):
                    pages = {
                        str(url): str(prid)
                        for url, prid in raw_pages.items()
                        if url and prid
                    }
                else:
                    pages = {}
            except Exception as exc:
                logger.warning("Ignoring unreadable %s: %s", self.path.name, exc)
                pages = {}

        if not pages:
            pages = self._scan_jsonl()
            if pages:
                self._pages = pages
                self.save()

        with self._lock:
            self._pages = pages
        if pages:
            logger.info(
                "Scrape index loaded: %d page(s) already scraped (%.2fs).",
                len(pages), time.monotonic() - started,
            )
        return len(pages)

    def _scan_jsonl(self) -> dict[str, str]:
        """Rebuild the URL -> prid map from the category JSONL files."""
        pages: dict[str, str] = {}
        for pages_file in sorted(self.site.categories_dir.glob("*/pages.jsonl")):
            try:
                with open(pages_file, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        url = str(record.get("source_url") or "")
                        prid = str(record.get("prid") or "")
                        if url and prid:
                            pages[url] = prid
            except OSError as exc:
                logger.warning("Could not scan %s: %s", pages_file, exc)
        return pages

    # -- queries ----------------------------------------------------------
    def count(self) -> int:
        with self._lock:
            return len(self._pages)

    def known_urls(self) -> set[str]:
        """Return a copy of every URL already scraped."""
        with self._lock:
            return set(self._pages)

    def is_known(self, url: str) -> bool:
        """True when *url* (or its normalized form) has already been scraped."""
        if not url:
            return False
        with self._lock:
            if url in self._pages:
                return True
        normalized = normalize_url(url)
        if normalized and normalized != url:
            with self._lock:
                return normalized in self._pages
        return False

    # -- updates ----------------------------------------------------------
    def add(self, record: dict[str, Any]) -> bool:
        """Register a scraped page. Returns True when it was not known yet."""
        url = str(record.get("source_url") or "")
        prid = str(record.get("prid") or "")
        if not url or not prid:
            return False
        with self._lock:
            if self._pages.get(url) == prid:
                return False
            self._pages[url] = prid
            return True

    def forget(self, urls: Iterable[str]) -> int:
        """Drop URLs from the skip list (used when their record is replaced)."""
        removed = 0
        with self._lock:
            for url in urls:
                if url and self._pages.pop(str(url), None) is not None:
                    removed += 1
        return removed

    def save(self) -> None:
        """Atomically persist the skip list."""
        with self._lock:
            payload = {
                "version": VERSION,
                "site_name": self.site.site_name,
                "start_url": self.site.start_url,
                "count": len(self._pages),
                "updated_at": utc_now_iso(),
                "pages": dict(self._pages),
            }
        atomic_write_json(self.path, payload)

    # -- historical links ------------------------------------------------
    def iter_history_links(
        self, include_external: bool = False
    ) -> Iterable[tuple[str, str]]:
        """Yield ``(source_url, link_url)`` pairs from already-scraped pages.

        Internal links are always yielded.  When *include_external* is true,
        links to other websites are yielded too so an incremental run can
        discover new off-site pages without refetching the source page.
        """
        for pages_file in sorted(self.site.categories_dir.glob("*/pages.jsonl")):
            try:
                with open(pages_file, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        links = record.get("links")
                        if not isinstance(links, list):
                            continue
                        source = str(record.get("source_url") or "")
                        for link in links:
                            if not isinstance(link, dict):
                                continue
                            kind = link.get("type")
                            if kind != "internal" and not (
                                include_external and kind == "external"
                            ):
                                continue
                            url = link.get("url")
                            if url:
                                yield source, str(url)
            except OSError as exc:
                logger.warning("Could not read links from %s: %s", pages_file, exc)


# ---------------------------------------------------------------------------
# Refresh bookkeeping (--refresh-existing)
# ---------------------------------------------------------------------------

REFRESH_INDEX_FILENAME = "refresh_index.json"
REFRESH_LOG_FILENAME = "refresh_changes.jsonl"


@dataclass
class RefreshTarget:
    """One already-stored page that a refresh run may re-fetch."""

    url: str
    prid: str
    folder: str
    collected_at: str
    content_hash: str
    fields: dict[str, str]
    checked_at: str


class RefreshIndex:
    """Remembers when each stored page was last checked and what it contained.

    Kept apart from ``scraped_index.json`` on purpose: a refresh run must not
    rewrite the incremental skip list, and the skip list should stay small.
    """

    def __init__(self, site: "SiteFolderManager") -> None:
        self.site = site
        self.path = site.data_dir / REFRESH_INDEX_FILENAME
        self._lock = threading.RLock()
        self._pages: dict[str, dict[str, Any]] = {}
        self._dirty = 0

    def load(self) -> int:
        if not self.path.exists():
            return 0
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw = payload.get("pages") if isinstance(payload, dict) else {}
            if isinstance(raw, dict):
                self._pages = {
                    str(prid): value
                    for prid, value in raw.items()
                    if isinstance(value, dict)
                }
        except Exception as exc:
            logger.warning("Ignoring unreadable %s: %s", self.path.name, exc)
            self._pages = {}
        return len(self._pages)

    def get(self, prid: str) -> Optional[dict[str, Any]]:
        with self._lock:
            value = self._pages.get(str(prid or ""))
            return dict(value) if value else None

    def put(
        self,
        prid: str,
        url: str,
        content_hash: str,
        fields: dict[str, str],
        checked_at: str = "",
    ) -> None:
        prid = str(prid or "")
        if not prid:
            return
        with self._lock:
            self._pages[prid] = {
                "url": url,
                "content_hash": content_hash,
                "fields": dict(fields),
                "checked_at": checked_at or utc_now_iso(),
            }
            self._dirty += 1

    def count(self) -> int:
        with self._lock:
            return len(self._pages)

    def maybe_save(self, threshold: int = 200, force: bool = False) -> None:
        with self._lock:
            pending = self._dirty
        if force or pending >= max(1, threshold):
            self.save()

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": VERSION,
                "site_name": self.site.site_name,
                "start_url": self.site.start_url,
                "count": len(self._pages),
                "updated_at": utc_now_iso(),
                "pages": dict(self._pages),
            }
            self._dirty = 0
        atomic_write_json(self.path, payload)


# ---------------------------------------------------------------------------
# Excel / CSV export
# ---------------------------------------------------------------------------

EXCEL_COLUMNS = [
    "prid",
    "title",
    "category",
    "other_categories",
    "section",
    "language",
    "date_time",
    "collected_at",
    "source_url",
    "text_length",
    "text_preview",
    "images",
    "image_paths",
    "pdfs",
    "pdf_paths",
    "audio",
    "audio_paths",
    "video",
    "video_paths",
    "documents",
    "document_paths",
    "internal_links",
    "external_links",
]

_CELL_LIMIT = 32_000  # Excel's hard limit is 32,767 characters per cell


def _clip_cell(value: Any, limit: int = _CELL_LIMIT) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        return text[: limit - 15] + " …[truncated]"
    return text


def _record_row(record: dict[str, Any]) -> list[Any]:
    """Turn one JSONL record into a spreadsheet row."""
    text = str(record.get("text") or "")
    links = record.get("links") if isinstance(record.get("links"), list) else []
    internal = sum(
        1 for link in links
        if isinstance(link, dict) and link.get("type") == "internal"
    )
    external = sum(
        1 for link in links
        if isinstance(link, dict) and link.get("type") == "external"
    )
    others = record.get("categories") or []
    return [
        record.get("prid", ""),
        _clip_cell(record.get("title", ""), 500),
        record.get("category", ""),
        _clip_cell("; ".join(map(str, others)), 500),
        _clip_cell(record.get("section", ""), 300),
        record.get("language", ""),
        record.get("date_time", ""),
        record.get("collected_at", ""),
        _clip_cell(record.get("source_url", ""), 2_000),
        len(text),
        _clip_cell(clean_ws(text, 500), 500),
        len(record.get("image_paths") or record.get("image_url") or []),
        _clip_cell(" | ".join(map(str, record.get("image_paths") or [])), 5_000),
        len(record.get("pdf_paths") or record.get("pdf_files") or []),
        _clip_cell(" | ".join(map(str, record.get("pdf_paths") or [])), 5_000),
        len(record.get("audio_paths") or record.get("audio_files") or []),
        _clip_cell(" | ".join(map(str, record.get("audio_paths") or [])), 5_000),
        len(record.get("video_paths") or record.get("video_files") or []),
        _clip_cell(" | ".join(map(str, record.get("video_paths") or [])), 5_000),
        len(record.get("document_paths") or record.get("document_files") or []),
        _clip_cell(" | ".join(map(str, record.get("document_paths") or [])), 5_000),
        internal,
        external,
    ]


def _collect_records(site: SiteFolderManager) -> list[dict[str, Any]]:
    """Read every record from every category's pages.jsonl."""
    records: list[dict[str, Any]] = []
    for pages_file in sorted(site.categories_dir.glob("*/pages.jsonl")):
        try:
            with open(pages_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError as exc:
            logger.warning("Could not read %s: %s", pages_file, exc)
    return records


def _sheet_title(name: str, used: set[str]) -> str:
    """Return a unique, Excel-legal sheet name (max 31 characters)."""
    title = re.sub(r"[\[\]:*?/\\]", " ", str(name or ""))
    title = re.sub(r"\s+", " ", title).strip(" '") or "Sheet"
    title = title[:31]
    candidate = title
    counter = 2
    while candidate.lower() in used:
        suffix = f" ({counter})"
        candidate = title[: 31 - len(suffix)] + suffix
        counter += 1
    used.add(candidate.lower())
    return candidate


def _summary_rows(
    records: list[dict[str, Any]], extra: Iterable[tuple[str, Any]]
) -> list[tuple[str, str]]:
    """Build key/value summary rows plus per-category counters."""
    rows: list[tuple[str, str]] = [
        (str(key), _clip_cell(value, 2_000)) for key, value in extra
    ]
    per_category: dict[str, Counter] = {}
    for record in records:
        category = clean_ws(record.get("category") or "Uncategorized", 80)
        counts = per_category.setdefault(category, Counter())
        counts["pages"] += 1
        for key in ("image_paths", "pdf_paths", "audio_paths", "video_paths", "document_paths"):
            counts[key] += len(record.get(key) or [])
    totals = Counter()
    for category in sorted(per_category, key=str.lower):
        counts = per_category[category]
        totals.update(counts)
        rows.append(
            (
                f"category: {category}",
                "pages={pages} images={image_paths} pdfs={pdf_paths} "
                "audio={audio_paths} video={video_paths} documents={document_paths}".format(
                    **{k: counts.get(k, 0) for k in (
                        "pages", "image_paths", "pdf_paths",
                        "audio_paths", "video_paths", "document_paths",
                    )}
                ),
            )
        )
    rows.append(
        (
            "total",
            "pages={pages} images={image_paths} pdfs={pdf_paths} "
            "audio={audio_paths} video={video_paths} documents={document_paths}".format(
                **{k: totals.get(k, 0) for k in (
                    "pages", "image_paths", "pdf_paths",
                    "audio_paths", "video_paths", "document_paths",
                )}
            ),
        )
    )
    return rows


def _write_excel(
    path: Path,
    records: list[dict[str, Any]],
    summary_rows: Optional[list[tuple[str, str]]] = None,
    empty_message: str = "No pages scraped yet",
) -> Path:
    """Write one workbook: optional summary sheet, all rows, one sheet/category."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E78")
    used_titles: set[str] = set()

    def write_header(worksheet: Any) -> None:
        cells = []
        for name in EXCEL_COLUMNS:
            cell = WriteOnlyCell(worksheet, value=name)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
            cells.append(cell)
        worksheet.append(cells)
        for index, name in enumerate(EXCEL_COLUMNS, start=1):
            worksheet.column_dimensions[get_column_letter(index)].width = max(
                12, min(50, len(name) + 6)
            )
        try:
            worksheet.freeze_panes = "A2"
        except Exception:  # pragma: no cover - write-only mode differences
            pass

    workbook = Workbook(write_only=True)

    if summary_rows:
        summary_sheet = workbook.create_sheet(_sheet_title("Run Summary", used_titles))
        summary_sheet.append(["key", "value"])
        for key, value in summary_rows:
            summary_sheet.append([key, value])
        summary_sheet.column_dimensions["A"].width = 34
        summary_sheet.column_dimensions["B"].width = 110

    main_sheet = workbook.create_sheet(_sheet_title("All Pages", used_titles))
    write_header(main_sheet)

    per_category: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        main_sheet.append(_record_row(record))
        category = clean_ws(record.get("category") or "Uncategorized", 80) or "Uncategorized"
        per_category.setdefault(category, []).append(record)

    if not records:
        main_sheet.append([empty_message] + [""] * (len(EXCEL_COLUMNS) - 1))

    for category in sorted(per_category, key=str.lower):
        sheet = workbook.create_sheet(_sheet_title(category, used_titles))
        write_header(sheet)
        for record in per_category[category]:
            sheet.append(_record_row(record))

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    workbook.save(temp_path)
    os.replace(temp_path, path)
    return path


def _write_csv(path: Path, records: list[dict[str, Any]]) -> Path:
    """Fallback export used when openpyxl is not installed."""
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(EXCEL_COLUMNS)
        for record in records:
            writer.writerow(_record_row(record))
    os.replace(temp_path, path)
    return path


def export_reports(
    site: SiteFolderManager,
    since_iso: Optional[str] = None,
    crawl_summary: Optional[Iterable[tuple[str, Any]]] = None,
) -> list[Path]:
    """Write data/scraped_pages.xlsx (all) and data/new_pages.xlsx (this run).

    Falls back to ``.csv`` files when openpyxl is unavailable.
    """
    records = _collect_records(site)
    # "New" is only meaningful for a crawl run; export-only has no new pages.
    new_records = (
        [rec for rec in records if str(rec.get("collected_at") or "") >= since_iso]
        if since_iso
        else []
    )
    written: list[Path] = []

    master_summary = _summary_rows(
        records,
        [
            ("site_name", site.website_name),
            ("site_folder", str(site.site_dir)),
            ("start_url", site.start_url),
            ("exported_at", utc_now_iso()),
            ("mode", "cumulative index of every scraped page"),
        ],
    )
    new_extra: list[tuple[str, Any]] = [
        ("mode", "pages discovered in the most recent run only"),
        ("site_name", site.website_name),
        ("start_url", site.start_url),
        ("run_started_at", since_iso or ""),
        ("exported_at", utc_now_iso()),
    ]
    if crawl_summary:
        new_extra.extend(crawl_summary)
    new_extra.append(("new_pages_in_this_run", len(new_records)))
    new_summary = _summary_rows(new_records, new_extra)

    try:
        import openpyxl  # noqa: F401  (existence check only)

        excel_available = True
    except Exception:
        excel_available = False

    if excel_available:
        written.append(
            _write_excel(site.data_dir / "scraped_pages.xlsx", records, master_summary)
        )
        written.append(
            _write_excel(site.data_dir / "new_pages.xlsx", new_records, new_summary,
                         empty_message="No new pages in this run")
        )
    else:
        logger.warning(
            "openpyxl is not installed -- writing CSV instead. "
            "Install it with: pip install openpyxl"
        )
        written.append(_write_csv(site.data_dir / "scraped_pages.csv", records))
        written.append(_write_csv(site.data_dir / "new_pages.csv", new_records))
    return written


# ---------------------------------------------------------------------------
# india.gov.in service catalogue harvester
# ---------------------------------------------------------------------------

INDIA_CMS_ENDPOINT = "https://www.india.gov.in/internal/cms"
INDIA_SERVICE_DETAIL_URL = "https://www.india.gov.in/services/details/{alias}"
INDIA_SERVICES_SITE = "https://www.india.gov.in/"
# The site's own Strapi proxy.  Unlike the search API
# (``/services/category/dataservices``, which caps at 10,000 results) this
# collection paginates over all ~14,080 services.
INDIA_SERVICES_CMS_ROUTE = (
    "/api/services?fields[0]=title&fields[1]=description&fields[2]=url"
    "&fields[3]=owner&fields[4]=maturity&fields[5]=keywords&fields[6]=alias"
    "&fields[7]=img_url&fields[8]=service_language"
    "&populate[ministry_dept]=true&populate[state_dept_org]=true"
    "&populate[category]=true&sort=id"
    "&pagination[pageSize]={size}&pagination[page]={page}"
)


def _purge_india_service_shells(
    site: SiteFolderManager, purge_all: bool = False
) -> list[str]:
    """Delete stored records that hold no real content.

    By default only invalid entries are removed: JavaScript-only
    ``/services/details/`` stubs (the portal renders those pages client-side,
    so a plain HTML fetch saves an almost-empty shell) and CDN "Access Denied"
    pages.  With ``purge_all`` every service record is removed so it can be
    rebuilt from the CMS in one consistent pass.
    """
    purged: list[str] = []
    for pages_file in sorted(site.categories_dir.glob("*/pages.jsonl")):
        try:
            raw_lines = pages_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        kept: list[str] = []
        changed = False
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except Exception:
                kept.append(line)
                continue
            url = str(record.get("source_url") or "")
            text = str(record.get("text") or "")
            is_service = "/services/details/" in url
            lowered = text[:600].lower()
            is_blocked = (
                "access denied" in lowered or "errors.edgesuite.net" in lowered
            )
            if is_blocked or (is_service and (purge_all or len(text) < 120)):
                purged.append(url)
                changed = True
                continue
            kept.append(line)
        if changed:
            tmp = pages_file.with_name(pages_file.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                for line in kept:
                    fh.write(line + "\n")
            os.replace(tmp, pages_file)
    return purged


def _strapi_relation_titles(node: Any) -> list[str]:
    """Extract result titles from a Strapi relation (single, list or ``data``)."""
    if not node:
        return []
    if isinstance(node, dict) and "data" in node:
        node = node.get("data")
    if isinstance(node, dict):
        node = [node]
    if not isinstance(node, list):
        return []
    titles: list[str] = []
    for entry in node:
        if not isinstance(entry, dict):
            continue
        attrs = entry.get("attributes")
        if not isinstance(attrs, dict):
            attrs = entry
        title = clean_ws(attrs.get("title") or attrs.get("name"), 200)
        if title and title not in titles:
            titles.append(title)
    return titles


def _india_service_record(
    item: dict[str, Any], site: SiteFolderManager
) -> Optional[dict[str, Any]]:
    """Convert one catalogue item (CMS or search API shape) to a JSONL record."""
    attrs = item.get("attributes")
    if not isinstance(attrs, dict):
        attrs = item
    alias = clean_ws(attrs.get("alias") or attrs.get("npiAlias"), 240)
    title = clean_ws(attrs.get("title"), 500)
    if not alias:
        return None

    source_url = INDIA_SERVICE_DETAIL_URL.format(alias=alias)
    description = clean_ws(attrs.get("description"), 12000)
    owner = clean_ws(attrs.get("owner"), 200)
    maturity = clean_ws(attrs.get("maturity"), 200)
    service_url = clean_ws(attrs.get("url"), 1000)
    keywords = attrs.get("keywords")
    if isinstance(keywords, (list, tuple)):
        keywords = ", ".join(clean_ws(k, 120) for k in keywords if k)
    keywords = clean_ws(keywords, 1000)

    ministries = _strapi_relation_titles(attrs.get("ministry_dept"))
    if not ministries:
        raw = attrs.get("npiMinistryDepartment") or []
        if isinstance(raw, str):
            raw = [raw]
        ministries = [clean_ws(m, 200) for m in raw if clean_ws(m, 200)]
    state_depts = _strapi_relation_titles(attrs.get("state_dept_org"))
    if not state_depts:
        raw = attrs.get("npiStateDepartment") or []
        if isinstance(raw, str):
            raw = [raw]
        state_depts = [clean_ws(d, 200) for d in raw if clean_ws(d, 200)]
    site_categories = _strapi_relation_titles(attrs.get("category"))

    text_parts = [title]
    if description:
        text_parts.append(description)
    if owner:
        text_parts.append(f"Owner: {owner}")
    if maturity:
        text_parts.append(f"Maturity: {maturity}")
    if ministries:
        text_parts.append("Ministry/Department: " + "; ".join(ministries))
    if state_depts:
        text_parts.append("State/UT department: " + "; ".join(state_depts))
    if site_categories:
        text_parts.append("Categories: " + "; ".join(site_categories))
    if keywords:
        text_parts.append(f"Keywords: {keywords}")
    if service_url:
        text_parts.append(f"Service link: {service_url}")

    image_url = clean_ws(attrs.get("img_url") or attrs.get("imageUrl"), 1000)
    if image_url and not re.match(r"^https?://", image_url, re.IGNORECASE):
        image_url = urljoin(site.start_url, image_url)

    links: list[dict[str, str]] = []
    if service_url:
        links.append(
            {
                "text": title or service_url,
                "url": service_url,
                "type": "internal" if is_internal(service_url, site.host) else "external",
            }
        )

    language = clean_ws(attrs.get("service_language"), 80) or "English"
    categories_list = ["Services"]
    for name in site_categories:
        if name.lower() != "services" and name not in categories_list:
            categories_list.append(name)

    return {
        "prid": url_to_prid(source_url),
        "title": title,
        "text": "\n".join(text_parts),
        "date_time": "",
        "source_url": source_url,
        "website_name": site.website_name,
        "category": "Services",
        "categories": categories_list[:8],
        "section": (site_categories[0] if site_categories else "") or (
            ministries[0] if ministries else (state_depts[0] if state_depts else "")
        ),
        "image_url": [image_url] if image_url else [],
        "image_paths": [],
        "pdf_files": [],
        "pdf_paths": [],
        "audio_files": [],
        "audio_paths": [],
        "video_files": [],
        "video_paths": [],
        "links": links,
        "language": language,
        "collected_at": utc_now_iso(),
        "image_alts": [title] if image_url else [],
        "pdf_text_paths": [],
        "document_files": [],
        "document_paths": [],
    }


def harvest_india_services(
    site: SiteFolderManager,
    categories: CategoryManager,
    storage: "Storage",
    index: ScrapedIndex,
    http: HttpClient,
    downloader: Optional["Downloader"] = None,
    *,
    page_size: int = 100,
    max_records: int = 0,
    purge_shells: bool = True,
    refresh: bool = False,
) -> dict[str, int]:
    """Pull india.gov.in's whole service catalogue from its CMS API.

    The portal is a Next.js application: its ~14k service pages are rendered
    in the browser and never appear as ``<a>`` links, so a normal crawler stops
    after the few hundred static pages.  This reads the same CMS collection the
    site itself uses and writes the records through the usual
    JSONL / index / spreadsheet pipeline.

    The search endpoint (``/services/category/dataservices``) returns only the
    first 10,000 hits because of a server-side cap, which is why the CMS route
    is used instead.
    """
    page_size = max(1, min(int(page_size), 100))
    category = categories.resolve("Services")
    stats = {"added": 0, "known": 0, "duplicates": 0, "items": 0, "total": 0, "failed_pages": 0}

    if purge_shells:
        purged = _purge_india_service_shells(site, purge_all=refresh)
        if purged:
            index.forget(purged)
            logger.info(
                "Removed %d invalid stored record(s) (empty shells or CDN block pages).",
                len(purged),
            )

    page = 1
    page_count: Optional[int] = None
    image_count = 0
    started = time.monotonic()
    while True:
        if max_records and stats["added"] >= max_records:
            break
        if page_count is not None and page > page_count:
            break

        route = INDIA_SERVICES_CMS_ROUTE.format(size=page_size, page=page)
        data = http.post_json(INDIA_CMS_ENDPOINT, {"route": route})
        if data is None:
            stats["failed_pages"] += 1
            logger.warning(
                "Service catalogue page %d failed (HTTP); skipping to the next page.",
                page,
            )
            if page_count is not None and page >= page_count:
                break
            page += 1
            continue

        inner = data.get("data") or {}
        items = inner.get("data") if isinstance(inner, dict) else None
        pagination = (inner.get("meta") or {}).get("pagination") if isinstance(inner, dict) else None
        if isinstance(pagination, dict):
            total = int(pagination.get("total") or 0)
            if total:
                stats["total"] = total
            if pagination.get("pageCount"):
                page_count = int(pagination["pageCount"])
        if not items:
            logger.info("Service catalogue page %d returned no items; finished.", page)
            break

        for item in items:
            if max_records and stats["added"] >= max_records:
                break
            if not isinstance(item, dict):
                continue
            record = _india_service_record(item, site)
            if record is None:
                continue
            stats["items"] += 1
            if index.is_known(record["source_url"]):
                stats["known"] += 1
                continue

            if downloader is not None and record["image_url"]:
                downloaded = downloader.download(
                    record["image_url"][0], category, record["prid"], "image",
                    referrer=record["source_url"],
                )
                if downloaded is not None:
                    record["image_paths"] = [downloaded.relative_path]
                    image_count += 1

            if storage.append(record, category.folder):
                stats["added"] += 1
            else:
                stats["duplicates"] += 1
            index.add(record)

        elapsed = time.monotonic() - started
        if page % 10 == 0 or page_count is None or page >= (page_count or 0):
            logger.info(
                "Service catalogue: page %d/%s | items seen=%d | new=%d | known=%d | %.0fs",
                page, page_count or "?", stats["items"], stats["added"], stats["known"],
                elapsed,
            )
        page += 1

    stats["images"] = image_count
    return stats


# ---------------------------------------------------------------------------
# Crawl state (resume)
# ---------------------------------------------------------------------------

@dataclass
class QueueItem:
    """One URL waiting to be crawled (or a lone file download)."""

    url: str
    kind: str = "page"
    category_hint: str = "uncategorized"
    referrer: str = ""
    link_only: bool = False
    depth: int = 0
    external: bool = False
    refresh: bool = False
    internal_depth: int = 0
    display_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "category_hint": self.category_hint,
            "referrer": self.referrer,
            "link_only": self.link_only,
            "depth": self.depth,
            "external": self.external,
            "refresh": self.refresh,
            "internal_depth": self.internal_depth,
            "display_name": self.display_name,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "QueueItem":
        return cls(
            url=str(payload.get("url") or ""),
            kind=str(payload.get("kind") or "page"),
            category_hint=str(payload.get("category_hint") or "uncategorized"),
            referrer=str(payload.get("referrer") or ""),
            link_only=bool(payload.get("link_only")),
            depth=max(0, int(payload.get("depth") or 0)),
            external=bool(payload.get("external")),
            refresh=bool(payload.get("refresh")),
            internal_depth=max(0, int(payload.get("internal_depth") or 0)),
            display_name=str(payload.get("display_name") or ""),
        )


class StateStore:
    """Atomic JSON state used to resume interrupted crawls."""

    def __init__(self, path: Path, site_name: str, start_url: str) -> None:
        self.path = path
        self.site_name = site_name
        self.start_url = start_url

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return payload
        except Exception as exc:
            logger.warning("Ignoring unreadable state file %s: %s", self.path, exc)
            return None

    def save(
        self,
        visited: Iterable[str],
        queue: Iterable[QueueItem],
        pages_done: int,
        completed: bool = False,
        mode: str = "",
    ) -> None:
        payload = {
            "version": VERSION,
            "site_name": self.site_name,
            "start_url": self.start_url,
            "mode": mode,
            "visited": sorted(set(visited)),
            "queue": [item.to_dict() for item in queue if item.url],
            "pages_done": pages_done,
            "completed": completed,
            "updated_at": utc_now_iso(),
        }
        atomic_write_json(self.path, payload)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

PAGE_CTYPES = {"text/html", "application/xhtml+xml"}
MAX_HTML_BYTES = 20 * 1024 * 1024


def looks_blocked_page(html_text: str) -> bool:
    """True for the HTTP-200 "Access Denied" page some WAFs return."""
    head = html_text[:8000].lower()
    return (
        "you don't have permission to access" in head
        or "errors.edgesuite.net" in head
        or "<title>access denied</title>" in head
        or "<title>403 forbidden</title>" in head
    )


class Crawler:
    """Breadth-first, polite, resumable crawler orchestration."""

    def __init__(
        self,
        site: SiteFolderManager,
        categories: CategoryManager,
        parser: Parser,
        http: HttpClient,
        downloader: Downloader,
        storage: Storage,
        state: StateStore,
        index: ScrapedIndex,
        failures: FailureLog,
        stop_event: threading.Event,
        start_url: str,
        max_pages: int = 0,
        workers: int = 4,
        state_every: int = STATE_SAVE_EVERY,
        js_enabled: bool = True,
        restart: bool = False,
        render_timeout: int = 60,
        render_budget: int = 0,
        honor_robots_delay: bool = True,
        incremental: bool = True,
        export_excel: bool = True,
        refresh_links: int = 25,
        rediscover: bool = False,
        seed_file: Optional[str] = None,
        media_enabled: bool = True,
        download_images: bool = True,
        media_kinds: Optional[set[str]] = None,
        image_formats: Optional[set[str]] = None,
        follow_external: bool = False,
        external_max_pages: int = 5000,
        max_depth: int = 3,
        max_internal_depth: int = 0,
        external_only: bool = False,
        follow_service_links: bool = False,
        internal_first: bool = False,
        include_service_pages: bool = False,
        category_complete: bool = False,
        drain_only: bool = False,
        refresh_existing: bool = False,
        refresh_after_days: float = 0.0,
        refresh_limit: int = 0,
        refresh_media: bool = False,
        refresh_index: Optional["RefreshIndex"] = None,
        prefer_english: bool = True,
        translate: bool = True,
    ) -> None:
        self.site = site
        self.categories = categories
        self.parser = parser
        self.http = http
        self.downloader = downloader
        self.storage = storage
        self.state = state
        self.index = index
        self.failures = failures
        self.stop_event = stop_event
        self.start_url = start_url
        self.max_pages = max(0, max_pages)
        self.workers = max(1, workers)
        self.state_every = max(1, state_every)
        self.js_enabled = js_enabled
        self.restart = restart
        self.render_timeout = max(10, render_timeout)
        self.honor_robots_delay = honor_robots_delay
        self.incremental = incremental
        self.export_excel = export_excel
        # --restart turns every skip check off for this run.
        self.effective_incremental = bool(incremental) and not restart
        self.refresh_links = max(0, refresh_links)
        self.rediscover = rediscover and not refresh_existing and not drain_only
        self.seed_file = seed_file
        self.media_enabled = media_enabled
        self.formats = {}
        self.download_images = bool(download_images)
        # None = every media kind; otherwise only these kinds are downloaded
        # (the URLs are still stored in the record either way).
        self.media_kinds = set(media_kinds) if media_kinds is not None else None
        # None/empty = every image format; otherwise only these extensions are
        # downloaded.  Image URLs stay in the record either way.
        cleaned_formats = {
            str(ext).strip().lower().lstrip(".")
            for ext in (image_formats or set())
            if str(ext).strip()
        }
        self.image_formats = cleaned_formats or None
        self.follow_external = bool(follow_external)
        self.http.allow_redirect = lambda source, target: (
            is_internal(target, self.site.host) or
            (self.follow_external and self._external_host_allowed(target) and
             (is_internal(source, self.site.host) or same_site(source, target))))
        self.external_max_pages = max(0, int(external_max_pages))
        self.max_depth = max(0, int(max_depth))
        # 0 = follow internal links as deep as the site goes (old behaviour).
        self.max_internal_depth = max(0, int(max_internal_depth))
        # External web scrap: internal pages are only used to find off-site
        # links; they are never saved and their media is never downloaded.
        self.external_only = bool(external_only)
        self.mode = select_mode(self.follow_external, self.external_only)
        self.follow_service_links = bool(follow_service_links)
        self.drain_only = bool(drain_only)
        self.external_pages = 0
        self.service_links_seeded = 0
        self.service_files_seeded = 0
        self._external_seen: set[str] = set()
        self._external_budget_logged = False

        # --internal-first / --include-service-pages / --category-complete bookkeeping
        self.internal_first = bool(internal_first)
        self.include_service_pages = bool(include_service_pages)
        self.category_complete = bool(category_complete)
        self._service_page_urls: set[str] = set()
        self._service_done: set[str] = set()
        self._service_lock = threading.Lock()
        # Category-complete: track which category we're currently exhausting
        if self.include_service_pages:
            self._load_service_page_targets()

        # --refresh-existing bookkeeping
        self.refresh_existing = bool(refresh_existing)
        self.refresh_after_days = max(0.0, float(refresh_after_days or 0))
        self.refresh_limit = max(0, int(refresh_limit or 0))
        self.refresh_media = bool(refresh_media)
        self.refresh_index = refresh_index
        self.prefer_english = bool(prefer_english) or RefreshIndex(site)
        # Best-effort translation of pages that stay non-English even after the
        # site's own language switch.  On failure the original text is saved.
        self.translate_enabled = bool(translate)
        self.translator = (
            EnglishTranslator(site.data_dir / "translation_cache.json")
            if self.translate_enabled else None
        )
        # External government portals are often Hindi by default.  Prime each
        # newly seen host once so its session cookie points at English before
        # any worker fetches a page from it.
        self._prime_events: dict[str, threading.Event] = {}
        self._prime_lock = threading.Lock()
        self._refresh_targets: dict[str, RefreshTarget] = {}
        self.refreshed_total = 0
        self.refreshed_changed = 0
        self.refreshed_unchanged = 0
        self.refreshed_baseline = 0
        self.refreshed_failed = 0
        self.media_replaced = 0
        self._refresh_log_lock = threading.Lock()
        self._refresh_log_path = site.data_dir / REFRESH_LOG_FILENAME

        self.run_started_at = utc_now_iso()
        self.new_pages = 0
        self.translated_pages = 0
        self.translation_failed = 0
        self._counter_lock = threading.Lock()
        self._render_lock = threading.Lock()
        # Rendering runs in a subprocess, but only one at a time so several
        # worker threads cannot start several browsers at once.
        self._render_gate = threading.Semaphore(1)
        self._render_budget = int(render_budget) if render_budget > 0 else None
        self.render_failures = 0
        self.empty_pages = 0
        self._playwright_available = importlib.util.find_spec("playwright") is not None
        if js_enabled and not self._playwright_available:
            logger.info(
                "Playwright not installed -- JavaScript fallback disabled. "
                "Install it with: pip install playwright && python -m playwright install chromium"
            )
        self._start_time = time.monotonic()
        self._interrupted = False
        # True only when the seed page itself could not be fetched and the run
        # therefore produced no data at all.
        self.start_failed = False
        self._failed_urls: set[str] = set()
        self._block_retried: set[str] = set()

    # -- state helpers ----------------------------------------------------
    def _save_snapshot(
        self,
        visited: set[str],
        pending: Iterable[QueueItem],
        inflight: dict[Any, QueueItem],
        pages_done: int,
        completed: bool = False,
    ) -> None:
        inflight_items = list(inflight.values())
        queue_items = inflight_items + list(pending)
        visited_snapshot = set(visited) - {item.url for item in inflight_items}
        try:
            self.state.save(visited_snapshot, queue_items, pages_done, completed,
                            mode=self.mode.name)
            self.downloader.save_index()
            self.categories.write_index()
            self.index.save()
            if self.refresh_existing:
                self.refresh_index.save()
        except Exception as exc:
            logger.warning("Could not save state: %s", exc)

    # -- media helpers ----------------------------------------------------
    @staticmethod
    def _kind_from_ctype(ctype: str) -> Optional[str]:
        """Map a Content-Type header to a downloadable kind (or ``None``).

        ``None`` means the response is a web asset (CSS/JS/JSON/XML/HTML)
        rather than a real document, so it must not be stored as a file.
        """
        ctype = (ctype or "").split(";")[0].strip().lower()
        if not ctype:
            return "document"
        if ctype.startswith("image/"):
            return "image"
        if "pdf" in ctype:
            return "pdf"
        if ctype.startswith("audio/"):
            return "audio"
        if ctype.startswith("video/"):
            return "video"
        if ctype in (
            "text/css", "text/javascript", "application/javascript",
            "application/x-javascript", "application/ecmascript",
            "application/json", "application/ld+json", "application/manifest+json",
            "text/xml", "application/xml", "application/rss+xml",
            "application/atom+xml", "application/xhtml+xml", "text/html",
            "application/x-httpd-php", "application/x-php",
        ) or ctype.startswith(("application/json", "application/xml")):
            return None
        return "document"

    # -- external crawling helpers ---------------------------------------
    @staticmethod
    def _external_host_allowed(url: str) -> bool:
        try:
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except Exception:
            return False
        if not host:
            return False
        for blocked in EXTERNAL_BLOCKED_HOSTS:
            if host == blocked or host.endswith("." + blocked):
                return False
        # Catch any country-specific Google/social domain (google.co.uk, etc.).
        if host.startswith(("google.", "facebook.", "instagram.", "twitter.")):
            return False
        return True

    def _claim_external_page(self, url: str = "") -> bool:
        """Reserve one slot of the external-page budget (thread safe).

        A URL is only claimed once per run, so repeated navigation links
        cannot exhaust the off-site budget before new pages are reached.
        """
        with self._counter_lock:
            if url and url in self._external_seen:
                return False
            if (
                self.external_max_pages > 0
                and self.external_pages >= self.external_max_pages
            ):
                if not self._external_budget_logged:
                    self._external_budget_logged = True
                    logger.info(
                        "External page budget reached (%d); no further off-site "
                        "pages will be queued this run.",
                        self.external_max_pages,
                    )
                return False
            if url:
                self._external_seen.add(url)
            self.external_pages += 1
            return True

    def _selected(self, kind, url):
        if not self.media_enabled or (self.media_kinds is not None and kind not in self.media_kinds):
            return False
        internal = is_internal(url, self.site.host)
        if (self.external_only and internal) or (not internal and
                (not self.follow_external or not self._external_host_allowed(url))):
            return False
        suffix = url_suffix(url).lower().lstrip('.')
        return kind not in self.formats or not suffix or suffix in self.formats[kind]

    def _image_format_allowed(self, url: str) -> bool:
        """True when an image URL's extension is one the user selected.

        An extension-less URL is allowed so the response's Content-Type can
        decide; the UI's format chips only narrow URLs with a known suffix.
        """
        allowed = self.image_formats
        if not allowed:
            return True
        ext = url_suffix(url).lstrip(".")
        if not ext:
            return True
        return ext in allowed

    def _download_all(
        self,
        parsed: ParsedPage,
        category: CategoryInfo,
        page_id: str,
        page_url: str,
        force_kinds: Optional[set[str]] = None,
    ) -> dict[str, list[str]]:
        """Download every media item; return relative paths by kind.

        *force_kinds* re-downloads those media kinds even when the file is
        already present (used by ``--refresh-media``); the file on disk is only
        replaced when the bytes actually differ.
        """
        result: dict[str, list[str]] = {
            "images": [], "pdfs": [], "audio": [], "video": [],
            "documents": [], "pdf_text": [], "pdf_names": [], "document_names": [],
        }
        if not self.media_enabled:
            # Text/link-only crawl: keep the media URLs in the record without
            # downloading the files (much faster for huge sites).
            return result
        force_kinds = force_kinds or set()
        jobs: list[tuple[str, list[str]]] = [
            ("pdf", list(parsed.pdfs)),
            ("audio", list(parsed.audios)),
            ("video", list(parsed.videos)),
            ("document", list(parsed.documents)),
        ]
        if self.media_kinds is not None:
            # Only the kinds the user switched on are downloaded; the others
            # stay in the record as links.
            jobs = [(kind, urls) for kind, urls in jobs if kind in self.media_kinds]
        if self.download_images and (self.media_kinds is None or "image" in self.media_kinds):
            # Images are mostly logos/banners but account for the bulk of the
            # requests, so --no-images is the big speed win.  When the user
            # picked specific formats (for example PNG only), other image
            # files such as SVG are left as links in the record.
            jobs.insert(
                0,
                ("image", [
                    u for u, _ in parsed.images if self._image_format_allowed(u)
                ]),
            )
        seen_counts: dict[str, int] = {}
        topic_names = getattr(parsed, "download_names", {}) or {}
        for kind, urls in jobs:
            key = KIND_COUNT_KEYS[kind]
            name_key = (
                "pdf_names" if kind == "pdf"
                else "document_names" if kind == "document" else ""
            )
            for url in urls:
                if self.stop_event.is_set():
                    break
                if not self._selected(kind, url) or (
                    not is_internal(page_url, self.site.host) and not same_site(page_url, url)
                ):
                    continue
                display_name = ""
                if name_key:
                    occurrence = seen_counts.get(name_key, 0)
                    seen_counts[name_key] = occurrence + 1
                    topic = str(topic_names.get(url) or "").strip()
                    if not topic:
                        topic = fallback_download_name(
                            parsed.title, parsed.links, url, kind, page_id,
                            text=parsed.text if len(urls) == 1 else "", index=occurrence,
                        )
                    if topic:
                        base = safe_topic_stem(topic, max_len=100, default=kind, seed=url)
                        display_name = base
                downloaded = self.downloader.download(
                    url, category, page_id, kind, referrer=page_url,
                    force=kind in force_kinds, display_name=display_name,
                )
                if downloaded is None:
                    continue
                if downloaded.replaced:
                    with self._counter_lock:
                        self.media_replaced += 1
                    logger.info("Replaced changed %s: %s", kind, downloaded.relative_path)
                result[key].append(downloaded.relative_path)
                if name_key and display_name:
                    result[name_key].append(Path(downloaded.relative_path).name)
                if downloaded.text_relative_path:
                    result["pdf_text"].append(downloaded.text_relative_path)
        return result

    # -- JS rendering -----------------------------------------------------
    @staticmethod
    def _is_blocked_page(parsed: ParsedPage) -> bool:
        title = (parsed.title or "").strip().lower()
        text = (parsed.text or "")[:600].lower()
        return title in {"access denied", "403 forbidden"} or (
            "you don't have permission to access" in text
            or "errors.edgesuite.net" in text
        )

    def _needs_render(self, parsed: ParsedPage) -> bool:
        if not self.js_enabled or not self._playwright_available:
            return False
        if len(parsed.text) >= 300 and parsed.title and not parsed.interactive:
            return False
        title = (parsed.title or "").lower()
        return not any(
            marker in title for marker in ("404", "not found", "access denied")
        )

    def _render_with_playwright(self, url: str) -> Optional[bytes]:
        with self._render_lock:
            if self._render_budget == 0:
                logger.warning(
                    "JavaScript render budget used up; %s is saved as fetched.", url
                )
                return None
            if self._render_budget is not None:
                self._render_budget -= 1
        script = str(Path(__file__).resolve())
        proxy = (self.http.proxy_pool.acquire()
                 if getattr(self.http, "proxy_pool", None) else None)
        cmd = [
            sys.executable, script, "--render-html", url,
            "--render-timeout", str(self.render_timeout),
        ]
        if not self.parser.buttons:
            cmd.append("--no-buttons")
        if proxy:
            cmd.extend(["--proxy", proxy])
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "timeout": self.render_timeout + 40,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        logger.info("JavaScript fallback (Playwright) for %s", url)
        try:
            # One browser at a time keeps memory use predictable when several
            # worker threads hit JS-only pages at once.
            with self._render_gate:
                completed = subprocess.run(cmd, **kwargs)
        except Exception as exc:
            logger.warning("Playwright subprocess failed for %s: %s", url, exc)
            with self._counter_lock:
                self.render_failures += 1
            return None
        html = completed.stdout or b""
        text = html.decode("utf-8", "replace")
        blocked = looks_blocked_page(text)
        if completed.returncode != 0 or not html.strip() or blocked:
            err = (completed.stderr or b"").decode("utf-8", "replace")[-400:].strip()
            logger.warning(
                "JavaScript render did not help for %s (%s)",
                url, "blocked page" if blocked else (err or "no content"),
            )
            with self._counter_lock:
                self.render_failures += 1
            if blocked:
                self.http.note_pressure(url, "rendered Access Denied page", cooldown=True)
                if proxy and getattr(self.http, "proxy_pool", None):
                    self.http.proxy_pool.mark_bad(proxy, "rendered block page")
            return None
        if proxy and getattr(self.http, "proxy_pool", None):
            self.http.proxy_pool.mark_good(proxy)
        return html

    # -- internal-first / service pages -----------------------------------
    @staticmethod
    def _service_key(url: str) -> str:
        return normalize_url(url) or url

    def _load_service_page_targets(self) -> None:
        """Collect API-harvested service URLs that still need a real fetch.

        ``--india-services`` stores the /services/details/... rows from the CMS
        JSON API and puts their URLs in the skip list, so those pages are never
        fetched as HTML.  With ``--include-service-pages`` the same URLs are
        queued for a real page fetch and their records go to a separate
        "Service Pages" category -- the URLs (and therefore the prids) are
        identical, so mixing them into "Services" would look like duplicates.
        """
        done_dir = self.categories.resolve(SERVICE_PAGES_CATEGORY).path
        done_file = done_dir / "pages.jsonl"
        done: set[str] = set()
        if done_file.exists():
            with done_file.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        url = str(json.loads(line).get("source_url") or "")
                    except Exception:
                        continue
                    if url:
                        done.add(self._service_key(url))
        self._service_done = done

        source = self.site.category_dir("services") / "pages.jsonl"
        urls: set[str] = set()
        if source.exists():
            with source.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        url = str(json.loads(line).get("source_url") or "")
                    except Exception:
                        continue
                    if SERVICE_DETAILS_PREFIX in url:
                        urls.add(self._service_key(url))
        self._service_page_urls = {u for u in urls if u and u not in done}
        logger.info(
            "Service pages: %d to fetch, %d already stored.",
            len(self._service_page_urls), len(done),
        )

    def _wants_service_page(self, url: str) -> bool:
        """True when *url* is an API service row that still needs a page fetch."""
        if not self._service_page_urls:
            return False
        return self._service_key(url) in self._service_page_urls

    def _mark_service_page_done(self, url: str) -> None:
        key = self._service_key(url)
        with self._service_lock:
            self._service_page_urls.discard(key)
            self._service_done.add(key)

    def _seed_service_pages(self, pending: deque["QueueItem"]) -> int:
        """Append every unread service page to the queue once per run."""
        if not self._service_page_urls:
            return 0
        queued = {item.url for item in pending if item.url}
        added = 0
        for url in sorted(self._service_page_urls):
            if url in queued:
                continue
            pending.append(
                QueueItem(
                    url=url,
                    kind="page",
                    category_hint=SERVICE_PAGES_CATEGORY,
                    depth=0,
                    external=False,
                )
            )
            queued.add(url)
            added += 1
        if added:
            logger.info("Queued %d service page(s) for a full HTML fetch.", added)
        return added

    # -- URL filtering ----------------------------------------------------
    def _is_other_language_url(self, url: str) -> bool:
        """True for a link that would switch the site away from English."""
        if not self.prefer_english:
            return False
        return url_is_other_language(url)

    def _should_skip(self, url: str) -> bool:
        low = url.lower()
        if any(hint in low for hint in TRACKING_PATH_HINTS):
            return True
        if re.search(r"[?&](?:print|share|replytocom|logout|signout)=", low):
            return True
        # Language paths such as /hi/ are real pages, not navigation noise.
        # They are crawled and translated instead of being skipped.
        return False

    def _translate_page(self, parsed: ParsedPage, final_url: str) -> None:
        """Translate non-English title/text to English when possible.

        Nothing is skipped: if the translation service is unavailable or a
        request fails, the page is stored exactly as it was found.
        """
        translator = self.translator
        if translator is None or not translator.available:
            return

        need_title = self._label_needs_translation(parsed.title)
        need_text = is_non_english_text(parsed.text or "")
        if not need_title and not need_text:
            # Still translate non-English menu labels so category folders and
            # page metadata read in English.
            self._translate_nav_entries(parsed)
            return

        source_language = parsed.language or ""
        translated_any = False
        failed = False

        if need_title:
            translated = translator.translate(parsed.title)
            if translated:
                parsed.original_title = parsed.title
                parsed.title = translated
                translated_any = True
            else:
                failed = True

        if need_text:
            translated = translator.translate(parsed.text)
            if translated:
                parsed.original_text = parsed.text
                parsed.text = translated
                translated_any = True
                parsed.language = "English"
            else:
                failed = True

        if translated_any:
            parsed.translated = True
            parsed.source_language = source_language
            with self._counter_lock:
                self.translated_pages += 1
            logger.info(
                "Language : translated non-English page to English: %s", final_url)
        if failed:
            parsed.translation_failed = True
            with self._counter_lock:
                self.translation_failed += 1
            logger.warning(
                "Language : translation failed; keeping the original language: %s",
                final_url,
            )
        self._translate_nav_entries(parsed)

    @staticmethod
    def _label_needs_translation(label: str) -> bool:
        if not label:
            return False
        return bool(
            INDIC_SCRIPT_RE.search(label) or NON_LATIN_SCRIPT_RE.search(label)
        )

    def _translate_nav_entries(self, parsed: ParsedPage) -> None:
        translator = self.translator
        if translator is None or not translator.available:
            return
        entries = parsed.nav_entries or []
        if not entries:
            return
        labels = [display for _, display in entries]
        if not any(self._label_needs_translation(label) for label in labels):
            return
        translated = translator.translate_lines(labels)
        if not translated:
            return
        parsed.nav_entries = [
            (path, new_label or old_label)
            for (path, old_label), new_label in zip(entries, translated)
        ]

    def _translate_label(self, label: str) -> str:
        """Translate one short label (a category/link name) when needed."""
        translator = self.translator
        if translator is None or not translator.available:
            return label
        if not self._label_needs_translation(label):
            return label
        translated = translator.translate(label)
        return translated or label

    # -- one item ---------------------------------------------------------
    def _page_links(
        self,
        parsed: ParsedPage,
        final_url: str,
        category_hint: str,
        depth: int = 0,
        external: bool = False,
        internal_depth: int = 0,
    ) -> list[QueueItem]:
        """Build queue items for every new HTML link on a page.

        Internal pages are followed until ``--internal-depth`` is reached
        (0 = no limit, the old behaviour).  When ``--follow-external`` is
        enabled, links to other websites are followed too -- but only up to
        ``--max-depth`` hops and ``--external-max-pages`` off-site pages.
        Once on an external site only links on that same site are followed, so
        the crawl cannot wander across the whole web.

        When ``--category-complete`` is enabled, links are grouped by category
        and the crawler exhausts all pages in one category before moving to
        the next, ensuring deep scraping of paginated categories like Documents.
        """
        if self.drain_only:
            # Finish the already-queued URLs only; never discover new pages.
            return []
        new_items: list[QueueItem] = []
        seen_links: set[str] = set()
        source_external = external or not is_internal(final_url, self.site.host)
        for link in parsed.links:
            target = link.get("url") or ""
            if not target or target in seen_links or target == final_url:
                continue
            if self._should_skip(target):
                continue
            media_kind = classify_media(target)
            if media_kind:
                # External web scrap: a file linked from an internal page is
                # kept only when it lives on another website.
                if (
                    self.external_only
                    and self.media_enabled
                    and not is_internal(target, self.site.host)
                    and (self.media_kinds is None or media_kind in self.media_kinds)
                ):
                    seen_links.add(target)
                    new_items.append(
                        QueueItem(
                            url=target,
                            kind="file",
                            category_hint=category_hint,
                            display_name=parsed.download_names.get(target) or link.get("text") or parsed.title,
                            referrer=final_url,
                            depth=1,
                            external=True,
                        )
                    )
                continue
            if url_suffix(target) in NON_HTML_EXT:
                continue

            target_internal = is_internal(target, self.site.host)
            child = self.mode.next_depth(
                Depth(internal_depth, depth), source_internal=not source_external,
                target_internal=target_internal, same_host=same_site(target, final_url),
                internal_limit=self.max_internal_depth, external_limit=self.max_depth)
            if child is None:
                continue
            if not target_internal and (
                    not self._external_host_allowed(target) or not self._claim_external_page(target)):
                continue
            hint = category_hint
            if target_internal:
                if hint.lower() == "uncategorized":
                    hint = self.categories._segment_category(target) or hint
                hint = self.categories._nav_match(target) or hint
            seen_links.add(target)
            item = QueueItem(
                url=target,
                kind="page",
                category_hint=hint,
                referrer=final_url,
                depth=child.external,
                external=not target_internal,
                internal_depth=child.internal,
                display_name=parsed.download_names.get(target) or link.get("text") or parsed.title,
            )

            new_items.append(item)
        return new_items

    def _process(
        self, item: QueueItem
    ) -> tuple[list[QueueItem], int, bool, str]:
        """Process one queue item -> (new items, pages done, ok, final URL)."""
        if self.stop_event.is_set() or not item.url:
            return [], 0, False, ""
        if not is_internal(item.url, self.site.host) and (
                not self.follow_external or not self._external_host_allowed(item.url)):
            return [], 0, True, item.url

        if (
            item.kind == "page"
            and not item.link_only
            and not item.refresh
            and self.effective_incremental
            and self.index.is_known(item.url)
            and not self._wants_service_page(item.url)
        ):
            # Safety net: incremental runs never refetch a known page.
            return [], 0, True, item.url

        if item.kind == "file":
            category = self.categories.resolve(item.category_hint or "uncategorized")
            page_id = url_to_prid(item.referrer or item.url)
            kind = classify_media(item.url) or "document"
            if not self._selected(kind, item.url):
                return [], 0, True, item.url
            if self.media_kinds is not None and kind not in self.media_kinds:
                return [], 0, True, item.url
            if self.external_only and is_internal(item.url, self.site.host):
                return [], 0, True, item.url
            downloaded = self.downloader.download(
                item.url, category, page_id, kind, referrer=item.referrer,
                force=self.refresh_media, display_name=item.display_name if kind in ("pdf", "document") else ""
            )
            return [], (1 if downloaded else 0), bool(downloaded), ""

        if not item.link_only:
            self._prime_host_language(item.url)

        response = self.http.get(item.url, stream=True)
        if response is None:
            return [], 0, False, ""

        final_url = normalize_url(response.url) or item.url
        if (self.rediscover and self.effective_incremental and not item.refresh
                and self.index.is_known(final_url)):
            item.link_only = True
        if (
            self.effective_incremental
            and not item.link_only
            and not item.refresh
            and final_url
            and final_url != item.url
            and self.index.is_known(final_url)
            and not self._wants_service_page(final_url)
        ):
            # Redirected to a page we already have; do not parse/download again.
            try:
                response.close()
            except Exception:
                pass
            logger.debug("Redirect target already scraped, skipping: %s", final_url)
            return [], 0, True, final_url

        ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        suffix = url_suffix(final_url)
        is_html = ctype in PAGE_CTYPES or (not ctype and suffix not in NON_HTML_EXT)

        if not is_html:
            # Extension-less download links (or unexpected binary content).
            kind = classify_media(final_url) or self._kind_from_ctype(ctype)
            try:
                response.close()
            except Exception:
                pass
            if kind is None or not self._selected(kind, final_url) or (self.external_only and is_internal(final_url,self.site.host)):
                # CSS/JS/JSON/XML and similar web assets are not documents.
                logger.debug("Skipping non-document response (%s): %s", ctype, final_url)
                return [], 0, True, final_url
            if kind in self.formats and self.downloader._ext_from_ctype(ctype).lstrip(".") not in self.formats[kind] and not classify_media(final_url):
                return [], 0, True, final_url
            if kind == "image" and not self._image_format_allowed(final_url):
                # The user selected other image formats, so keep this URL only
                # in the page's link list instead of downloading the file.
                logger.debug(
                    "Image format not selected, keeping the link only: %s",
                    final_url,
                )
                return [], 0, True, final_url
            category = self.categories.resolve(item.category_hint or "uncategorized")
            page_id = url_to_prid(item.referrer or final_url)
            downloaded = self.downloader.download(
                final_url, category, page_id, kind, referrer=item.referrer,
                force=self.refresh_media, display_name=item.display_name if kind in ("pdf", "document") else ""
            )
            return [], (1 if downloaded else 0), bool(downloaded), final_url

        try:
            length = int(response.headers.get("Content-Length") or 0)
            if length > MAX_HTML_BYTES:
                logger.warning("Skipping oversized HTML page (%d bytes): %s", length, final_url)
                return [], 1, True, final_url
            content = response.content
        except Exception as exc:
            logger.warning("Could not read %s: %s", final_url, exc)
            return [], 0, False, final_url
        finally:
            try:
                response.close()
            except Exception:
                pass

        if not content:
            logger.warning("Empty response body: %s", final_url)
            return [], 0, False, final_url

        try:
            parsed = self.parser.parse(content, final_url)
        except Exception as exc:
            logger.warning("HTML parse failed for %s: %s", final_url, exc)
            return [], 0, False, final_url

        # Some CDNs (Akamai in front of india.gov.in / dahd.gov.in) answer with
        # HTTP 200 and an "Access Denied" page instead of a 403.  Never store
        # that as data, and slow the crawl down when it happens.
        if self._is_blocked_page(parsed) or looks_blocked_page(
            content[:8000].decode("utf-8", "replace")
        ):
            logger.warning("Blocked page (access denied), not saved: %s", final_url)
            self.http.note_pressure(final_url, "Access Denied page", cooldown=True)
            if item.url not in self._block_retried:
                # Wait out the cool-down (done inside http.get) and try once more.
                self._block_retried.add(item.url)
                return self._process(item)
            self.failures.add(final_url, "access denied page")
            return [], 0, False, final_url

        render_failed = False
        if self.js_enabled and self._needs_render(parsed):
            rendered = self._render_with_playwright(final_url)
            if rendered:
                try:
                    rendered_page = self.parser.parse(rendered, final_url)
                    rendered_page.retain_discovery(parsed)
                    parsed = rendered_page
                except Exception as exc:
                    logger.debug("Parse of rendered HTML failed for %s: %s", final_url, exc)
                # The rendered page can itself be a WAF block page.
                if self._is_blocked_page(parsed):
                    logger.warning(
                        "Blocked page (access denied) after rendering, not saved: %s",
                        final_url,
                    )
                    self.failures.add(final_url, "access denied page")
                    return [], 0, False, final_url
            else:
                render_failed = True

        if is_internal(final_url, self.site.host):
            self.categories.learn_nav(parsed.nav_entries)
        if item.link_only or not self.mode.saves_page(is_internal(final_url, self.site.host)):
            # link_only: cheap re-check of an already-scraped hub page.
            # external_only: an internal page is used only to find off-site
            # links; no internal record or media is stored.
            logger.debug("Link discovery only: %s", final_url)
            return (
                self._page_links(
                    parsed,
                    final_url,
                    item.category_hint or "uncategorized",
                    depth=item.depth,
                    external=item.external,
                    internal_depth=item.internal_depth,
                ),
                int(self.rediscover),
                True,
                final_url,
            )

        # Never present a content-less page as a successful scrape without a
        # trace: count it, log it and flag the record.
        if not (parsed.title or "").strip() and len((parsed.text or "").strip()) < 50:
            with self._counter_lock:
                self.empty_pages += 1
            render_failed = True
            logger.warning(
                "Page has no title/text (JavaScript content was not available): %s",
                final_url,
            )

        # Try to turn any remaining Hindi/Tamil/... text into English.  If the
        # translation service is unavailable the page is still stored with its
        # original text -- no page is ever skipped for its language.
        if self.parser.collect_text:
            self._translate_page(parsed, final_url)

        external_page = not is_internal(final_url, self.site.host)
        if external_page:
            # Keep every off-site page in one predictable place instead of
            # scattering government portals across guessed categories.
            category = self.categories.resolve(EXTERNAL_CATEGORY)
            match_categories = [EXTERNAL_CATEGORY]
            section = urlsplit(final_url).hostname or "external"
            page_id = url_to_prid(final_url)
        else:
            if self._wants_service_page(final_url):
                # Real HTML copy of an API-harvested service row.
                category = self.categories.resolve(SERVICE_PAGES_CATEGORY)
                match_categories = [SERVICE_PAGES_CATEGORY]
                section = "Services"
                page_id = url_to_prid(final_url)
            else:
                match = self.categories.detect(parsed, final_url)
                # Non-English menu labels are translated above, but the
                # detected primary/categories can still be Hindi on a mixed
                # site; translate them before they become folder names.
                primary = self._translate_label(match.primary)
                if self.category_complete and item.category_hint and item.category_hint.lower() != "uncategorized":
                    primary = item.category_hint
                category = self.categories.resolve(primary)
                match_categories = [
                    self._translate_label(name) for name in match.categories
                ]
                section = self._translate_label(match.section)
                page_id = url_to_prid(final_url)

        if not self.parser.collect_text:
            parsed.text = parsed.original_text = ""
        fields = content_fields(parsed)
        fingerprint = content_fingerprint(fields)
        force_kinds: set[str] = set()
        if item.refresh and self.refresh_media:
            force_kinds = {"image", "pdf", "document", "audio", "video"}

        media = self._download_all(
            parsed, category, page_id, final_url, force_kinds=force_kinds
        )

        collected_at = utc_now_iso()
        record = {
            "prid": page_id,
            "title": parsed.title,
            "text": parsed.text,
            "date_time": parsed.date_time,
            "source_url": final_url,
            "website_name": self.site.website_name,
            "category": category.display,
            "categories": match_categories,
            "section": section,
            "image_url": [url for url, _ in parsed.images if self._selected("image", url) and self._image_format_allowed(url)],
            "image_paths": media["images"],
            "pdf_files": [u for u in parsed.pdfs if self._selected("pdf", u)],
            "pdf_paths": media["pdfs"],
            "pdf_names": media["pdf_names"],
            "audio_files": [u for u in parsed.audios if self._selected("audio", u)],
            "audio_paths": media["audio"],
            "video_files": [u for u in parsed.videos if self._selected("video", u)],
            "video_paths": media["video"],
            "links": parsed.links,
            "language": parsed.language,
            "collected_at": collected_at,
            "content_hash": fingerprint,
            "last_checked": collected_at,
            # Additional fields required by the extraction spec:
            "image_alts": [alt for u, alt in parsed.images if self._selected("image", u) and self._image_format_allowed(u)],
            "pdf_text_paths": media["pdf_text"],
            "document_files": [u for u in parsed.documents if self._selected("document", u)],
            "document_paths": media["documents"],
            "document_names": media["document_names"],
        }
        if render_failed:
            record["render_failed"] = True
        if parsed.translated or parsed.translation_failed:
            record["translated"] = parsed.translated
            record["translation_failed"] = parsed.translation_failed
            record["original_language"] = parsed.source_language or parsed.language
            if parsed.original_title:
                record["original_title"] = parsed.original_title
            if parsed.original_text:
                record["original_text"] = parsed.original_text
        if item.refresh:
            # A partial re-check must not clear fields collected by an earlier
            # run. Storage merges the selected fields into the existing row.
            if not self.parser.collect_text:
                record.pop("text", None)
            if not self.parser.hyperlinks and not self.parser.buttons:
                record.pop("links", None)
            for kind, keys in {
                "image": ("image_url", "image_paths", "image_alts"),
                "pdf": ("pdf_files", "pdf_paths", "pdf_names", "pdf_text_paths"),
                "audio": ("audio_files", "audio_paths"),
                "video": ("video_files", "video_paths"),
                "document": ("document_files", "document_paths", "document_names"),
            }.items():
                if not self.media_enabled or (self.media_kinds is not None and kind not in self.media_kinds):
                    for key in keys:
                        record.pop(key, None)
            self._handle_refresh(item, record, category.folder, fields)
        else:
            appended = self.storage.append(record, category.folder)
            if appended:
                with self._counter_lock:
                    self.new_pages += 1
            else:
                logger.debug("Duplicate page record skipped: %s", final_url)
            # Register in the skip list even if the JSONL row already existed.
            self.index.add(record)
            for page_url in (final_url, item.url):
                if self._wants_service_page(page_url):
                    self._mark_service_page_done(page_url)

        return (
            self._page_links(
                parsed,
                final_url,
                item.category_hint if external_page else category.display,
                depth=item.depth,
                external=external_page,
                internal_depth=item.internal_depth,
            ),
            1,
            True,
            final_url,
        )

    # -- refresh mode ------------------------------------------------------
    def _load_refresh_targets(self) -> list[RefreshTarget]:
        """List stored pages that this run may re-fetch, oldest check first."""
        targets: list[RefreshTarget] = []
        skipped_services = 0
        skipped_files = 0
        seen_prids: set[str] = set()
        for pages_file in sorted(self.site.categories_dir.glob("*/pages.jsonl")):
            folder = pages_file.parent.name
            try:
                with open(pages_file, "r", encoding="utf-8") as fh:
                    for line in fh:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            record = json.loads(stripped)
                        except Exception:
                            continue
                        url = str(record.get("source_url") or "")
                        prid = str(record.get("prid") or "")
                        if not url or not prid or prid in seen_prids:
                            continue
                        if not is_internal(url, self.site.host) and not self.follow_external:
                            continue
                        if record.get("service_id") and folder == "services":
                            skipped_services += 1
                            continue
                        if classify_media(url) or url_suffix(url) in NON_HTML_EXT:
                            skipped_files += 1
                            continue
                        seen_prids.add(prid)
                        meta = self.refresh_index.get(prid) or {}
                        fields = meta.get("fields")
                        targets.append(
                            RefreshTarget(
                                url=url,
                                prid=prid,
                                folder=folder,
                                collected_at=str(record.get("collected_at") or ""),
                                content_hash=str(
                                    meta.get("content_hash")
                                    or record.get("content_hash")
                                    or ""
                                ),
                                fields=fields if isinstance(fields, dict) else {},
                                checked_at=str(
                                    meta.get("checked_at")
                                    or record.get("last_checked")
                                    or ""
                                ),
                            )
                        )
            except OSError as exc:
                logger.warning("Could not read %s for refresh: %s", pages_file, exc)

        total = len(targets)
        if self.refresh_after_days > 0:
            cutoff = time.time() - self.refresh_after_days * 86400
            targets = [
                target
                for target in targets
                if not target.checked_at or iso_to_epoch(target.checked_at) <= cutoff
            ]
        aged = len(targets)
        targets.sort(
            key=lambda target: (
                iso_to_epoch(target.checked_at),
                0 if is_internal(target.url, self.site.host) else 1,
                target.url,
            )
        )
        if self.refresh_limit > 0:
            targets = targets[: self.refresh_limit]

        logger.info(
            "Refresh    : %d stored page(s), %d after age filter, %d selected"
            " (skipped %d service-catalogue, %d direct-file record(s)).",
            total, aged, len(targets), skipped_services, skipped_files,
        )
        if skipped_services:
            logger.info(
                "Refresh    : service records are rebuilt with "
                "--india-services --india-services-refresh, not by page refresh."
            )
        return targets

    def _log_refresh_change(self, payload: dict[str, Any]) -> None:
        try:
            with self._refresh_log_lock:
                with open(self._refresh_log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("Could not append to %s: %s", self._refresh_log_path, exc)

    def _handle_refresh(
        self,
        item: QueueItem,
        record: dict[str, Any],
        folder: str,
        fields: dict[str, str],
    ) -> None:
        """Compare a re-fetched page with the stored one and update it in place."""
        with self._counter_lock:
            self.refreshed_total += 1
        target = self._refresh_targets.get(item.url)
        fingerprint = str(record.get("content_hash") or content_fingerprint(fields))
        checked_at = str(record.get("last_checked") or utc_now_iso())

        if target is None:
            # The stored row disappeared (or moved categories) mid-run.
            if self.storage.upsert(record, folder) == "skipped":
                with self._counter_lock:
                    self.refreshed_failed += 1
                return
            self.index.add(record)
            self.refresh_index.put(
                str(record["prid"]), str(record["source_url"]), fingerprint, fields, checked_at
            )
            with self._counter_lock:
                self.refreshed_baseline += 1
            return

        status = "unchanged"
        if not target.content_hash:
            # First time this record gets a fingerprint: baseline, not a change.
            status = "baseline"
        elif target.content_hash != fingerprint:
            status = "changed"

        if status == "unchanged":
            self.refresh_index.put(
                str(record["prid"]), str(record["source_url"]), fingerprint, fields, checked_at
            )
            self.refresh_index.maybe_save()
            with self._counter_lock:
                self.refreshed_unchanged += 1
            logger.debug("Refresh: unchanged %s", item.url)
            return

        if status == "changed":
            record["updated_at"] = checked_at
            if target.collected_at:
                record["collected_at"] = target.collected_at

        if self.storage.upsert(record, folder) == "skipped":
            with self._counter_lock:
                self.refreshed_failed += 1
            logger.warning("Refresh: could not rewrite record for %s", item.url)
            return

        if target.folder and (target.folder != folder or target.prid != record["prid"]):
            self.storage.remove(target.prid, target.folder)
            logger.info(
                "Refresh: %s moved from %s to %s", item.url, target.folder, folder
            )
        if target.url and target.url != record["source_url"]:
            self.index.forget([target.url])
        self.index.add(record)
        self.refresh_index.put(
            str(record["prid"]), str(record["source_url"]), fingerprint, fields, checked_at
        )
        self.refresh_index.maybe_save()

        with self._counter_lock:
            if status == "changed":
                self.refreshed_changed += 1
            else:
                self.refreshed_baseline += 1

        if status == "changed":
            changed_fields: list[str] = []
            if target.fields:
                changed_fields = [
                    name
                    for name in sorted(set(target.fields) | set(fields))
                    if target.fields.get(name) != fields.get(name)
                ]
            logger.info(
                "Refresh: CHANGED %s (%s)",
                record["source_url"],
                ", ".join(changed_fields) or "content",
            )
            self._log_refresh_change(
                {
                    "checked_at": checked_at,
                    "url": record["source_url"],
                    "previous_url": target.url if target.url != record["source_url"] else "",
                    "prid": record["prid"],
                    "title": str(record.get("title") or "")[:300],
                    "old_hash": target.content_hash,
                    "new_hash": fingerprint,
                    "changed_fields": changed_fields,
                }
            )

    def _hub_urls(self, known: set[str], limit: int) -> list[str]:
        """Pick up to *limit* shallow, already-scraped pages to re-check links.

        Homepage and section/menu pages change most often, so they are the
        cheapest place to discover newly added pages.
        """
        if limit <= 0 or not known:
            return []
        seed = normalize_url(self.start_url)
        candidates = [
            url for url in known
            if is_internal(url, self.site.host)
            and not classify_media(url)
            and url_suffix(url) not in NON_HTML_EXT
        ]

        def depth(url: str) -> int:
            try:
                return len([part for part in urlsplit(url).path.split("/") if part])
            except Exception:
                return 99

        # Menus and listing pages live near the root; skip deep article URLs.
        candidates = [url for url in candidates if depth(url) <= 2]
        candidates.sort(
            key=lambda url: (0 if url == seed else 1, depth(url), url)
        )
        return candidates[:limit]

    def _seed_from_history(
        self, pending: deque[QueueItem], visited: set[str]
    ) -> int:
        """Queue unseen links found on pages scraped in earlier runs.

        This is how incremental runs still discover *new* pages without
        re-downloading the old ones: every stored record keeps its link list.
        When external following is enabled, off-site links from internal
        pages and same-site links from external pages are replayed too.
        """
        added = 0
        queued = {item.url for item in pending if item.url}
        for source, target in self.index.iter_history_links(
            include_external=self.follow_external
        ):
            if not target or target in visited or target in queued:
                continue
            if target in self._failed_urls:
                continue
            if classify_media(target) or url_suffix(target) in NON_HTML_EXT:
                continue
            if self._should_skip(target):
                continue

            source_internal = is_internal(source, self.site.host)
            if source_internal:
                if is_internal(target, self.site.host):
                    depth, external, hint = 0, False, "uncategorized"
                else:
                    # Service-catalogue links are handled (or deliberately
                    # skipped) by _seed_from_service_links, not replayed here.
                    if "/services/details/" in source and not self.follow_service_links:
                        continue
                    # First off-site hop; depth is unknown for historic pages.
                    if not self.follow_external:
                        continue
                    if not self._external_host_allowed(target):
                        continue
                    depth, external, hint = 1, True, EXTERNAL_CATEGORY
            else:
                # Never wander away from an external site's own domain.
                if not self.follow_external or not same_site(target, source):
                    continue
                if not self._external_host_allowed(target):
                    continue
                depth, external, hint = 1, True, EXTERNAL_CATEGORY

            if external and not self._claim_external_page(target):
                continue
            pending.append(
                QueueItem(
                    url=target,
                    kind="page",
                    category_hint=hint,
                    referrer=source,
                    depth=depth,
                    external=external,
                )
            )
            queued.add(target)
            added += 1
        return added

    def _seed_from_service_links(
        self, pending: deque[QueueItem], visited: set[str]
    ) -> int:
        """Queue the off-site service-portal links stored on service records.

        The india.gov.in service catalogue is harvested from a JSON API, so
        the linked ministry/state portals and their PDFs/applications were
        never fetched.  This replays those links: direct file links are
        downloaded, and (when ``--follow-external`` is on) HTML portals are
        crawled like any other external page.
        """
        if not self.follow_service_links:
            return 0
        queued = {item.url for item in pending if item.url}
        seen: set[str] = set()
        pages_added = 0
        files_added = 0
        records_seen = 0

        for pages_file in sorted(self.site.categories_dir.glob("*/pages.jsonl")):
            try:
                fh = open(pages_file, "r", encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not read service links from %s: %s", pages_file, exc)
                continue
            try:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    source = str(record.get("source_url") or "")
                    if "/services/details/" not in source:
                        continue
                    records_seen += 1
                    links = record.get("links")
                    if not isinstance(links, list):
                        continue
                    category_name = str(record.get("category") or "Services")
                    for link in links:
                        if not isinstance(link, dict):
                            continue
                        if link.get("type") != "external":
                            continue
                        target = normalize_url(str(link.get("url") or ""))
                        if (
                            not target
                            or target in seen
                            or target in visited
                            or target in queued
                            or target in self._failed_urls
                        ):
                            continue
                        if is_internal(target, self.site.host):
                            continue
                        seen.add(target)
                        media_kind = classify_media(target)
                        if media_kind:
                            if not self.media_enabled:
                                continue
                            pending.append(
                                QueueItem(
                                    url=target,
                                    kind="file",
                                    category_hint=category_name,
                                    referrer=source,
                                    depth=1,
                                    external=True,
                                )
                            )
                            queued.add(target)
                            files_added += 1
                            continue
                        if url_suffix(target) in NON_HTML_EXT:
                            continue
                        if not self._external_host_allowed(target):
                            continue
                        if not self._claim_external_page(target):
                            continue
                        pending.append(
                            QueueItem(
                                url=target,
                                kind="page",
                                category_hint=EXTERNAL_CATEGORY,
                                referrer=source,
                                depth=1,
                                external=True,
                            )
                        )
                        queued.add(target)
                        pages_added += 1
            finally:
                fh.close()

        self.service_links_seeded = pages_added
        self.service_files_seeded = files_added
        if pages_added or files_added:
            logger.info(
                "Service catalogue links: %d external page(s) and %d file(s) "
                "queued from %d service record(s).",
                pages_added, files_added, records_seen,
            )
        return pages_added + files_added

    def _seed_from_file(
        self, pending: deque[QueueItem], visited: set[str]
    ) -> int:
        """Queue URLs listed in ``--seed-file`` (one per line).

        Many large sites hide their real content behind JavaScript/CMS APIs,
        so no amount of link-following finds those pages.  A seed file lets
        you supply the URLs directly; they are treated exactly like discovered
        links (same host, HTML only, deduplicated, incremental-safe).  When
        ``--follow-external`` is enabled, off-site URLs and direct file URLs
        in the seed file are accepted too.
        """
        if not self.seed_file:
            return 0
        path = Path(self.seed_file).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            logger.warning("Seed file not found: %s", path)
            return 0

        added = 0
        skipped = 0
        queued = {item.url for item in pending if item.url}
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    target = normalize_url(line)
                    if not target:
                        skipped += 1
                        continue
                    if target in visited or target in queued:
                        continue
                    if target in self._failed_urls:
                        continue
                    target_internal = is_internal(target, self.site.host)
                    if not target_internal and not self.follow_external:
                        skipped += 1
                        continue
                    hint = "uncategorized" if target_internal else EXTERNAL_CATEGORY

                    media_kind = classify_media(target)
                    if media_kind:
                        if not self.media_enabled:
                            skipped += 1
                            continue
                        pending.append(
                            QueueItem(
                                url=target, kind="file",
                                category_hint=hint,
                                depth=0 if target_internal else 1,
                                external=not target_internal,
                            )
                        )
                        queued.add(target)
                        added += 1
                        continue
                    if url_suffix(target) in NON_HTML_EXT:
                        skipped += 1
                        continue
                    if self._should_skip(target):
                        skipped += 1
                        continue
                    depth = 0
                    external = False
                    if not target_internal:
                        if not self._external_host_allowed(target):
                            skipped += 1
                            continue
                        if not self._claim_external_page(target):
                            skipped += 1
                            continue
                        depth, external = 1, True
                    pending.append(
                        QueueItem(
                            url=target, kind="page",
                            category_hint=hint,
                            depth=depth,
                            external=external,
                        )
                    )
                    queued.add(target)
                    added += 1
        except OSError as exc:
            logger.warning("Could not read seed file %s: %s", path, exc)
            return added

        logger.info(
            "Seed file: %d URL(s) queued, %d already known/invalid skipped (%s).",
            added, skipped, path,
        )
        return added

    # -- language ---------------------------------------------------------
    @staticmethod
    def _find_english_link(soup: BeautifulSoup, base_url: str) -> str:
        """Return the best same-host link that switches the site to English."""
        host = (urlsplit(base_url).hostname or "").lower()
        best = ""
        best_score = 0

        def consider(raw_href: str, score: int) -> None:
            nonlocal best, best_score
            href = (raw_href or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                return
            absolute = urljoin(base_url, href)
            parts = urlsplit(absolute)
            if parts.scheme not in ("http", "https"):
                return
            if (parts.hostname or "").lower() != host:
                return
            if score > best_score:
                best, best_score = absolute, score

        # <link rel="alternate" hreflang="en" href="...">
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            hreflang = (link.get("hreflang") or "").lower()
            if "alternate" in rel and hreflang.startswith("en"):
                consider(link["href"], 6)

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            text = " ".join(anchor.get_text(" ", strip=True).split()).lower()
            hreflang = (anchor.get("hreflang") or "").lower()
            score = 0
            if hreflang.startswith("en"):
                score += 6
            if text in ("english", "en", "english version", "english (en)"):
                score += 5
            elif "english" in text and len(text) <= 40:
                score += 3
            if re.search(r"[?&](?:lang|language|locale|hl|setlang)=en(?:[&#]|$)", href, re.I):
                score += 4
            if re.search(r"(?:^|/)lang(?:uage)?/en(?:[/?#]|$)", href, re.I):
                score += 4
            if re.search(r"/(?:en|eng)(?:[/?#]|$)", href, re.I):
                score += 2
            if score:
                consider(href, score)
        return best

    def _prime_language(self, seed_url: str) -> Optional[str]:
        """Switch the site to English when it offers the choice.

        Returns an extra entry URL when the English version lives at a
        different path (for example ``/en/``); returns ``None`` when the
        site is already English, has no English switch, or the switch only
        stores a cookie (as ``/lang/en`` does on dare.gov.in).
        """
        if not self.prefer_english:
            return None

        resp = self.http.get(seed_url)
        if resp is None:
            return None
        try:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type and "html" not in content_type and "xml" not in content_type:
                return None
            html = resp.text
            final_url = normalize_url(resp.url) or seed_url
        finally:
            try:
                resp.close()
            except Exception:
                pass

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            return None

        current = self.parser._extract_language(soup)
        if current.lower().startswith("en"):
            logger.info("Language : %s (the site's default)", current)
            return None

        link = self._find_english_link(soup, final_url)
        if not link:
            logger.info(
                "Language : %s (no English switch found; non-English pages "
                "will be translated to English)", current,
            )
            return None

        switched = self.http.get(link)
        if switched is None:
            logger.warning(
                "Language : the English switch %s could not be fetched; "
                "non-English pages will be translated or kept as-is (%s).",
                link, current,
            )
            return None
        try:
            switch_final = normalize_url(switched.url) or link
        finally:
            try:
                switched.close()
            except Exception:
                pass

        logger.info(
            "Language : switched from %s to English through %s", current, link
        )
        if switch_final and switch_final != final_url:
            logger.info("Language : the English version starts at %s", switch_final)
            return switch_final
        return None

    def _prime_host_language(self, url: str) -> None:
        """Switch a newly seen external host to English before its first page.

        Many state portals keep the chosen language in a session cookie.  The
        shared cookie jar means priming the host once is enough for every
        worker thread.  Internal hosts are already primed from the start URL.
        """
        if not self.prefer_english:
            return
        host = (urlsplit(url).hostname or "").lower()
        if not host or is_internal(url, self.site.host):
            return
        with self._prime_lock:
            event = self._prime_events.get(host)
            if event is None:
                event = threading.Event()
                self._prime_events[host] = event
                owner = True
            else:
                owner = False
        if not owner:
            # Another worker is already priming this host; wait so the first
            # request uses the English cookie too, but never block forever.
            event.wait(timeout=60)
            return
        try:
            scheme = urlsplit(url).scheme or "https"
            self._prime_language(f"{scheme}://{host}/")
        except Exception as exc:
            logger.debug("Language priming failed for %s: %s", host, exc)
        finally:
            event.set()

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        seed_url = normalize_url(self.start_url)
        if not seed_url:
            raise SystemExit(f"Invalid start URL: {self.start_url}")
        # Prefer English before any worker starts: many sites (dare.gov.in,
        # for example) keep the chosen language in a session cookie, and the
        # shared cookie jar makes that choice visible to every thread.
        english_seed = self._prime_language(seed_url)
        seed_kind = "file" if classify_media(seed_url) else "page"
        seed = QueueItem(url=seed_url, kind=seed_kind, category_hint="uncategorized")
        if english_seed and english_seed != seed_url:
            logger.info("Language : English entry page found at %s", english_seed)

        incremental = self.effective_incremental
        known_urls = self.index.known_urls() if incremental else set()
        if incremental:
            logger.info(
                "Incremental mode: %d page(s) already in the scrape index "
                "will be skipped (only new pages are fetched).",
                len(known_urls),
            )
        elif not self.restart:
            logger.info("Incremental mode disabled: every reachable page will be re-fetched.")

        pending: deque[QueueItem] = deque()
        visited: set[str] = set()
        pages_done = 0
        restored = False
        seed_ok = True

        state_payload = self.state.load()
        if state_payload and not self.restart:
            same_start = normalize_url(state_payload.get("start_url", "")) == seed_url
            state_done = bool(state_payload.get("completed"))
            same_mode = state_payload.get("mode", self.mode.name) == self.mode.name
            if same_start and not same_mode:
                logger.info("Crawl mode changed to %s; discovering from the seed with existing records intact.",
                            self.mode.name)
            elif same_start and not state_done:
                restored = True
                visited = set(state_payload.get("visited") or [])
                pending = deque(
                    QueueItem.from_dict(raw)
                    for raw in (state_payload.get("queue") or [])
                    if isinstance(raw, dict)
                )
                pages_done = int(state_payload.get("pages_done") or 0)
                logger.info(
                    "Resuming interrupted crawl: %d URLs queued, %d already visited, "
                    "%d pages done this run.",
                    len(pending), len(visited), pages_done,
                )
            elif same_start:
                if not incremental and not self.refresh_existing:
                    logger.info("Previous crawl already complete for %s.", seed_url)
                    self._summary(0, interrupted=False)
                    return
                logger.info(
                    "Previous crawl is complete -- checking only for newly added pages."
                )
            elif state_payload.get("queue"):
                logger.info(
                    "State belongs to a different start URL (%s); starting fresh for %s.",
                    state_payload.get("start_url"), seed_url,
                )

        if incremental and not self.rediscover:
            # Never re-fetch anything already recorded, even when resuming.
            visited |= known_urls

        if not restored:
            pending = deque()
            pages_done = 0
            if seed_url not in visited:
                pending.append(seed)
            elif not incremental:
                # --restart / --no-incremental: crawl the seed page again.
                visited.discard(seed_url)
                pending.append(seed)
            # Scrape the English entry as well, so the language switch never
            # hides the original start page.
            if (
                english_seed
                and english_seed != seed_url
                and english_seed not in visited
            ):
                pending.appendleft(
                    QueueItem(
                        url=english_seed,
                        kind="page",
                        category_hint="uncategorized",
                    )
                )
                logger.info("Language : queued the English entry page %s", english_seed)

        # Off-site service-portal links stored in the CMS harvest.
        if not self.drain_only:
            self._seed_from_service_links(pending, visited)

        if self.refresh_existing:
            targets = self._load_refresh_targets()
            self._refresh_targets = {target.url: target for target in targets}
            queued_refresh = {i.url for i in pending if i.refresh}
            targets = [target for target in targets if target.url not in queued_refresh]
            if targets:
                for target in reversed(targets):
                    pending.appendleft(
                        QueueItem(
                            url=target.url,
                            kind="page",
                            refresh=True, category_hint=target.folder,
                            depth=0,
                            external=not is_internal(target.url, self.site.host),
                        )
                    )
                logger.info(
                    "Refresh    : re-fetching %d page(s)%s; only changed records "
                    "are rewritten.",
                    len(targets),
                    (
                        f" last checked {self.refresh_after_days:g}+ days ago"
                        if self.refresh_after_days
                        else ""
                    ),
                )

        if incremental and not self.rediscover:
            # A content refresh already re-reads the pages, so the cheap
            # hub link recheck would only duplicate work.
            hub_urls = [] if (self.refresh_existing or self.drain_only) else self._hub_urls(
                known_urls, self.refresh_links
            )
            for hub_url in reversed(hub_urls):
                pending.appendleft(
                    QueueItem(url=hub_url, kind="page", link_only=True)
                )
            if hub_urls:
                logger.info(
                    "Re-checking links on %d already-scraped hub page(s) "
                    "(no media downloads, no duplicate records).",
                    len(hub_urls),
                )
            discovered = 0 if self.drain_only or not self.parser.hyperlinks else self._seed_from_history(
                pending, visited
            )
            if discovered:
                logger.info(
                    "Queued %d unseen link(s) found on already-scraped pages.",
                    discovered,
                )

        # Explicit seeds (e.g. pages only reachable through a CMS API/JS, or the
        # direct links collected by the app's "overview" mode).  These are
        # supplied by the caller, not discovered, so they are honoured even with
        # --drain-only; their own links are still not followed.
        self._seed_from_file(pending, visited)
        if self.include_service_pages:
            self._seed_service_pages(pending)

        if not pending:
            logger.info(
                "Nothing new to scrape -- the local dataset for %s is up to date.",
                self.site.website_name,
            )
            self._save_snapshot(visited, pending, {}, pages_done, completed=True)
            self._summary(pages_done, interrupted=False)
            return

        robots_delay = self.http.robots.crawl_delay(seed_url) if self.honor_robots_delay else 0.0
        if robots_delay > 0:
            effective = max(self.http.limiter.delay, min(robots_delay, 10.0))
            if effective > self.http.limiter.delay:
                logger.info("Honouring robots.txt Crawl-delay: %.1fs", effective)
                self.http.limiter.set_delay(effective)

        queued_urls: set[str] = {item.url for item in pending if item.url}
        frontier = CrawlQueue(pending, self.category_complete, self.internal_first)
        pending.clear()
        limit = self.max_pages
        total = limit if limit > 0 else None
        inflight: dict[Any, QueueItem] = {}
        last_saved_pages = pages_done
        bar = tqdm(
            total=total,
            unit="page",
            desc="crawling",
            dynamic_ncols=True,
            smoothing=0.05,
            file=sys.stderr,
        )

        try:
            with cfutures.ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="crawler"
            ) as pool:
                while True:
                    if self.stop_event.is_set():
                        self._interrupted = True
                        break

                    # Dispatch work.
                    while frontier and len(inflight) < self.workers * 3:
                        if self.stop_event.is_set():
                            break
                        inflight_pages = sum(
                            1 for it in inflight.values()
                            if it.kind == "page" and (self.rediscover or not it.link_only)
                        )
                        if limit > 0 and pages_done + inflight_pages >= limit:
                            break
                        item = frontier.take(bool(inflight))
                        if item is None:
                            break
                        queued_urls.discard(item.url)
                        if not item.url or item.url in self._failed_urls:
                            continue
                        if (
                            item.url in visited
                            and not item.link_only
                            and not item.refresh
                            and not self._wants_service_page(item.url)
                        ):
                            continue
                        if self.rediscover and item.kind == "page" and item.url in known_urls:
                            item.link_only = True
                        if self.rediscover or not item.link_only:
                            visited.add(item.url)
                        future = pool.submit(self._process, item)
                        inflight[future] = item

                    if not inflight:
                        if not frontier or self.stop_event.is_set():
                            break
                        if limit > 0 and pages_done >= limit:
                            break
                        continue

                    done, _ = cfutures.wait(
                        set(inflight), timeout=0.5,
                        return_when=cfutures.FIRST_COMPLETED,
                    )
                    for future in done:
                        item = inflight.pop(future, None)
                        if item is None:
                            continue
                        try:
                            new_items, processed, ok, final_url = future.result()
                        except Exception as exc:
                            logger.exception("Unhandled error for %s: %s", item.url, exc)
                            new_items, processed, ok, final_url = [], 0, False, ""
                        if processed:
                            pages_done += processed
                            bar.update(processed)
                        if final_url:
                            visited.add(final_url)
                        if not ok:
                            visited.discard(item.url)
                            self._failed_urls.add(item.url)
                            if item.url == seed_url:
                                seed_ok = False
                            if item.refresh:
                                with self._counter_lock:
                                    self.refreshed_total += 1
                                    self.refreshed_failed += 1
                        for new_item in new_items:
                            if (
                                new_item.url
                                and new_item.url not in visited
                                and new_item.url not in queued_urls
                                and new_item.url not in self._failed_urls
                            ):
                                frontier.append(new_item)
                                queued_urls.add(new_item.url)

                    if pages_done - last_saved_pages >= self.state_every:
                        self._save_snapshot(visited, frontier, inflight, pages_done)
                        last_saved_pages = pages_done
        except KeyboardInterrupt:
            self._interrupted = True
            logger.warning("Interrupted by user; saving state...")
        finally:
            bar.close()
            finished = not self._interrupted and not frontier and not inflight
            self._save_snapshot(
                visited, frontier, inflight, pages_done, completed=finished
            )

        self.start_failed = bool(
            not seed_ok and pages_done == 0 and self.new_pages == 0
        )
        self._summary(pages_done, interrupted=self._interrupted)

    # -- summary ----------------------------------------------------------
    def _summary(self, pages_done: int, interrupted: bool) -> None:
        elapsed = time.monotonic() - self._start_time
        self.categories.recount()
        self.categories.write_index()
        totals = Counter()
        logger.info("=" * 78)
        logger.info(
            "%s crawl %s for %s",
            "Interrupted" if interrupted else "Finished",
            f"({pages_done} pages)",
            self.site.website_name,
        )
        logger.info("Site folder : %s", self.site.site_dir)
        logger.info("-" * 78)
        for info in self.categories.all():
            counts = info.counts
            for key in ("images", "pdfs", "audio", "video", "documents"):
                totals[key] += counts[key]
            totals["pages"] += counts["pages"]
            logger.info(
                "  %-32s pages=%-6d images=%-6d pdfs=%-5d audio=%-4d video=%-4d docs=%-4d",
                info.folder, counts["pages"], counts["images"], counts["pdfs"],
                counts["audio"], counts["video"], counts["documents"],
            )
        logger.info("-" * 78)
        logger.info(
            "TOTAL        pages=%-6d images=%-6d pdfs=%-5d audio=%-4d video=%-4d docs=%-4d",
            totals["pages"], totals["images"], totals["pdfs"], totals["audio"],
            totals["video"], totals["documents"],
        )
        logger.info(
            "Requests=%d | failures=%d | elapsed=%s | state=%s",
            self.http.request_count,
            self.failures.count,
            time.strftime("%H:%M:%S", time.gmtime(elapsed)),
            "saved" if self.site.state_path.exists() else "none",
        )
        if self.external_only:
            ext_pages = 0
            for info in self.categories.all():
                if info.display == EXTERNAL_CATEGORY:
                    ext_pages = info.counts["pages"]
                    break
            if ext_pages == 0:
                logger.warning(
                    "No external websites found on %s. External web scrap only "
                    "stores pages on other domains, so nothing was saved.",
                    self.start_url,
                )
        skipped_known = (
            max(0, self.index.count() - self.new_pages)
            if self.effective_incremental
            else 0
        )
        logger.info(
            "NEW pages this run=%d | already-scraped pages skipped=%d | "
            "indexed pages total=%d",
            self.new_pages,
            skipped_known,
            self.index.count(),
        )
        if self.translated_pages:
            logger.info(
                "Language : translated %d non-English page(s) to English.",
                self.translated_pages,
            )
        if self.translation_failed:
            logger.info(
                "Language : kept the original language for %d page(s) "
                "(translation unavailable).",
                self.translation_failed,
            )
        if self.render_failures or self.empty_pages:
            logger.warning(
                "JavaScript: %d render(s) failed | %d page(s) saved without "
                "title/text. See the per-page warnings above.",
                self.render_failures, self.empty_pages,
            )
        if getattr(self.http, "throttle_events", 0):
            logger.warning(
                "Politeness: slowed down %d time(s) because the server pushed "
                "back. Final delay is %.1fs between requests.",
                self.http.throttle_events, self.http.limiter.delay,
            )
        if self.follow_external or self.follow_service_links:
            logger.info(
                "External pages queued=%d (budget=%s) | service links added: "
                "pages=%d files=%d",
                self.external_pages,
                self.external_max_pages or "unlimited",
                self.service_links_seeded,
                self.service_files_seeded,
            )
        if self.refresh_existing:
            logger.info(
                "REFRESH checked=%d | changed=%d | unchanged=%d | "
                "first-fingerprint=%d | failed=%d | media replaced=%d",
                self.refreshed_total,
                self.refreshed_changed,
                self.refreshed_unchanged,
                self.refreshed_baseline,
                self.refreshed_failed,
                self.media_replaced,
            )
            if self.refreshed_changed and self._refresh_log_path.exists():
                logger.info("Refresh change log: %s", self._refresh_log_path)
            self.refresh_index.save()
        logger.info("=" * 78)

        self.index.save()
        if self.export_excel:
            try:
                summary = [
                    ("new_pages_this_run", self.new_pages),
                    ("known_pages_skipped", skipped_known),
                    ("pages_translated_to_english", self.translated_pages),
                    ("pages_kept_original_language", self.translation_failed),
                    ("external_pages_queued", self.external_pages),
                    ("service_pages_queued", self.service_links_seeded),
                    ("service_files_queued", self.service_files_seeded),
                    ("requests_this_run", self.http.request_count),
                    ("failures_this_run", self.failures.count),
                    ("elapsed_seconds", f"{elapsed:.1f}"),
                ]
                if getattr(self.http, "proxy_pool", None):
                    stats = self.http.proxy_pool.stats()
                    summary.extend([
                        ("proxies_configured", stats["configured"]),
                        ("proxy_mode", stats["mode"]),
                        ("proxy_rotations", stats["rotations"]),
                    ])
                if self.refresh_existing:
                    summary.extend(
                        [
                            ("refreshed_pages_checked", self.refreshed_total),
                            ("refreshed_pages_changed", self.refreshed_changed),
                            ("refreshed_pages_unchanged", self.refreshed_unchanged),
                            ("refreshed_pages_baseline", self.refreshed_baseline),
                            ("refreshed_media_replaced", self.media_replaced),
                        ]
                    )
                targets = export_reports(
                    self.site,
                    since_iso=self.run_started_at,
                    crawl_summary=summary,
                )
                for target in targets:
                    logger.info("Spreadsheet ready: %s", target)
            except Exception as exc:
                logger.warning("Could not write spreadsheet export: %s", exc)


# ---------------------------------------------------------------------------
# Optional Playwright renderer (runs in a subprocess to stay thread-safe)
# ---------------------------------------------------------------------------

def _launch_render_browser(pw):
    """Start the most browser-like engine available.

    The bundled Chromium advertises ``HeadlessChrome`` and some WAFs (Akamai
    in front of dahd.gov.in) answer it with "Access Denied".  A real installed
    Chrome/Edge, still headless, gets the normal page.  The bundled Chromium
    stays as the last resort so the fallback never becomes unavailable.
    """
    errors: list[str] = []
    for channel in ("chrome", "msedge", None):
        kwargs: dict[str, Any] = {"headless": True}
        if channel:
            kwargs["channel"] = channel
        try:
            return pw.chromium.launch(**kwargs)
        except Exception as exc:
            errors.append(f"{channel or 'chromium'}: {type(exc).__name__}")
    raise RuntimeError("no browser could be started (" + ", ".join(errors) + ")")


# ----------------------------------------------------------------------
def render_html_to_stdout(url: str, timeout_ms: int,
                          proxy: Optional[str] = None, buttons: bool = True) -> int:
    """Render a JS-heavy page with Playwright and write UTF-8 HTML to stdout.

    Comprehensively interacts with the page to reveal ALL hidden content:
    - Clicks View/Show/Load/Expand/See More buttons and links
    - Expands accordions, tabs, dropdowns, details/summary
    - Scrolls to trigger lazy loading and infinite scroll
    - Works for ANY website - finds elements by behavior patterns, not specific classes
    - Reveals PDFs, documents, images, audio, video, tables, lists behind interactions
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.stderr.write(
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium\n"
        )
        return 2
    try:
        with sync_playwright() as pw:
            browser = _launch_render_browser(pw)
            try:
                context = browser.new_context(
                    service_workers="block",
                    # A normal desktop UA is required: the real browsers send
                    # "HeadlessChrome/..." by default and some WAFs block that.
                    user_agent=DEFAULT_USER_AGENT,
                    ignore_https_errors=True,
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                    proxy=playwright_proxy(proxy),
                )
                page = context.new_page()
                html = navigation.explore(page, url, timeout_ms, buttons, RobotsGate(DEFAULT_USER_AGENT).allowed)
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - depends on browser runtime
        sys.stderr.write(f"render failed: {type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.buffer.write(html.encode("utf-8", "replace"))
    sys.stdout.buffer.flush()
    return 0


def repair_empty_pages(
    site,
    categories,
    storage,
    robots: RobotsGate,
    limiter: RateLimiter,
    *,
    limit: int = 50,
    timeout: int = 45,
    stop_event: Optional[threading.Event] = None,
    proxy_pool: Optional[ProxyPool] = None,
) -> dict:
    """Re-render stored pages that have no title/text, in place.

    Only content fields are replaced.  The page id, category, source URL,
    collected timestamp and every downloaded media path are preserved, so
    nothing already collected is lost.  Progress is
    written to ``data/repair_progress.json`` so the web app can show it.
    """
    stats = {
        "found": 0, "attempted": 0, "repaired": 0,
        "still_empty": 0, "blocked": 0, "errors": 0,
    }
    progress_path = site.data_dir / "repair_progress.json"

    def write_progress() -> None:
        done = (stats["repaired"] + stats["still_empty"]
                + stats["blocked"] + stats["errors"])
        payload = {
            "total": stats["found"],
            "done": done,
            "repaired": stats["repaired"],
            "still_empty": stats["still_empty"],
            "blocked": stats["blocked"],
            "errors": stats["errors"],
            "percent": round(done * 100 / max(1, stats["found"]), 1),
            "updated_at": utc_now_iso(),
        }
        try:
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            progress_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    targets: list[tuple[str, dict]] = []
    for jsonl in sorted(site.categories_dir.glob("*/pages.jsonl")):
        folder = jsonl.parent.name
        try:
            with jsonl.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(record.get("title") or "").strip():
                        continue
                    if str(record.get("text") or "").strip():
                        continue
                    url = str(record.get("source_url") or "")
                    if not url.startswith(("http://", "https://")):
                        continue
                    targets.append((folder, record))
        except OSError:
            continue
    stats["found"] = len(targets)
    write_progress()
    if limit > 0:
        targets = targets[:limit]

    parser = Parser(site.host)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    creationflags = 0x08000000 if os.name == "nt" else 0

    for folder, old in targets:
        if stop_event is not None and stop_event.is_set():
            break
        url = str(old.get("source_url") or "")
        stats["attempted"] += 1
        try:
            if not robots.allowed(url):
                stats["blocked"] += 1
                write_progress()
                continue
            robots_delay = robots.crawl_delay(url)
            if robots_delay > limiter.delay:
                limiter.set_delay(min(20.0, robots_delay))
            limiter.wait(stop_event)
            proxy = proxy_pool.acquire() if proxy_pool else None
            cmd = [
                sys.executable, "-u", str(Path(__file__).resolve()),
                "--render-html", url, "--render-timeout", str(timeout),
            ]
            if proxy:
                cmd.extend(["--proxy", proxy])
            done = subprocess.run(
                cmd, capture_output=True, timeout=timeout + 45,
                env=env, creationflags=creationflags,
            )
            html_bytes = done.stdout or b""
            if done.returncode != 0 or len(html_bytes) < 500:
                stats["errors"] += 1
                if proxy and proxy_pool:
                    proxy_pool.mark_bad(proxy, "render failed")
                write_progress()
                continue
            text = html_bytes[:8000].decode("utf-8", "replace")
            if looks_blocked_page(text):
                stats["blocked"] += 1
                if proxy and proxy_pool:
                    proxy_pool.mark_bad(proxy, "rendered block page")
                write_progress()
                continue
            if proxy and proxy_pool:
                proxy_pool.mark_good(proxy)
            parsed = parser.parse(html_bytes, url)
            title = str(parsed.title or "").strip()
            body = str(parsed.text or "").strip()
            if not title and len(body) < 50:
                stats["still_empty"] += 1
                write_progress()
                continue

            record = dict(old)
            record.update({
                "title": parsed.title,
                "text": parsed.text,
                "date_time": parsed.date_time,
                "links": parsed.links,
                "image_url": [u for u, _ in parsed.images],
                "pdf_files": list(parsed.pdfs),
                "audio_files": list(parsed.audios),
                "video_files": list(parsed.videos),
                "document_files": list(parsed.documents),
                "language": parsed.language,
                "last_checked": utc_now_iso(),
                "content_hash": content_fingerprint(content_fields(parsed)),
            })
            record.pop("render_failed", None)
            record.pop("render_error", None)
            storage.upsert(record, folder)
            stats["repaired"] += 1
            logger.info("Repaired empty page: %s", url)
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("Could not repair %s: %s", url, exc)
        write_progress()

    write_progress()
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scraper.py",
        description=(
            "Crawl an entire website, extract text/media/links and store everything "
            "in per-category folders under <root-dir>/<site_name>."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    for option in ("buttons", "hyperlinks", "text"):
        parser.add_argument("--" + option, action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--formats", action="append", default=[], metavar="KIND:EXT,EXT")
    parser.add_argument("--url", help="Start URL, e.g. https://www.india.gov.in/")
    parser.add_argument(
        "--root-dir", default=DEFAULT_ROOT_DIR,
        help="Root folder that holds one folder per website",
    )
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Maximum NEW pages to crawl this run (0 = unlimited)",
    )
    parser.add_argument(
        "--delay", type=float, default=1.5,
        help="Minimum seconds between HTTP requests (politeness)",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Parallel worker threads (requests remain globally rate-limited)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from data/state.json (this is already the default)",
    )
    parser.add_argument(
        "--restart", action="store_true",
        help="Ignore saved state and the skip list: re-crawl every page",
    )
    parser.add_argument(
        "--incremental", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Skip pages already recorded in data/scraped_pages.xlsx and fetch "
            "only newly discovered pages (use --no-incremental for the old "
            "behaviour)"
        ),
    )
    parser.add_argument(
        "--excel", action=argparse.BooleanOptionalAction, default=True,
        help="Write data/scraped_pages.xlsx and data/new_pages.xlsx after each run",
    )
    parser.add_argument(
        "--export-only", action="store_true",
        help="Rebuild the Excel/CSV reports from existing JSONL files and exit",
    )
    parser.add_argument(
        "--no-js", action="store_true",
        help="Disable the Playwright fallback for JavaScript-rendered pages",
    )
    parser.add_argument(
        "--render-budget", type=int, default=0,
        help=(
            "Maximum number of JavaScript page renders per run "
            "(0 = unlimited). Rendering is serialised, one browser at a time."
        ),
    )
    parser.add_argument(
        "--download-youtube", action="store_true",
        help="Download YouTube/Vimeo videos with yt-dlp (off by default)",
    )
    parser.add_argument(
        "--ignore-crawl-delay", action="store_true",
        help="Do not raise --delay to the robots.txt Crawl-delay value",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help=(
            "Do not verify HTTPS certificates (only for sites whose "
            "certificate chain cannot be validated)"
        ),
    )
    parser.add_argument(
        "--strict-ssl", action="store_true",
        help=(
            "Never retry a failed certificate check without verification; "
            "fail instead (automatic fallback is on by default)"
        ),
    )
    parser.add_argument(
        "--no-language-switch", action="store_true",
        help=(
            "Do not switch the site to English when it offers a language "
            "choice; scrape the site's default language"
        ),
    )
    parser.add_argument(
        "--no-translate", action="store_true",
        help=(
            "Do not translate non-English pages; save their original text "
            "(translation is attempted by default, with a fallback to the "
            "original language when it fails)"
        ),
    )
    parser.add_argument(
        "--refresh-links", type=int, default=25,
        help=(
            "On incremental runs, re-check links on this many already-scraped "
            "hub pages (homepage/sections) without re-downloading media; "
            "0 disables it"
        ),
    )
    parser.add_argument("--rediscover", action="store_true",
                        help="Follow navigation from the seed through stored pages as well as new pages")
    parser.add_argument(
        "--refresh-existing", action="store_true",
        help=(
            "Re-fetch pages that are already stored, compare their content and "
            "rewrite only the records whose title/text/media list changed"
        ),
    )
    parser.add_argument(
        "--refresh-after", type=float, default=0.0, metavar="DAYS",
        help=(
            "With --refresh-existing: only refresh pages not checked in the "
            "last DAYS days (0 = every stored page)"
        ),
    )
    parser.add_argument(
        "--refresh-limit", type=int, default=0, metavar="N",
        help=(
            "With --refresh-existing: stop after refreshing N pages this run "
            "(0 = no cap); oldest-checked pages are refreshed first"
        ),
    )
    parser.add_argument(
        "--refresh-media", action="store_true",
        help=(
            "With --refresh-existing: re-download PDFs/documents/audio/video "
            "(and images of changed pages) and replace a file only when its "
            "bytes differ"
        ),
    )
    parser.add_argument(
        "--seed-file", metavar="PATH", default=None,
        help=(
            "Text file with one extra URL per line to enqueue (useful for "
            "pages hidden behind JS/CMS APIs); already-scraped URLs are skipped"
        ),
    )
    parser.add_argument(
        "--follow-external", action="store_true",
        help=(
            "Also follow hyperlinks to other websites (PDFs/documents linked "
            "from any page are always downloaded); off-site pages go to the "
            "'External Sites' category and stay on their own domain"
        ),
    )
    parser.add_argument(
        "--external-only", action="store_true",
        help=(
            "External web scrap: use the internal pages only to find off-site "
            "links; store nothing from the internal site, only the external "
            "websites it links to (implies --follow-external)"
        ),
    )
    parser.add_argument(
        "--external-max-pages", type=int, default=5000, metavar="N",
        help=(
            "Maximum off-site pages to queue this run (0 = unlimited); only "
            "applies when external following is enabled"
        ),
    )
    parser.add_argument(
        "--drain-only", action="store_true",
        help=(
            "Only fetch the URLs already in the saved queue; do not follow "
            "links to any new pages. Finishes a known backlog fast without "
            "expanding the crawl"
        ),
    )
    parser.add_argument(
        "--max-depth", type=int, default=3, metavar="N",
        help=(
            "How many link hops away from the starting site external pages may "
            "be followed (1 = only the directly linked portals)"
        ),
    )
    parser.add_argument(
        "--internal-depth", type=int, default=0, metavar="N",
        help=(
            "How many link levels inside the site to follow: 1 = only the start "
            "page and the links on it, 2 = also the links on those pages, "
            "0 = the whole site (default)"
        ),
    )
    parser.add_argument(
        "--follow-service-links",
        action=argparse.BooleanOptionalAction, default=None,
        help=(
            "Crawl the external ministry/state portals stored in harvested "
            "india.gov.in service records and download their files (enabled "
            "automatically by --follow-external)"
        ),
    )
    parser.add_argument(
        "--internal-first", action="store_true",
        help=(
            "Crawl every on-site page before any off-site page; the external "
            "queue is kept and only starts once internal pages are exhausted"
        ),
    )
    parser.add_argument(
        "--category-complete", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Complete each category fully before moving to the next: exhaust "
            "all pages within a category (following every link/button) before "
            "starting another category. Ensures deep category scraping like "
            "'Documents' with pagination and sub-pages."
        ),
    )
    parser.add_argument(
        "--include-service-pages", action="store_true",
        help=(
            "Also fetch the india.gov.in /services/details/... pages as real "
            "HTML into a 'Service Pages' category (normally skipped because "
            "--india-services already stored their text through the API)"
        ),
    )
    parser.add_argument(
        "--media", action=argparse.BooleanOptionalAction, default=True,
        help="Download images/PDFs/audio/video (use --no-media for text-only crawls)",
    )
    parser.add_argument(
        "--images", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Download image files; use --no-images to skip logos/banners and "
            "keep only PDFs/documents (much faster, same page text)"
        ),
    )
    parser.add_argument(
        "--image-formats", default="",
        help=(
            "Comma-separated image extensions to download, for example "
            "'png,jpg,svg'. Empty means every image type. Image URLs are always "
            "kept in the JSON even when their file is not downloaded."
        ),
    )
    parser.add_argument(
        "--pdf", action=argparse.BooleanOptionalAction, default=True,
        help="Download PDF files (--no-pdf keeps the links only)",
    )
    parser.add_argument(
        "--pdf-text", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Extract the text of downloaded PDFs into .txt files "
            "(--no-pdf-text keeps only the PDF files)"
        ),
    )
    parser.add_argument(
        "--audio", action=argparse.BooleanOptionalAction, default=True,
        help="Download audio files (--no-audio keeps the links only)",
    )
    parser.add_argument(
        "--video", action=argparse.BooleanOptionalAction, default=True,
        help="Download video files (--no-video keeps the links only)",
    )
    parser.add_argument(
        "--documents", action=argparse.BooleanOptionalAction, default=True,
        help="Download other documents such as doc/xls/zip (--no-documents keeps the links only)",
    )
    parser.add_argument(
        "--india-services", action="store_true",
        help=(
            "Harvest india.gov.in's ~13.6k service records from its JSON API "
            "(the pages a link crawler cannot see). Use with "
            "--url https://www.india.gov.in/"
        ),
    )
    parser.add_argument(
        "--india-services-limit", type=int, default=0, metavar="N",
        help="Stop the service API harvest after N new records (0 = all)",
    )
    parser.add_argument(
        "--india-services-refresh", action="store_true",
        help=(
            "Delete the stored service records first and rebuild them all from "
            "the CMS (use after a scraper upgrade)"
        ),
    )
    parser.add_argument(
        "--india-services-page-size", type=int, default=100, metavar="N",
        help="Records per service CMS request (max 100, default 100)",
    )
    parser.add_argument(
        "--state-every", type=int, default=STATE_SAVE_EVERY,
        help="Save resume state and category index every N pages",
    )
    parser.add_argument(
        "--max-file-mb", type=float, default=250.0,
        help="Skip media files larger than this many megabytes",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log level (the log file always gets DEBUG)",
    )
    parser.add_argument(
        "--proxy", action="append", default=[], metavar="URL",
        help=(
            "Proxy to use, e.g. http://user:pass@IP:PORT. Repeat for more than "
            "one. With no proxy, the machine's real connection is used."
        ),
    )
    parser.add_argument(
        "--proxy-file", metavar="PATH",
        help="Text file with one proxy per line (same format as --proxy)",
    )
    parser.add_argument(
        "--proxy-mode", default="rotate_on_block",
        choices=["rotate_on_block", "rotate_per_page", "sticky"],
        help="When to change proxy (default: rotate_on_block)",
    )
    parser.add_argument("--render-html", metavar="URL", help=argparse.SUPPRESS)
    parser.add_argument("--render-timeout", type=int, default=60, help=argparse.SUPPRESS)
    parser.add_argument("--repair-empty", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repair-limit", type=int, default=50, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    # Do this before any HTTP client is created: truststore makes Python use
    # the OS certificate store, which accepts the incomplete chains that many
    # government sites send.
    system_trust = install_system_trust()

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.render_html:
        return render_html_to_stdout(
            args.render_html,
            max(10000, args.render_timeout * 1000),
            proxy=args.proxy[0] if args.proxy else None, buttons=args.buttons,
        )
    if not args.url:
        if args.india_services:
            args.url = INDIA_SERVICES_SITE
        else:
            parser.error("--url is required")

    start_url = args.url.strip()
    if not re.match(r"^https?://", start_url, re.IGNORECASE):
        start_url = "https://" + start_url
    normalized = normalize_url(start_url)
    if not normalized:
        parser.error(f"invalid start URL: {args.url}")
    start_url = normalized

    delay = max(0.0, float(args.delay))
    workers = min(32, max(1, int(args.workers)))
    max_pages = max(0, int(args.max_pages))
    external_only = bool(args.external_only)
    follow_external = bool(args.follow_external) or external_only
    external_max_pages = max(0, int(args.external_max_pages))
    max_depth = max(0, int(args.max_depth))
    max_internal_depth = max(0, int(args.internal_depth))
    follow_service_links = args.follow_service_links
    if follow_service_links is None:
        follow_service_links = follow_external

    refresh_existing = bool(args.refresh_existing)
    refresh_after_days = max(0.0, float(args.refresh_after))
    refresh_limit = max(0, int(args.refresh_limit))
    refresh_media = bool(args.refresh_media)
    if refresh_existing and args.restart:
        parser.error("--refresh-existing cannot be combined with --restart")
    if refresh_existing and not args.incremental:
        parser.error(
            "--refresh-existing needs incremental mode (remove --no-incremental)"
        )

    site = SiteFolderManager(args.root_dir, start_url)
    site.ensure_layout()
    setup_logging(site.logs_dir, args.log_level)
    if not refresh_existing and (refresh_after_days or refresh_limit or refresh_media):
        logger.warning(
            "--refresh-after/--refresh-limit/--refresh-media are ignored without "
            "--refresh-existing."
        )
    logger.info("=" * 78)
    logger.info("%s v%s", APP_NAME, VERSION)
    logger.info("Start URL : %s", start_url)
    logger.info("Site      : %s  (%s)", site.site_name, site.website_name)
    logger.info("Folder    : %s", site.site_dir)
    logger.info(
        "Settings  : delay=%.1fs workers=%d max_pages=%s js=%s incremental=%s",
        delay, workers, max_pages or "unlimited", not args.no_js,
        bool(args.incremental) and not args.restart,
    )
    if args.insecure:
        logger.warning(
            "TLS       : certificate verification DISABLED (--insecure)"
        )
    else:
        logger.info(
            "TLS       : %s%s",
            "system certificate store" if system_trust else "bundled certificate store",
            " | automatic retry without verification for broken chains"
            if not args.strict_ssl else " | strict verification",
        )
    if args.media:
        kinds = [name for name, on in (
            ("images", args.images), ("pdf", args.pdf), ("audio", args.audio),
            ("video", args.video), ("documents", args.documents),
        ) if on]
        logger.info(
            "Downloads : %s (links are stored for every kind either way)",
            ", ".join(kinds) if kinds else "nothing - links only",
        )
        image_formats = sorted({
            ext.strip().lower().lstrip(".")
            for ext in (args.image_formats or "").split(",")
            if ext.strip()
        })
        if args.images and image_formats:
            logger.info("Images    : only %s files", ", ".join(image_formats))
    if max_internal_depth:
        logger.info(
            "Depth     : %d internal level(s) from the start page (0 = whole site)",
            max_internal_depth,
        )
    if follow_external or follow_service_links:
        logger.info(
            "External  : follow=%s service_links=%s max_external_pages=%s max_depth=%d",
            follow_external, follow_service_links,
            external_max_pages or "unlimited", max_depth,
        )
    if refresh_existing:
        logger.info(
            "Refresh   : existing pages, after=%s day(s), limit=%s, media=%s",
            f"{refresh_after_days:g}", refresh_limit or "unlimited", refresh_media,
        )
    if args.internal_first or args.include_service_pages:
        logger.info(
            "Order     : internal_first=%s service_pages=%s",
            bool(args.internal_first), bool(args.include_service_pages),
        )
    if args.drain_only:
        logger.info(
            "Drain     : only already-queued URLs will be fetched; "
            "no new pages will be discovered."
        )
    logger.info("=" * 78)

    settings = {
        "start_url": start_url,
        "domain": site.domain,
        "max_pages": max_pages,
        "delay": delay,
        "workers": workers,
        "resume": True,
        "incremental": bool(args.incremental),
        "excel_reports": bool(args.excel),
        "refresh_links": max(0, int(args.refresh_links)),
        "refresh_existing": refresh_existing,
        "refresh_after_days": refresh_after_days,
        "refresh_limit": refresh_limit,
        "refresh_media": refresh_media,
        "seed_file": args.seed_file or "",
        "download_media": bool(args.media),
        "download_images": bool(args.images),
        "image_formats": [
            ext.strip().lower().lstrip(".")
            for ext in (args.image_formats or "").split(",")
            if ext.strip()
        ],
        "download_pdf": bool(args.pdf),
        "download_audio": bool(args.audio),
        "download_video": bool(args.video),
        "download_documents": bool(args.documents),
        "follow_external": follow_external,
        "external_max_pages": external_max_pages,
        "max_depth": max_depth,
        "max_internal_depth": max_internal_depth,
        "external_only": external_only,
        "follow_service_links": follow_service_links,
        "internal_first": bool(args.internal_first),
        "include_service_pages": bool(args.include_service_pages),
        "category_complete": bool(args.category_complete),
        "drain_only": bool(args.drain_only),
        "india_services": bool(args.india_services),
        "india_services_limit": max(0, int(args.india_services_limit)),
        "india_services_page_size": max(1, int(args.india_services_page_size)),
        "india_services_refresh": bool(args.india_services_refresh),
        "respect_robots_txt": True,
        "verify_ssl": not args.insecure,
        "ssl_fallback": not args.strict_ssl,
        "prefer_english": not args.no_language_switch,
        "translate": not args.no_translate,
        "javascript_fallback": not args.no_js,
        "download_youtube": bool(args.download_youtube),
        "state_save_every": max(1, args.state_every),
        "max_file_mb": float(args.max_file_mb),
        "user_agent": DEFAULT_USER_AGENT,
    }
    site.write_config(settings)

    proxy_entries = list(args.proxy or [])
    if args.proxy_file:
        try:
            proxy_entries.extend(
                line.strip() for line in Path(args.proxy_file).read_text(
                    encoding="utf-8", errors="replace").splitlines()
            )
        except OSError as exc:
            logger.warning("Could not read --proxy-file: %s", exc)
    proxy_pool = ProxyPool(proxy_entries, args.proxy_mode)
    if proxy_pool:
        logger.info(
            "Network   : %d proxy/proxies, mode=%s (credentials hidden in logs)",
            len(proxy_pool.proxies), proxy_pool.mode,
        )
    else:
        logger.info("Network   : direct connection (no proxies configured)")

    stop_event = threading.Event()
    failures = FailureLog(site.failed_path)
    limiter = RateLimiter(delay)
    robots = RobotsGate(DEFAULT_USER_AGENT)
    http = HttpClient(
        user_agent=DEFAULT_USER_AGENT,
        limiter=limiter,
        robots=robots,
        failures=failures,
        stop_event=stop_event,
        verify_ssl=not args.insecure,
        ssl_fallback=not args.strict_ssl,
        proxy_pool=proxy_pool,
    )
    categories = CategoryManager(site)
    if args.repair_empty:
        repair_stats = repair_empty_pages(
            site,
            categories,
            Storage(site, categories),
            robots,
            limiter,
            limit=max(0, int(args.repair_limit)),
            timeout=max(10, int(args.render_timeout)),
            proxy_pool=proxy_pool,
        )
        logger.info("Empty-page repair finished: %s", json.dumps(repair_stats))
        if proxy_pool:
            logger.info("Proxy stats: %s", json.dumps(proxy_pool.stats()))
        return 1 if repair_stats["errors"] else 0
    index = ScrapedIndex(site)
    index.load()
    refresh_index = RefreshIndex(site)
    if refresh_index.load():
        logger.info(
            "Refresh index loaded: %d page(s) with a known content fingerprint.",
            refresh_index.count(),
        )

    if args.export_only:
        index.save()
        try:
            targets = export_reports(
                site,
                crawl_summary=[
                    ("mode", "export only (no crawling)"),
                    ("indexed_pages", index.count()),
                ],
            )
        except Exception as exc:
            logger.error("Export failed: %s", exc)
            return 1
        for target in targets:
            logger.info("Spreadsheet ready: %s", target)
        return 0

    if args.india_services:
        if refresh_existing:
            logger.warning(
                "--refresh-existing re-fetches page records, but service records "
                "come from the CMS API; rebuild those with "
                "--india-services --india-services-refresh instead."
            )
        if "india.gov.in" not in site.host:
            logger.warning(
                "The service API belongs to india.gov.in but --url host is %s.",
                site.host,
            )
        html_parser = Parser(site.host)  # noqa: F841 (kept for symmetry/logging)
        downloader: Optional[Downloader] = None
        if args.media:
            downloader = Downloader(
                http=http,
                site=site,
                categories=categories,
                failures=failures,
                stop_event=stop_event,
                max_file_bytes=max(1, int(args.max_file_mb * 1024 * 1024)),
                youtube_enabled=bool(args.download_youtube),
                pdf_text=bool(args.pdf_text),
            )
        storage = Storage(site, categories)
        run_started_at = utc_now_iso()
        started = time.monotonic()
        try:
            stats = harvest_india_services(
                site=site,
                categories=categories,
                storage=storage,
                index=index,
                http=http,
                downloader=downloader,
                page_size=max(1, int(args.india_services_page_size)),
                max_records=max(0, int(args.india_services_limit)),
                refresh=bool(args.india_services_refresh),
            )
        except KeyboardInterrupt:
            logger.warning("Service harvest interrupted; saving what was collected.")
            stats = {"added": 0, "known": 0, "duplicates": 0, "items": 0,
                     "total": 0, "failed_pages": 0, "images": 0}
        finally:
            storage.close()

        elapsed = time.monotonic() - started
        categories.recount()
        categories.write_index()
        index.save()
        totals = Counter()
        for info in categories.all():
            for key in ("pages", "images", "pdfs", "audio", "video", "documents"):
                totals[key] += info.counts[key]
        logger.info("=" * 78)
        logger.info("Service catalogue harvest finished for %s", site.website_name)
        logger.info(
            "API records=%d | new=%d | already known=%d | duplicates=%d | "
            "images saved=%d | failed API pages=%d",
            stats.get("total", 0), stats.get("added", 0), stats.get("known", 0),
            stats.get("duplicates", 0), stats.get("images", 0),
            stats.get("failed_pages", 0),
        )
        logger.info(
            "TOTAL        pages=%-6d images=%-6d pdfs=%-5d audio=%-4d video=%-4d docs=%-4d",
            totals["pages"], totals["images"], totals["pdfs"], totals["audio"],
            totals["video"], totals["documents"],
        )
        logger.info("Indexed pages total=%d | elapsed=%s", index.count(),
                    time.strftime("%H:%M:%S", time.gmtime(elapsed)))
        logger.info("=" * 78)

        if args.excel:
            try:
                targets = export_reports(
                    site,
                    since_iso=run_started_at,
                    crawl_summary=[
                        ("mode", "india.gov.in service API harvest"),
                        ("api_records", stats.get("total", 0)),
                        ("new_pages_this_run", stats.get("added", 0)),
                        ("already_known_skipped", stats.get("known", 0)),
                        ("requests_this_run", http.request_count),
                        ("failures_this_run", failures.count),
                        ("elapsed_seconds", f"{elapsed:.1f}"),
                    ],
                )
                for target in targets:
                    logger.info("Spreadsheet ready: %s", target)
            except Exception as exc:
                logger.warning("Could not write spreadsheet export: %s", exc)
        return 0

    html_parser = Parser(site.host)
    html_parser.buttons, html_parser.hyperlinks, html_parser.collect_text = args.buttons, args.hyperlinks, args.text
    downloader = Downloader(
        http=http,
        site=site,
        categories=categories,
        failures=failures,
        stop_event=stop_event,
        max_file_bytes=max(1, int(args.max_file_mb * 1024 * 1024)),
        youtube_enabled=bool(args.download_youtube),
        pdf_text=bool(args.pdf_text),
    )
    storage = Storage(site, categories)
    state = StateStore(site.state_path, site.site_name, start_url)
    crawler = Crawler(
        site=site,
        categories=categories,
        parser=html_parser,
        http=http,
        downloader=downloader,
        storage=storage,
        state=state,
        index=index,
        failures=failures,
        stop_event=stop_event,
        start_url=start_url,
        max_pages=max_pages,
        workers=workers,
        state_every=max(1, args.state_every),
        js_enabled=not args.no_js,
        restart=bool(args.restart),
        render_timeout=max(10, args.render_timeout),
        render_budget=max(0, int(args.render_budget)),
        honor_robots_delay=not bool(args.ignore_crawl_delay),
        incremental=bool(args.incremental),
        export_excel=bool(args.excel),
        refresh_links=max(0, int(args.refresh_links)),
        rediscover=args.rediscover,
        seed_file=args.seed_file,
        media_enabled=bool(args.media),
        download_images=bool(args.images),
        media_kinds=(
            ({"pdf"} if args.pdf else set())
            | ({"audio"} if args.audio else set())
            | ({"video"} if args.video else set())
            | ({"document"} if args.documents else set())
            | ({"image"} if args.images else set())
        ),
        image_formats={
            ext.strip().lower().lstrip(".")
            for ext in (args.image_formats or "").split(",")
            if ext.strip()
        },
        follow_external=follow_external,
        external_max_pages=external_max_pages,
        max_depth=max_depth,
        max_internal_depth=max_internal_depth,
        external_only=external_only,
        follow_service_links=follow_service_links,
        category_complete=args.category_complete,
        internal_first=bool(args.internal_first),
        include_service_pages=bool(args.include_service_pages),
        drain_only=bool(args.drain_only),
        refresh_existing=refresh_existing,
        refresh_after_days=refresh_after_days,
        refresh_limit=refresh_limit,
        refresh_media=refresh_media,
        refresh_index=refresh_index,
        prefer_english=not bool(args.no_language_switch),
        translate=not bool(args.no_translate),
    )

    for spec in args.formats:
        kind, _, extensions = spec.partition(":")
        crawler.formats[kind] = set(extensions.lower().split(","))

    def _handle_sigint(signum: int, frame: Any) -> None:
        if stop_event.is_set():
            logger.warning("Second Ctrl+C received -- forcing exit.")
            raise KeyboardInterrupt
        stop_event.set()
        logger.warning(
            "Ctrl+C received -- finishing the current request and saving state. "
            "Press Ctrl+C again to force quit."
        )

    for _sig_name in ("SIGINT", "SIGBREAK"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is None:
            continue
        try:
            signal.signal(_sig, _handle_sigint)
        except Exception:  # pragma: no cover - non-main thread / unsupported
            pass

    exit_code = 0
    try:
        crawler.run()
        if crawler.start_failed:
            logger.error(
                "The start page could not be fetched, so no data was scraped. "
                "See logs/failed_urls.txt for the exact error."
            )
            exit_code = 3
    except KeyboardInterrupt:
        exit_code = 130
        logger.warning("Forced exit; the last saved state can be resumed with --resume.")
    except SystemExit:
        raise
    except Exception as exc:
        exit_code = 1
        logger.exception("Fatal error: %s", exc)
    finally:
        storage.close()
        http.close()
        try:
            categories.write_index()
        except Exception:
            pass
        try:
            index.save()
        except Exception:
            pass

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

