# Scraping Web App

A local web app and command-line crawler for scraping public websites, including
all pages, menus, images, PDFs, documents, audio and video. It keeps the original
scrape untouched and can make a separate de-duplicated copy from the **De-dupe**
tab.

Includes:

- Internal, external and full-site crawling
- All-language crawling: English switch first, translation for remaining pages,
  original text kept if translation fails
- Global proxy rotation plus optional per-job override
- Resume, stop, re-run and partial re-check of saved pages
- Separate de-duplicated output with a manifest and full restore
- JSON viewer with readable one-field-per-line formatting
- Repair tool for pages that were saved empty

> Use this only on public pages you are allowed to crawl. The crawler reads
> `robots.txt`, keeps per-site delays and never bypasses logins, paywalls or a
> deliberate no-bot denial. CAPTCHA solving is **not** implemented in this
> version.

---

## 1. Download

### Option A — Git clone

Install Git, then run:

```bash
git clone https://github.com/Chan367/web-scraping-tool.git
cd web-scraping-tool
```

Later, to update to the newest version:

```bash
git pull
```

### Option B — Download ZIP

1. Open <https://github.com/Chan367/web-scraping-tool>
2. Click **Code** → **Download ZIP**
3. Extract the ZIP somewhere, for example `Desktop\web-scraping-tool`
4. Open a terminal inside that folder

---

## 2. Install

Python **3.10 or newer** is required.

### Windows

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If `python` is not found, install Python from <https://www.python.org/downloads/>
and tick **Add Python to PATH**, or use `py -3` instead of `python`.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### JavaScript-heavy pages (optional)

Some pages are empty in plain HTML. For those, install Playwright:

```bash
pip install playwright
python -m playwright install chromium
```

The renderer uses installed **Chrome**, then **Edge**, then the bundled Chromium.

---

## 3. Run the web app

### Easiest on Windows

Double-click **`start_webapp.bat`**.

### Terminal

```bat
python webapp/app.py
```

or on macOS/Linux:

```bash
python3 webapp/app.py
```

Then open:

<http://127.0.0.1:5000>

Useful environment variables:

| Variable | Meaning | Example |
|---|---|---|
| `PORT` | Change the web port | `set PORT=8080` then run the app |
| `NO_BROWSER` | Do not auto-open a browser | `set NO_BROWSER=1` |
| `SCRAPER_MAX_CONCURRENT_JOBS` | Max simultaneous jobs (default `2`) | `set SCRAPER_MAX_CONCURRENT_JOBS=2` |

All local state is created automatically under `webapp/data/`. That folder is
not committed to GitHub, so each user starts with a clean job list.

---

## 4. Use the web app

### Scrape tab

1. Enter a full URL, for example `https://www.example.gov.in/`
2. Choose a storage folder and click **Check**
3. Pick what to download (text, images, PDFs, audio, video, documents)
4. Choose the crawl mode:
   - **Internal web scrap** — only this website
   - **External web scrap** — only websites it links to
   - **Full web scraping** — both
5. Set advanced limits if needed (max pages, depth, delay, workers, file size)
6. Click **Start scrape**

PDFs and other documents are saved with the table's **Topic** text. If there is
no Topic column, the page title + original file name is used. Duplicate names
get `-1`, `-2`, and so on. Pressing **Resume** on a stopped job also renames the
files it already downloaded before continuing.

A new URL creates its own folder:

```text
<storage folder>\Scraped_Data\<website_name>\
```

### Live scraping tab

Same as above, plus a start date, optional end date and repeat interval. Live
runs store their own output under `<site>/live/<job id>/`.

### Jobs tab

Shows live progress, logs, folder counts and JSON output.

- **Stop** — saves state and stops after the current request
- **Resume** — continues without fetching saved pages again
- **Run again** — fetches only new or changed pages
- **Start over** — moves the old folder to a backup and crawls fresh
- **Repair empty pages** — re-renders records that were saved with no title/text
- **View JSON** — opens `Json/Overall.json` in a readable viewer

At most **2 jobs run at once** by default.

### De-dupe tab

The original scrape is never modified. This tab lists finished/stopped sites and
lets you create a separate de-duplicated copy:

1. Click **De-dupe**
2. Choose the destination parent folder
3. The result is written to:

```text
<chosen path>\<site>_deduped\
  Files\<kind>\<hash>\<hash>.<ext>
  categories\
  Json\
  dedupe_manifest.json
  dedupe_report.json
```

The manifest maps the original path, category, original name, source URL, hash,
stored path and size. It supports **Restore-full** back to
`<parent>\<site>_full`.

---

## 5. Proxy rotation

The proxy framework is ready, but the list is empty by default, so jobs use the
computer's real IP.

### Global list — applies to all jobs

On the **Scrape** tab, open the **Proxy rotation (all jobs)** card:

- Paste one proxy per line, for example
  `http://user:pass@1.2.3.4:8080`
- Choose a mode:
  - **Rotate when blocked** (recommended) — switch after `403`, `429`, `503`,
    a proxy failure or a rendered block page
  - **Rotate on every page**
  - **Sticky** — one proxy for the whole job
- Click **Test** to check the list
- Click **Save**, or **Clear list** to go back to the real IP

The saved list lives in `webapp/data/settings.json`, which is not committed.

### Per-job override

Under **Advanced limits** in either Scrape or Live scraping:

- Leave the proxy box empty to use the global list
- Paste proxies to override the list for that job
- Choose **Real IP — ignore global list** to force the real connection

Proxy credentials are masked in logs and in logged job commands.

### Command line

```bash
python scraper.py --url https://example.com ^
  --proxy http://user:pass@1.2.3.4:8080 ^
  --proxy-mode rotate_on_block
```

Or keep proxies in a text file, one per line:

```bash
python scraper.py --url https://example.com --proxy-file proxies.txt
```

Do not commit files containing real proxy credentials.

---

## 6. Command-line crawler

The web app is only a wrapper around `scraper.py`. You can run the crawler
directly:

```bat
python scraper.py --url https://www.example.gov.in/ --root-dir .\Scraped_Data
```

See all options:

```bat
python scraper.py --help
```

Common flags:

| Flag | Meaning |
|---|---|
| `--max-pages N` | Stop after N pages (`0` = all) |
| `--internal-depth N` | How many internal link levels to follow |
| `--follow-external` | Also crawl linked external sites |
| `--delay N` | Seconds between requests |
| `--workers N` | Parallel workers |
| `--proxy URL` | Add a proxy; repeat for more |
| `--proxy-file PATH` | Read proxies from a file |
| `--proxy-mode MODE` | `rotate_on_block`, `rotate_per_page` or `sticky` |
| `--no-translate` | Save pages in their original language |
| `--refresh-existing` | Re-check already stored pages |

---

## 7. Output folders

A new scrape contains only these items:

```text
Scraped_Data\<website_name>\
  categories\                         the single source of truth
    <category>\
      pages.jsonl
      media\
    external_sites\
  Json\
    Overall.json                      the JSON viewer file
    master.json                       every raw record
  data\                               state, indexes, caches, coverage
  logs\                               crawler logs and failures
  scraper\                            crawler copy + saved config
  scraping_summary.json
  website_info.json
```

`Internal_Pages/` and `External_Pages/` are **not** created. External pages live
in `categories/external_sites/`. Raw HTML snapshot files are **not** saved either;
the JSON record plus downloaded media are the deliverable.

---

## 8. Troubleshooting

- **The page is empty in the JSON.** Install Playwright and Chromium, then run
  **Repair empty pages** on that job.
- **TLS / certificate error.** The app normally uses the Windows/OS certificate
  store and retries broken chains safely. Use `--strict-ssl` only if you want
  failures instead.
- **A job will not start.** Only 2 jobs run at once by default. Stop one or
  raise `SCRAPER_MAX_CONCURRENT_JOBS`.
- **Proxy test fails.** Check the format `scheme://user:pass@IP:PORT`, whether
  the proxy allows your target port, and whether the credentials are correct.
- **Port 5000 is busy.** Set `PORT=8080` before starting the app.
- **The website blocks / bans the crawl (HTTP 403, 429, 503 or an "Access Denied"
  page).** The crawler now handles this on its own: it pauses *all* workers
  (30 s, then 60 s, 120 s ... up to 15 min, or the server's `Retry-After`),
  slows the delay down, and retries the page afterwards instead of dropping it.
  Look for `Block suspected ... Pausing ALL requests` in the log. A single
  forbidden page (403 while other pages still load) is skipped without pausing.
  If bans keep coming back:
  - raise **Delay** (2-3 s) and lower **Workers** (1-2) in Advanced;
  - the web app never uses a delay below `0.5` s (`SCRAPER_MIN_DELAY`, set `0` to
    turn the floor off);
  - run from your own computer / home or office connection - cloud servers'
    IPs are blocked by most government sites;
  - add proxies (section 5) as a last resort.
- **`ModuleNotFoundError: translator` / `tqdm`.** You are not running from the
  project folder or did not install the requirements. Run
  `pip install -r requirements.txt` and start with `python webapp/app.py`.

---

## 9. Repository layout

```text
scraper.py              main crawler / renderer / repair engine
rename_downloads.py     topic-based file renaming (used by Resume)
cleanup_originals.py    removes verified duplicate originals after renaming
translator.py           translation helper
requirements.txt        Python dependencies
start_webapp.bat        one-click Windows launcher
webapp/
  app.py                Flask web server and API
  jobs.py               job queue, limits, repair worker
  runner.py             builds and redacts scraper commands
  paths.py              storage and proxy settings
  packager.py           builds the final output tree
  dedupe.py             separate de-dupe / restore engine
  template.py           JSON sample handling
  static/               HTML, CSS, JavaScript
```

Runtime data, scraped websites, virtual environments and proxy credentials are
excluded by `.gitignore`.

## Navigation, collection, and re-checks

Internal mode stays on the starting site. External mode walks that site to
discover off-site destinations but saves only external results. Full mode saves
both. Internal depth and external hop depth are separate limits; zero means no
depth limit. With **Complete each category** enabled, the scheduler waits for
the active category's workers, their downloads, and newly queued children.
The log names each category as it starts. Category discovery uses real menu
destinations (including nested menus and query routes) and headed lists in
sections. Shared destinations are visited once, even if several menus link to them.

`robots.txt` redirects are followed, so sites that move their policy file from
`example.com/robots.txt` to `www.example.com/robots.txt` are not mistaken for
blocked origins. A missing or WAF-blocked (4xx) robots file means "no extra
restrictions"; only a 5xx/unreachable file disables crawling for that origin.

**Follow hyperlinks** and **Explore interactive controls** can be switched
independently of body text and file downloads. `hyperlinks.py` reads links,
pagination and embedded viewers; `navigation.py` discovers button routes and
explores dynamic controls. `crawl_modes.py` defines the three traversal policies;
`crawl_queue.py` schedules their categories. Browser discovery is bounded by
the per-page timeout and the optional browser-rendered page budget (0 = no
page-count cap). It skips form submission and
cross-origin subresources; a page that depends on them may yield fewer results.

**Follow navigation through previously saved pages** (on for web jobs, CLI
`--rediscover`) traces links again from the starting page, including through
known pages at any permitted depth. New pages are collected without duplicating
existing records. Use **Re-check Stored Pages** to update existing page records
and their attachments too.

**Resume** restores queued work for the same crawl mode. **Re-check Stored Pages** selects
existing category records by age and count, compares their content and links,
and follows newly discovered children. File verification uses saved ETag or
Last-Modified validators when available. Without validators the server must
send the bytes for comparison. Source URLs remain URLs; document paths and
display filenames are stored separately.

Run the offline regression sites (requires dependencies and a Playwright Chromium
installation) with `python tests/test_crawl.py -v`. They use temporary output
directories, two local hostnames, and the actual scraper CLI.
