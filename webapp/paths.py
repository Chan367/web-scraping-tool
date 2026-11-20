"""Shared paths and helpers for the scraping web app."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

WEBAPP = Path(__file__).resolve().parent
ROOT = WEBAPP.parent
SCRAPER = ROOT / "scraper.py"
DATA_DIR = WEBAPP / "data"
STATIC_DIR = WEBAPP / "static"
JOBS_FILE = DATA_DIR / "jobs.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

# Default place for new scrapes: <project>/Scraped_Data/<website_name>/...
# The user can point this at another existing folder from the UI; the chosen
# root is saved in data/settings.json and reused for later jobs.
DEFAULT_OUTPUT_ROOT = ROOT
SCRAPED_DIR = ROOT / "Scraped_Data"

# Extensions shown as chips in the UI for each download toggle.
MEDIA_FORMATS = {
    "image": ["png", "jpeg", "jpg", "webp", "jfif", "gif", "svg", "bmp", "ico",
              "avif", "tif", "tiff", "apng"],
    "pdf": ["pdf"],
    "audio": ["mp3", "wav", "ogg", "m4a", "aac", "flac", "wma", "opus", "amr"],
    "video": ["mp4", "webm", "mkv", "avi", "mov", "m4v", "flv", "wmv", "mpg", "mpeg", "3gp"],
    "documents": ["doc", "docx", "ppt", "pptx", "xls", "xlsx", "zip", "csv", "txt", "rtf",
                  "odt", "ods", "odp", "epub", "json", "xml"],
}


# ------------------------------------------------------------- settings ---
def _load_settings() -> dict:
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_settings(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def resolve_path(raw) -> Path:
    """Turn user input into an absolute path (relative paths use the project)."""
    text = str(raw or "").strip().strip('"')
    path = Path(text).expanduser() if text else ROOT
    if not path.is_absolute():
        path = ROOT / path
    try:
        return path.resolve()
    except OSError:
        return path


def output_root() -> Path:
    """Folder the user chose for website folders (default: project root)."""
    raw = _load_settings().get("output_root")
    return resolve_path(raw) if raw else DEFAULT_OUTPUT_ROOT


def set_output_root(raw) -> Path:
    """Remember a new storage folder and return its absolute path."""
    path = resolve_path(raw)
    data = _load_settings()
    data["output_root"] = str(path)
    _save_settings(data)
    return path


def scraped_dir(root: Path | None = None) -> Path:
    """Where website folders live: <chosen folder>/Scraped_Data/."""
    base = Path(root) if root else output_root()
    return base / "Scraped_Data"


# --------------------------------------------------------------- proxies ---
PROXY_MODES = ("rotate_on_block", "rotate_per_page", "sticky")


def mask_proxy(raw: str) -> str:
    """Hide credentials and most of the host when showing a proxy in the UI."""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(text)
        host = parts.hostname or ""
        hidden = (host[:3] + "***") if len(host) > 3 else "***"
        return f"{parts.scheme}://{hidden}:{parts.port}"
    except Exception:
        return "***"


def proxy_settings(mask: bool = True) -> dict:
    """Global proxy defaults used by every new job."""
    data = _load_settings()
    raw = data.get("proxies") or []
    if isinstance(raw, str):
        raw = [line.strip() for line in raw.splitlines()]
    proxies = [str(p).strip() for p in raw if str(p).strip()]
    mode = str(data.get("proxy_mode") or "rotate_on_block")
    if mode not in PROXY_MODES:
        mode = "rotate_on_block"
    return {
        "proxies": [mask_proxy(p) for p in proxies] if mask else proxies,
        "proxy_mode": mode,
        "has_proxies": bool(proxies),
    }


def set_proxy_settings(proxies, mode: str = "rotate_on_block") -> dict:
    """Save the global proxy list and rotation mode."""
    if isinstance(proxies, str):
        proxies = [line.strip() for line in proxies.splitlines()]
    clean = []
    for raw in proxies or []:
        text = str(raw or "").strip()
        if text and text not in clean:
            clean.append(text)
    if mode not in PROXY_MODES:
        mode = "rotate_on_block"
    data = _load_settings()
    data["proxies"] = clean
    data["proxy_mode"] = mode
    _save_settings(data)
    return proxy_settings(mask=True)


def storage_info() -> dict:
    root = output_root()
    return {
        "output_root": str(root),
        "scraped_dir": str(scraped_dir(root)),
        "default_root": str(DEFAULT_OUTPUT_ROOT),
    }


def output_root_error(path: Path) -> str:
    """Return a human message when *path* cannot be used, else ''."""
    if not path.exists():
        return f"Folder not found: {path}"
    if not path.is_dir():
        return f"That path is a file, not a folder: {path}"
    if not is_writable(path):
        return f"Folder found, but it is not writable: {path}"
    return ""


def is_writable(path: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=str(path), prefix=".write_test_", delete=True):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ dirs ---
def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    SCRAPED_DIR.mkdir(parents=True, exist_ok=True)
    root = output_root()
    if root.is_dir():
        scraped_dir(root).mkdir(parents=True, exist_ok=True)


def site_name(url: str) -> str:
    """Same naming the crawler uses: india_gov_in."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
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


def pretty_name(url: str) -> str:
    """India.gov.in style label."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "Unknown"
    return host[:1].upper() + host[1:]


def site_path(name: str) -> Path:
    """Find an existing folder for one site (read-only lookup).

    Searches the chosen storage folder first, then the default
    ``Scraped_Data`` and finally legacy site folders at the project root.
    """
    for base in (scraped_dir(), SCRAPED_DIR):
        candidate = base / name
        if candidate.is_dir():
            return candidate
    legacy = ROOT / name
    if legacy.is_dir() and (legacy / "categories").is_dir():
        return legacy
    return scraped_dir() / name


def site_dir(url: str, root: Path | None = None) -> Path:
    """Folder a *new* scrape uses: <chosen folder>/Scraped_Data/<site_name>/."""
    return scraped_dir(root) / site_name(url)


def all_site_dirs() -> list[Path]:
    """Every site folder we know about: chosen root, default, then legacy."""
    found: dict[str, Path] = {}
    for base in (scraped_dir(), SCRAPED_DIR, ROOT):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or not (child / "categories").is_dir():
                continue
            found.setdefault(child.name, child)
    return list(found.values())


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"
