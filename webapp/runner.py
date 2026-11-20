"""Run scraper.py as a child process and report progress."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import paths

CREATE_GROUP = 0x00000200 if os.name == "nt" else 0


def _scraper_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def proxy_args(config: dict | None = None) -> list[str]:
    """Build scraper proxy flags from per-job config + global defaults.

    A per-job empty list is an intentional override: that job uses the real IP
    even when a global proxy list is configured.
    """
    limits = (config or {}).get("limits") or {}
    defaults = paths.proxy_settings(mask=False)
    mode_raw = str(limits.get("proxy_mode") or "")
    if mode_raw == "real_ip":
        raw = []
        mode = "rotate_on_block"
    else:
        raw = limits.get("proxies") if "proxies" in limits else defaults["proxies"]
        mode = mode_raw or defaults["proxy_mode"]
    if isinstance(raw, str):
        raw = [line.strip() for line in raw.splitlines()]
    proxies = []
    for entry in raw or []:
        text = str(entry or "").strip()
        if text and text not in proxies:
            proxies.append(text)
    if mode not in paths.PROXY_MODES:
        mode = defaults["proxy_mode"]
    out = ["--proxy-mode", mode]
    for proxy in proxies:
        out.extend(["--proxy", proxy])
    return out


def redact_command(cmd: list[str]) -> str:
    """Return a log-safe command string with proxy credentials hidden."""
    out = []
    hide = False
    for part in cmd:
        if hide:
            out.append(paths.mask_proxy(part))
            hide = False
        else:
            out.append(part)
            if part == "--proxy":
                hide = True
    return " ".join(out)


def discover_links(url: str, include_external: bool, limit: int = 400,
                   config: dict | None = None) -> list[str]:
    """Fetch one page and return its links (used by overview mode).

    Plain HTTP first.  If that yields almost nothing (a JavaScript-only page),
    the page is rendered with the same Playwright fallback scraper.py uses and
    the links are read from the rendered HTML.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except Exception:
        return []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    pargs = proxy_args(config)
    proxy = pargs[pargs.index("--proxy") + 1] if "--proxy" in pargs else None
    try:
        resp = requests.get(
            url, headers=headers, timeout=30, verify=False,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        soup = None

    links = _links_from_soup(soup, url, include_external, limit) if soup is not None else []
    if len(links) >= 3:
        return links

    rendered = _render_html(url, proxy=proxy)
    if rendered:
        soup = BeautifulSoup(rendered, "lxml")
        more = _links_from_soup(soup, url, include_external, limit)
        if len(more) > len(links):
            return more
    return links


def _links_from_soup(soup, url: str, include_external: bool, limit: int) -> list[str]:
    host = (urlsplit(url).hostname or "").lower()
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        absolute = urljoin(url, href)
        if absolute in seen:
            continue
        scheme = urlsplit(absolute).scheme
        if scheme not in ("http", "https"):
            continue
        if not include_external and host not in (urlsplit(absolute).hostname or "").lower():
            continue
        seen.add(absolute)
        out.append(absolute)
        if len(out) >= limit:
            break
    return out


def _render_html(url: str, timeout: int = 45, proxy: str | None = None) -> str:
    """Render a JS-heavy page through scraper.py --render-html."""
    cmd = [sys.executable, str(paths.SCRAPER), "--render-html", url,
           "--render-timeout", str(timeout)]
    if proxy:
        cmd.extend(["--proxy", proxy])
    kwargs: dict = {"capture_output": True, "timeout": timeout + 40,
                    "env": _scraper_env()}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        done = subprocess.run(cmd, **kwargs)
    except Exception:
        return ""
    if done.returncode != 0 or len(done.stdout or b"") < 500:
        return ""
    return (done.stdout or b"").decode("utf-8", "replace")


def build_command(url: str, config: dict, seed_file: Path | None = None,
                  root_dir: Path | None = None) -> list[str]:
    modes = config.get("modes") or {}
    limits = config.get("limits") or {}
    types = config.get("types") or {}

    cmd = [
        sys.executable, "-u", str(paths.SCRAPER),
        "--url", url,
        "--root-dir", str(root_dir or paths.SCRAPED_DIR),
        "--delay", str(float(limits.get("delay", 1.0))),
        "--workers", str(int(limits.get("workers", 4))),
        "--max-pages", str(int(limits.get("max_pages", 0) or 0)),
        "--max-file-mb", str(float(limits.get("max_file_mb", 250) or 250)),
        "--state-every", "5",
        "--log-level", "INFO",
    ]

    # --- crawl scope -------------------------------------------------------
    # internal = this website only, external = only the other websites it links
    # to (its own pages are used for link discovery only), full = both.
    # deep/overview are kept so jobs created before the rename still run.
    scope = "internal"
    if modes.get("full"):
        scope = "full"
    elif modes.get("external"):
        scope = "external"
    legacy_overview = bool(modes.get("overview")) and scope == "internal"

    if scope == "full":
        depth = int(limits.get("max_depth", 0) or 0) or 50
        ext_pages = int(limits.get("external_max_pages", 0) or 0)
        cmd += ["--follow-external", "--external-max-pages", str(ext_pages),
                "--max-depth", str(depth)]
    elif scope == "external":
        depth = int(limits.get("max_depth", 0) or 0) or 50
        ext_pages = int(limits.get("external_max_pages", 0) or 0)
        cmd += ["--external-only", "--follow-external",
                "--external-max-pages", str(ext_pages), "--max-depth", str(depth)]
    else:
        cmd += ["--max-depth", str(int(limits.get("max_depth", 3) or 3))]

    # How far inside the site to walk: 0 = whole site.  In external mode this
    # is how much of the site is checked for off-site links.  Legacy overview
    # is fixed at one level and uses the seed file below.
    if not legacy_overview:
        internal_depth = int(limits.get("internal_depth", 0) or 0)
        if internal_depth > 0:
            cmd += ["--internal-depth", str(internal_depth)]

    if legacy_overview and seed_file is not None:
        cmd += ["--seed-file", str(seed_file), "--drain-only"]

    # --- re-check pages that are already stored ---------------------------
    refresh = config.get("refresh") or {}
    if refresh.get("on"):
        cmd += ["--refresh-existing",
                "--refresh-after", str(float(refresh.get("after_days") or 0) or 0),
                "--refresh-limit", str(int(refresh.get("limit") or 0) or 0)]
        if refresh.get("media"):
            cmd.append("--refresh-media")

    # --- media toggles -----------------------------------------------------
    # Only the switched-on kinds are downloaded. The JSON keeps every link
    # either way, so a text-only run still records the PDF/audio/video URLs.
    on = {k for k in ("image", "pdf", "audio", "video", "documents")
          if _is_on(types.get(k))}
    if not on:
        cmd.append("--no-media")
    else:
        if "image" not in on:
            cmd.append("--no-images")
        if "pdf" not in on:
            cmd.append("--no-pdf")
        if "audio" not in on:
            cmd.append("--no-audio")
        if "video" not in on:
            cmd.append("--no-video")
        if "documents" not in on:
            cmd.append("--no-documents")

    # Image format chips: only the selected extensions are downloaded.  If the
    # Images card is on but no format is selected, keep the URLs and download
    # no image files (same result as turning Images off).
    if "image" in on:
        image_entry = types.get("image")
        formats = []
        if isinstance(image_entry, dict):
            formats = [str(f).strip().lower() for f in (image_entry.get("formats") or []) if str(f).strip()]
        if formats:
            cmd += ["--image-formats", ",".join(dict.fromkeys(formats))]
        else:
            cmd.append("--no-images")

    # PDF text sidecars are separate from the PDF download itself: the PDF card
    # carries its own toggle. Off = save the PDF file but no extracted .txt.
    pdf_entry = types.get("pdf")
    if isinstance(pdf_entry, dict) and pdf_entry.get("pdf_text") is False:
        cmd.append("--no-pdf-text")

    # --- proxy rotation -----------------------------------------------------
    cmd.extend(proxy_args(config))

    return cmd


def _is_on(entry) -> bool:
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, dict):
        return bool(entry.get("on"))
    return False


class ScrapeRun:
    """One scraper.py invocation."""

    def __init__(self, url: str, config: dict, site: Path, log_path: Path):
        self.url = url
        self.config = config
        self.site = site
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.stopped = False
        self.exit_code: int | None = None
        self.started_at: float | None = None
        self.seed_file: Path | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        modes = self.config.get("modes") or {}
        if modes.get("overview"):
            links = discover_links(
                self.url,
                bool(modes.get("external") or modes.get("full")),
                config=self.config,
            )
            self.seed_file = self.site / "data" / "overview_seed.txt"
            self.seed_file.parent.mkdir(parents=True, exist_ok=True)
            self.seed_file.write_text("\n".join([self.url] + links), encoding="utf-8")

        cmd = build_command(self.url, self.config, self.seed_file,
                            root_dir=self.site.parent)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("a", encoding="utf-8", errors="replace")
        self._log_handle.write("\n" + "=" * 78 + "\n")
        self._log_handle.write(time.strftime("RUN STARTED %Y-%m-%d %H:%M:%S\n"))
        self._log_handle.write(redact_command(cmd) + "\n")
        self._log_handle.flush()

        self.proc = subprocess.Popen(
            cmd,
            cwd=str(paths.ROOT),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            env=_scraper_env(),
            creationflags=CREATE_GROUP,
        )
        self.started_at = time.time()

    def stop(self, wait: float = 12.0) -> None:
        self.stopped = True
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGINT)
        except Exception:
            pass
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.3)
        try:
            self.proc.terminate()
        except Exception:
            pass

    def wait(self) -> int:
        if not self.proc:
            return -1
        self.exit_code = self.proc.wait()
        try:
            self._log_handle.write(time.strftime("RUN FINISHED %Y-%m-%d %H:%M:%S")
                                   + f" (exit {self.exit_code})\n")
            self._log_handle.flush()
            self._log_handle.close()
        except Exception:
            pass
        return self.exit_code

    # -- progress ----------------------------------------------------------
    def progress(self) -> dict:
        state_file = self.site / "data" / "state.json"
        data = {
            "visited": 0,
            "queued": 0,
            "total": 0,
            "percent": 0.0,
            "completed": False,
            "elapsed": 0,
            "running": bool(self.proc and self.proc.poll() is None),
        }
        if self.started_at:
            data["elapsed"] = int(time.time() - self.started_at)
        if state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                visited = len(state.get("visited") or [])
                queued = len(state.get("queue") or [])
                total = visited + queued
                data.update({
                    "visited": visited,
                    "queued": queued,
                    "total": total,
                    "percent": round(visited / total * 100, 1) if total else 0.0,
                    "completed": bool(state.get("completed")),
                    "updated_at": state.get("updated_at", ""),
                })
            except Exception:
                pass
        return data


def tail(path: Path, lines: int = 200) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            content = fh.readlines()
        return "".join(content[-lines:])
    except OSError:
        return ""
