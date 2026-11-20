"""Build the scraped output tree from the crawler's raw output.

Every scrape is delivered under ``Scraped_Data/<website_name>/``:

    categories/<category>/           the ONLY copy of pages, HTML and media
        html/  media/  pages.jsonl
    Json/Overall.json                the JSON viewer file
    Json/master.json                 every record, unchanged
    website_info.json
    scraping_summary.json
    logs/  data/  scraper/           written by the crawler

``Internal_Pages/`` and ``External_Pages/`` are no longer created: they were a
second copy of the same files and wasted disk space.  External pages already
live in ``categories/external_sites/``.  The original folder is always left
non-de-duplicated; the separate De-dupe page writes a de-duplicated copy to a
folder chosen by the user (see ``dedupe.py``).
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import template as tpl

# kind -> folder the crawler uses under categories/<cat>/media/
SOURCE_DIRS = {
    "image": "images",
    "pdf": "pdfs",
    "audio": "audio",
    "video": "video",
    "documents": "documents",
}

# kind -> record field that holds the downloaded relative paths
MEDIA_FIELDS = {
    "image": "image_paths",
    "pdf": "pdf_paths",
    "audio": "audio_paths",
    "video": "video_paths",
    "documents": "document_paths",
}

# output sub-folder for each kind (there is no Audio folder in the tree, so
# audio joins the other documents)
KIND_OUTPUT = {
    "image": "Images",
    "video": "Videos",
    "pdf": "Documents",
    "documents": "Documents",
    "audio": "Documents",
}

INTERNAL_BUCKETS = ("Home", "About", "Services", "Contact")
OTHER_BUCKET = "Other_Categories"
EXTERNAL_CATEGORY = "External Sites"

# Order used when matching a category: Contact is checked before Services so a
# "Contact Department" does not land in Services.
BUCKET_MATCH_ORDER = ("Home", "Contact", "About", "Services")

BUCKET_KEYWORDS = {
    "Home": ("home", "welcome", "landing", "front page", "main page"),
    "About": ("about", "who we are", "profile", "introduction", "overview",
              "history", "mission", "vision", "organisation", "organization"),
    "Services": ("service", "scheme", "program", "initiative", "facility",
                 "portal", "department", "division", "ministry"),
    "Contact": ("contact", "reach us", "get in touch", "enquiry", "inquiry",
                "help", "support", "feedback", "grievance"),
}

HASH_CACHE = "hash_cache.json"
# Only these folders are part of the packaged output.  categories/ is the
# crawler's raw (and only) store of pages, HTML and media; Json/ holds the two
# generated JSON files.  The old delivery roots are never written again.
OUTPUT_ROOTS = ("categories", "Json")
LEGACY_OUTPUT_ROOTS = ("Internal_Pages", "External_Pages")

# Keep the packaged JSON English-only even when the raw crawl (or an older
# run) still contains Hindi/Indic records.  The rules match scraper.py:
#   * a non-Latin script dominates the text (>= 40 letters, >= 35%), or
#   * there is a large non-Latin block even beside English boilerplate, or
#   * the page title itself is a full non-Latin translation.
# A short motto or a language switcher is small enough to keep.
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
    if not LANGUAGE_NAME_RE.search(block):
        return False
    scripts = sum(
        1 for script_range in INDIC_SCRIPT_RANGES
        if re.search(f"[{script_range}]", block)
    )
    return scripts >= 2


def _non_latin_block_total(text: str) -> int:
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


def is_non_english_title(title: str) -> bool:
    sample = (title or "")[:500]
    if not sample.strip():
        return False
    indic, other, latin = _script_counts(sample)
    if indic >= 20 and indic / max(1, indic + latin) >= 0.35:
        return True
    if other >= 20 and other / max(1, other + latin) >= 0.35:
        return True
    return False


def is_non_english_record(record: dict) -> bool:
    """True when a stored page is not English-only."""
    if is_non_english_title(record.get("title") or ""):
        return True
    sample = (record.get("text") or "")[:120000]
    if not sample.strip():
        return False
    indic, other, latin = _script_counts(sample)
    if indic >= 40 and indic / max(1, indic + latin) >= 0.35:
        return True
    if other >= 40 and other / max(1, other + latin) >= 0.35:
        return True
    if indic >= 200 and indic / max(1, indic + latin) >= 0.15:
        return True
    if other >= 200 and other / max(1, other + latin) >= 0.15:
        return True
    if _non_latin_block_total(sample) >= 200:
        return True
    return False


# ---------------------------------------------------------------- hashing ---
def _load_hash_cache(site: Path) -> dict:
    path = site / "data" / HASH_CACHE
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_hash_cache(site: Path, cache: dict) -> None:
    path = site / "data" / HASH_CACHE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def file_hash(path: Path, cache: dict | None = None) -> str:
    """sha256 of the file, cached on (mtime, size) so repeat runs are cheap."""
    try:
        st = path.stat()
    except OSError:
        return ""
    key = str(path)
    if cache is not None:
        entry = cache.get(key)
        if entry and entry.get("mtime") == st.st_mtime and entry.get("size") == st.st_size:
            return entry.get("sha", "")
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    digest = h.hexdigest()
    if cache is not None:
        cache[key] = {"mtime": st.st_mtime, "size": st.st_size, "sha": digest}
    return digest


# ------------------------------------------------------------ type toggles --
def enabled_kinds(config: dict) -> dict[str, list[str]]:
    """Return {kind: [formats]} for the toggles the user switched on."""
    types = config.get("types") or {}
    out: dict[str, list[str]] = {}
    for kind in SOURCE_DIRS:
        entry = types.get(kind)
        if isinstance(entry, bool):
            if entry:
                out[kind] = []
            continue
        if isinstance(entry, dict) and entry.get("on"):
            out[kind] = [str(f).lower().lstrip(".") for f in entry.get("formats") or []]
    return out


# ----------------------------------------------------------------- records --
def _host(url: str) -> str:
    host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _website_name(url: str) -> str:
    host = _host(url)
    if host == "pib.gov.in":
        return "PIB"
    if host == "india.gov.in" or host.endswith(".india.gov.in"):
        return "India.gov.in"
    return host or "Unknown"


# Windows refuses paths over 260 characters unless long-path support is turned
# on. Keep a little headroom so child files (Links/, HTML/, media) still fit.
MAX_PATH = 240


def _slug(name: str, limit: int = 80) -> str:
    """Filesystem-safe name, truncated with a short hash when over ``limit``.

    The hash keeps two long names that share a prefix from colliding after the
    truncation.
    """
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(name or "")).strip("_")
    cleaned = cleaned or "category"
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8]
    keep = max(1, limit - len(digest) - 1)
    return f"{cleaned[:keep].rstrip('_')}_{digest}"


def _fit_filename(folder: Path, name: str) -> str:
    """Shorten a file name so ``folder / name`` fits inside :data:`MAX_PATH`."""
    name = str(name or "file")
    limit = MAX_PATH - len(str(folder)) - 1
    if len(name) <= limit:
        return name
    suffix = Path(name).suffix
    stem = Path(name).stem or "file"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    keep = max(1, limit - len(suffix) - len(digest) - 1)
    return f"{stem[:keep].rstrip('_')}_{digest}{suffix}"


def dumps_json_records(records: list) -> str:
    """Pretty JSON array with the first ``{`` pulled onto the ``[`` line.

    The layout is the usual 2-space indent, but the array opens as ``[{``::

        [{
            "prid": "...",
            "title": "..."
          },
          {
            "prid": "...",
            "title": "..."
          }
        ]

    Records are never written on a single line.
    """
    text = json.dumps(records, ensure_ascii=False, indent=2)
    if text.startswith("[\n  {"):
        text = "[{" + text[len("[\n  {"):]
    return text


def write_json_records(path: Path, records: list[dict]) -> None:
    """Write a JSON array using :func:`dumps_json_records`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_json_records(records) + "\n", encoding="utf-8")


def read_records(site: Path) -> list[dict]:
    """Every stored page, de-duplicated by page id (or URL when there is none)."""
    seen = set()
    records = []
    for jsonl in sorted((site / "categories").rglob("pages.jsonl")):
        try:
            with jsonl.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = r.get("prid") or r.get("source_url") or ""
                    if key and key in seen:
                        continue
                    seen.add(key)
                    records.append(r)
        except OSError:
            continue
    return records


def in_window(record: dict, start: str, end: str) -> bool:
    """Date window for live scraping. Uses page date, else collected_at."""
    if not start and not end:
        return True
    stamp = (record.get("date_time") or "").strip()[:10]
    if len(stamp) != 10:
        stamp = (record.get("collected_at") or "").strip()[:10]
    if len(stamp) != 10:
        return True
    if start and stamp < start[:10]:
        return False
    if end and stamp > end[:10]:
        return False
    return True


def _is_external(record: dict) -> bool:
    return (record.get("category") or "") == EXTERNAL_CATEGORY


def _bucket_for(record: dict) -> str:
    """Map a crawler category to Home / About / Services / Contact / Other."""
    category = (record.get("category") or "").strip()
    section = (record.get("section") or "").strip()
    path = (urlsplit(record.get("source_url") or "").path or "/").rstrip("/").lower()
    if path in ("", "/index.html", "/index.htm", "/index.php", "/home",
                "/home.html", "/home.htm"):
        return "Home"

    # Use the first real path segment (skip a trailing index page name) so a
    # page like /catalogue/category/books_1/index.html is not treated as Home
    # just because its file name is "index.html".
    segments = [s for s in path.split("/")
                if s and s not in ("index.html", "index.htm", "index.php")]
    first = segments[0] if segments else ""
    haystack = f"{category} {section} {first}".lower()
    for bucket in BUCKET_MATCH_ORDER:
        if any(word in haystack for word in BUCKET_KEYWORDS[bucket]):
            return bucket
    return OTHER_BUCKET


def _internal_folder(record: dict, delivery: Path | None = None) -> tuple[str, str]:
    """Return (folder relative to Internal_Pages, json file name)."""
    bucket = _bucket_for(record)
    if bucket in INTERNAL_BUCKETS:
        return bucket, f"{bucket.lower()}.json"
    category = (record.get("category") or "Uncategorized").strip() or "Uncategorized"
    limit = 80
    if delivery is not None:
        # Path is <delivery>/Internal_Pages/Other_Categories/<slug>/<slug>.json
        # and the folder also holds Links/ and HTML/ files.
        base = str(delivery / "Internal_Pages" / OTHER_BUCKET)
        for_children = MAX_PATH - len(base) - 29
        for_json = (MAX_PATH - len(base) - 7) // 2
        limit = max(16, min(80, for_children, for_json))
    slug = _slug(category, limit)
    return f"{OTHER_BUCKET}/{slug}", f"{slug}.json"


def _json_record(record: dict, text_on: bool, html_rel: str = "") -> dict:
    return {
        "prid": record.get("prid", ""),
        "title": record.get("title", ""),
        "text": record.get("text", "") if text_on else "",
        "date_time": record.get("date_time", ""),
        "source_url": record.get("source_url", ""),
        "website_name": _website_name(record.get("source_url", "")),
        "category": record.get("category", ""),
        "categories": record.get("categories") or [],
        "section": record.get("section", ""),
        "image_url": record.get("image_url") or [],
        "pdf_files": record.get("pdf_files") or [],
        "audio_files": record.get("audio_files") or [],
        "video_files": record.get("video_files") or [],
        "document_files": record.get("document_files") or [],
        "links": record.get("links") or [],
        "language": record.get("language", ""),
        "collected_at": record.get("collected_at", ""),
        "html_file": html_rel,
    }


def _overall_record(record: dict, text_on: bool) -> dict:
    """The shape the JSON template sees (same fields as before)."""
    return {
        "prid": record.get("prid", ""),
        "title": record.get("title", ""),
        "text": record.get("text", "") if text_on else "",
        "date_time": record.get("date_time", ""),
        "source_url": record.get("source_url", ""),
        "website_name": _website_name(record.get("source_url", "")),
        "image_url": record.get("image_url") or [],
        "pdf_files": record.get("pdf_files") or [],
        "audio_files": record.get("audio_files") or [],
        "video_files": record.get("video_files") or [],
        "document_files": record.get("document_files") or [],
        "language": record.get("language", ""),
        "collected_at": record.get("collected_at", ""),
    }


# ------------------------------------------------------------------ media ---
def _rel_path(site: Path, rel) -> Path | None:
    if not rel:
        return None
    path = site / str(rel).replace("\\", "/")
    return path if path.is_file() else None


def _media_plan(site: Path, record: dict, dest_root: Path,
                pdf_text: bool = True) -> list[dict]:
    plan = []
    seen: set[str] = set()
    for kind, field in MEDIA_FIELDS.items():
        for rel in record.get(field) or []:
            src = _rel_path(site, rel)
            if src is None:
                continue
            key = str(src)
            if key in seen:
                continue
            seen.add(key)
            kind_dir = dest_root / KIND_OUTPUT[kind]
            plan.append({"src": src, "kind": kind,
                         "dst": kind_dir / _fit_filename(kind_dir, src.name)})
    # PDF text sidecars are documents too, but only when the job asked for them.
    if pdf_text:
        for rel in record.get("pdf_text_paths") or []:
            src = _rel_path(site, rel)
            if src is None or str(src) in seen:
                continue
            seen.add(str(src))
            doc_dir = dest_root / "Documents"
            plan.append({"src": src, "kind": "documents",
                         "dst": doc_dir / _fit_filename(doc_dir, src.name)})
    return plan


def _html_plan(site: Path, record: dict, dest_root: Path) -> dict | None:
    src = _rel_path(site, record.get("html_path"))
    name = f"{_slug(record.get('prid') or 'page')}.html"
    if src is not None:
        return {"src": src, "dst": dest_root / "HTML" / name, "generated": False}
    # Older records have no stored HTML; write a readable text snapshot so the
    # HTML folder is still complete.
    return {"record": record, "dst": dest_root / "HTML" / name, "generated": True}


def _copy_file(src: Path, dst: Path, cache: dict | None = None) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dst.exists():
            if (dst.stat().st_size == src.stat().st_size
                    and file_hash(dst, cache) == file_hash(src, cache)):
                return "skipped"
            digest = file_hash(src, cache)
            dst = dst.with_name(_fit_filename(
                dst.parent, f"{dst.stem}__{digest[:8]}{dst.suffix}"))
            if dst.exists():
                if (dst.stat().st_size == src.stat().st_size
                        and file_hash(dst, cache) == file_hash(src, cache)):
                    return "skipped"
        shutil.copy2(src, dst)
        return "copied"
    except OSError:
        return "error"


def _write_generated_html(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    title = html_lib.escape(str(record.get("title") or ""))
    text = html_lib.escape(str(record.get("text") or ""))
    url = html_lib.escape(str(record.get("source_url") or ""))
    path.write_text(
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body>\n"
        f"<!-- generated from stored text; raw HTML was not saved -->\n"
        f"<h1>{title}</h1>\n<p><a href=\"{url}\">{url}</a></p>\n"
        f"<pre>{text}</pre>\n</body></html>\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------- skeleton ---
def _make_page_dirs(folder: Path, *, external: bool) -> None:
    for name in ("Images", "Videos", "Documents"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    (folder / "HTML").mkdir(parents=True, exist_ok=True)
    if not external:
        (folder / "Links").mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ build ---
def build(site: Path, config: dict,
          date_window: tuple[str, str] | None = None,
          delivery: Path | None = None,
          collected_after: str = "",
          start_url: str = "",
          prefer_english: bool = False,
          extra_skipped: int = 0) -> dict:
    """Create the fixed output tree and return run statistics."""
    delivery = delivery or site
    delivery.mkdir(parents=True, exist_ok=True)

    kinds = enabled_kinds(config)
    text_on = bool((config.get("types") or {}).get("text", True))
    pdf_entry = (config.get("types") or {}).get("pdf")
    pdf_text_on = not (isinstance(pdf_entry, dict)
                       and pdf_entry.get("pdf_text") is False)
    window = date_window or ("", "")

    master: list[dict] = []
    internal: list[dict] = []
    external: list[dict] = []
    skipped_non_english = int(extra_skipped or 0)
    translated_pages = 0
    kept_original_language = 0
    for record in read_records(site):
        if collected_after and (record.get("collected_at") or "") < collected_after:
            continue
        if (window[0] or window[1]) and not in_window(record, *window):
            continue
        # Non-English pages are translated by the crawler when possible and
        # saved in their original language when not, so never drop a record
        # here unless the caller explicitly asks for English-only output.
        if prefer_english and is_non_english_record(record):
            skipped_non_english += 1
            continue
        if record.get("translated"):
            translated_pages += 1
        elif record.get("translation_failed") or is_non_english_record(record):
            kept_original_language += 1
        master.append(record)
        (external if _is_external(record) else internal).append(record)

    # A page with neither title nor text is almost always a JavaScript-only
    # page that could not be rendered.  Count it so the job can warn instead
    # of reporting a silent success.
    empty_records = sum(
        1 for record in master
        if not str(record.get("title") or "").strip()
        and not str(record.get("text") or "").strip()
    )
    render_failed_records = sum(
        1 for record in master if record.get("render_failed")
    )
    empty_where = [
        record for record in master
        if not str(record.get("title") or "").strip()
        and not str(record.get("text") or "").strip()
    ]
    empty_render_failed = sum(1 for record in empty_where if record.get("render_failed"))
    empty_external = sum(1 for record in empty_where if _is_external(record))

    # ---- group records by the crawler's own category --------------------
    # categories/<category>/ is the only content store.  These counts are the
    # coverage report: they let the UI show "42 / 42 categories scraped" and
    # let a reviewer check that every category's pages and files were kept.
    category_records: dict[str, list[dict]] = defaultdict(list)
    hosts: dict[str, int] = defaultdict(int)
    for record in master:
        category = (str(record.get("category") or "").strip() or "Uncategorized")
        category_records[category].append(record)
        if _is_external(record):
            hosts[_host(record.get("source_url", "")) or "external"] += 1

    category_coverage: dict[str, dict] = {}
    for category, rows in sorted(category_records.items()):
        category_coverage[category] = {
            "pages": len(rows),
            "images": sum(len(r.get("image_paths") or []) for r in rows),
            "pdfs": sum(len(r.get("pdf_paths") or []) for r in rows),
            "audio": sum(len(r.get("audio_paths") or []) for r in rows),
            "video": sum(len(r.get("video_paths") or []) for r in rows),
            "documents": sum(len(r.get("document_paths") or []) for r in rows),
            "html": sum(1 for r in rows if r.get("html_path")),
            "complete": all(
                bool(str(r.get("title") or "").strip()
                     or str(r.get("text") or "").strip())
                for r in rows
            ),
        }
    try:
        coverage_path = site / "data" / "category_coverage.json"
        coverage_path.parent.mkdir(parents=True, exist_ok=True)
        coverage_path.write_text(json.dumps({
            "generated_at": _now(),
            "categories": len(category_coverage),
            "coverage": category_coverage,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    link_counts = {
        "image": sum(len(r.get("image_url") or []) for r in master),
        "pdf": sum(len(r.get("pdf_files") or []) for r in master),
        "audio": sum(len(r.get("audio_files") or []) for r in master),
        "video": sum(len(r.get("video_files") or []) for r in master),
        "documents": sum(len(r.get("document_files") or []) for r in master),
        "links": sum(len(r.get("links") or []) for r in master),
    }

    # Nothing is copied here: the crawler already stored every page, HTML file
    # and media file under categories/.  Counts describe what is on disk, and
    # report how many files would be written by a separate de-duped export.
    media_fields = (
        ("image", "image_paths"), ("pdf", "pdf_paths"),
        ("audio", "audio_paths"), ("video", "video_paths"),
        ("documents", "document_paths"),
    )
    per_kind: Counter = Counter()
    for kind, field in media_fields:
        per_kind[kind] = sum(len(r.get(field) or []) for r in master)
    copied = sum(per_kind.values())
    html_files = sum(1 for r in master if r.get("html_path"))
    skipped = errors = duplicates = bytes_copied = 0
    duplicate_examples: list[dict] = []

    # ---- phase 5: JSON folder, website info and summary ----------------
    json_dir = delivery / "Json"
    json_dir.mkdir(parents=True, exist_ok=True)
    for name in ("data", "logs", "scraper"):
        (delivery / name).mkdir(parents=True, exist_ok=True)
    master_path = json_dir / "master.json"
    write_json_records(master_path, master)

    overall_rows = [_overall_record(r, text_on) for r in master]
    template_text = (config.get("json_template") or "").strip()
    rendered = tpl.render(overall_rows, template_text) if template_text else overall_rows
    overall = json_dir / "Overall.json"
    write_json_records(overall, rendered)

    start = start_url or _start_url(site)
    website_info = {
        "website_name": _host(start) or site.name,
        "display_name": _display_name(start) or site.name,
        "start_url": start,
        "domain": _host(start),
        "generated_at": _now(),
        "pages": len(master),
        "internal_pages": len(internal),
        "external_pages": len(external),
        "external_websites": [
            {"host": host, "pages": pages} for host, pages in sorted(hosts.items())
        ],
        "categories": {
            name: data["pages"] for name, data in category_coverage.items()
        },
        "category_coverage": category_coverage,
        "media": dict(per_kind),
        "html_files": html_files,
        "duplicates_removed": 0,
        "deduplicated": False,
    }
    (delivery / "website_info.json").write_text(
        json.dumps(website_info, ensure_ascii=False, indent=2), encoding="utf-8")

    notice = ""
    if (config.get("modes") or {}).get("external") and not external:
        notice = (
            "No external websites have been found on this site, so External "
            "web scrap saved nothing. Your own pages are never stored in this "
            "mode."
        )
    elif not master:
        notice = (
            "No pages were saved. The start page could not be read or the site "
            "returned no crawlable content. Check logs/failed_urls.txt and "
            "logs/scraper.log for the exact error."
        )

    summary = {
        "website_name": website_info["website_name"],
        "start_url": start,
        "scraped_at": _now(),
        "modes": config.get("modes") or {},
        "pages": len(master),
        "internal_pages": len(internal),
        "external_pages": len(external),
        "categories": website_info["categories"],
        "categories_scraped": len(category_coverage),
        "category_coverage": category_coverage,
        "external_websites": website_info["external_websites"],
        "media_found": link_counts,
        "media_in_categories": dict(per_kind),
        "media_copied": 0,
        "media_skipped": skipped,
        "duplicates_removed": 0,
        "duplicate_examples": duplicate_examples,
        "non_english_skipped": skipped_non_english,
        "translated_pages": translated_pages,
        "kept_original_language": kept_original_language,
        "empty_records": empty_records,
        "empty_render_failed": empty_render_failed,
        "empty_external": empty_external,
        "render_failed_records": render_failed_records,
        "files_copied": 0,
        "bytes_copied": bytes_copied,
        "html_files": html_files,
        "text_included": text_on,
        "deduplicated": False,
        "legacy_folders": [
            name for name in LEGACY_OUTPUT_ROOTS if (site / name).is_dir()
        ],
        "notice": notice,
    }
    summary_path = delivery / "scraping_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "delivery": str(delivery),
        "records": len(rendered),
        "master_records": len(master),
        "internal_records": len(internal),
        "external_records": len(external),
        "overall_json": str(overall),
        "master_json": str(master_path),
        "website_info": str(delivery / "website_info.json"),
        "summary_json": str(summary_path),
        "json_bytes": overall.stat().st_size,
        "master_bytes": master_path.stat().st_size,
        "media": {
            "copied": copied,
            "skipped": skipped,
            "duplicates": duplicates,
            "errors": errors,
            "bytes": bytes_copied,
            "per_kind": dict(per_kind),
            "duplicate_examples": duplicate_examples,
        },
        "links": link_counts,
        "types": {k: len(v) for k, v in kinds.items()},
        "text": text_on,
        "notice": notice,
        "non_english_skipped": skipped_non_english,
        "translated_pages": translated_pages,
        "kept_original_language": kept_original_language,
        "empty_records": empty_records,
        "empty_render_failed": empty_render_failed,
        "empty_external": empty_external,
        "render_failed_records": render_failed_records,
        "categories_scraped": len(category_coverage),
        "category_coverage": category_coverage,
        "legacy_folders": [
            name for name in LEGACY_OUTPUT_ROOTS if (site / name).is_dir()
        ],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _start_url(site: Path) -> str:
    state_file = site / "data" / "state.json"
    if state_file.is_file():
        try:
            return json.loads(state_file.read_text(encoding="utf-8")).get("start_url", "")
        except Exception:
            pass
    return ""


def _display_name(url: str) -> str:
    host = _host(url)
    return host[:1].upper() + host[1:] if host else ""


# ------------------------------------------------------- rebuild from master --
def rebuild_from_master(master_path: Path, template_text: str, out_path: Path,
                        prefer_english: bool = False) -> dict:
    """Turn master.json into any other JSON shape - no crawling involved."""
    if not master_path.is_file():
        raise ValueError("master.json not found for this site - run a scrape first.")
    try:
        master = json.loads(master_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"master.json is damaged: {exc}")
    if not isinstance(master, list):
        raise ValueError("master.json should contain a list of records.")

    if prefer_english:
        master = [r for r in master if not is_non_english_record(r)]
    records = [_overall_record(r, True) for r in master]
    rendered = tpl.render(records, template_text) if (template_text or "").strip() else records
    write_json_records(out_path, rendered)
    return {"file": str(out_path), "records": len(rendered),
            "bytes": out_path.stat().st_size}


# ---------------------------------------------------------------- duplicates --
def _output_files(delivery: Path):
    for name in OUTPUT_ROOTS:
        root = delivery / name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                yield path


def duplicate_report(delivery: Path, limit_groups: int = 200) -> dict:
    """Find files inside the delivered output whose content is identical."""
    if not delivery.is_dir():
        return {"groups": [], "duplicate_files": 0, "wasted_bytes": 0, "scanned": 0}
    by_size: dict[int, list[Path]] = defaultdict(list)
    scanned = 0
    for f in _output_files(delivery):
        scanned += 1
        try:
            by_size[f.stat().st_size].append(f)
        except OSError:
            pass

    cache: dict = {}
    groups = []
    duplicate_files = 0
    wasted = 0
    for size, files in by_size.items():
        if len(files) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for f in files:
            by_hash[file_hash(f, cache)].append(f)
        for digest, same in by_hash.items():
            if len(same) < 2 or not digest:
                continue
            same.sort(key=lambda p: (len(p.name), p.name))
            duplicate_files += len(same) - 1
            wasted += size * (len(same) - 1)
            if len(groups) < limit_groups:
                groups.append({
                    "sha": digest[:12],
                    "size": size,
                    "keep": str(same[0].relative_to(delivery)).replace("\\", "/"),
                    "duplicates": [str(p.relative_to(delivery)).replace("\\", "/")
                                   for p in same[1:]],
                })
    groups.sort(key=lambda g: g["size"] * len(g["duplicates"]), reverse=True)
    return {
        "groups": groups,
        "duplicate_files": duplicate_files,
        "wasted_bytes": wasted,
        "scanned": scanned,
    }


def remove_duplicates(delivery: Path) -> dict:
    """Never modify the original scrape folder.

    De-duplication is done by the separate De-dupe page, which writes a new
    copy to a folder chosen by the user.  That keeps the non-de-duplicated
    output available for hand-off and makes the change lossless (the manifest
    can restore every original file name and category).
    """
    report = duplicate_report(delivery)
    return {
        "removed": 0,
        "freed": 0,
        "errors": 0,
        "groups": len(report.get("groups") or []),
        "message": (
            "The original folder is kept unchanged. Use the De-dupe page to "
            "create a separate de-duplicated copy in a folder you choose."
        ),
    }


def folder_report(delivery: Path) -> list[dict]:
    """One row per category plus the generated folders.

    Only the top level of ``categories/`` is listed, so a site with 40
    categories shows 40 rows instead of thousands of hash folders.
    """
    rows: list[dict] = []

    def add(folder: Path, label: str) -> None:
        count = 0
        total = 0
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            count += 1
            try:
                total += path.stat().st_size
            except OSError:
                pass
        if count:
            rows.append({"path": label, "files": count, "bytes": total})

    categories = delivery / "categories"
    if categories.is_dir():
        for folder in sorted(categories.iterdir()):
            if folder.is_dir():
                add(folder, f"categories/{folder.name}")
    for name in ("Json", "data", "logs", "scraper"):
        folder = delivery / name
        if folder.is_dir():
            add(folder, name)
    return rows
