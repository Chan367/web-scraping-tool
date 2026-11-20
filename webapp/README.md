# Scraping Web App

A local web UI around `scraper.py`. It does not re-implement the crawler — it runs
`scraper.py` as a child process, tracks its progress, then builds the delivery
folder and the JSON files from whatever the crawler saved.

For download, installation and full setup instructions, see the root
[`README.md`](../README.md).

## Start it

Double-click **`start_webapp.bat`** in the project root, or:

```
python webapp\app.py
```

The browser opens at <http://127.0.0.1:5000>. Change the port with `PORT=8080`,
or stop the auto-open with `NO_BROWSER=1`.

## The four tabs

### Scrape
- **Website URL** — a new domain gets its own folder, named after the host
  (`https://www.example.gov.in/` → `example_gov_in/`). The dropdown above the
  input lists sites you already scraped, so you can continue one.
- **Storage folder** — the existing folder where the whole website folder is
  created. Type a path (for example `C:\Users\you\Desktop\web_scraping`) and
  click **Check**: a found folder is saved and reused, a missing path shows
  *Folder not found*. The website folder itself is
  `<storage folder>\Scraped_Data\<website_name>\`.
- **What to collect** — six categories (`text`, `image`, `pdf`, `audio`,
  `video`, `documents`). Turn one on and its formats appear as chips; only the
  chips left blue are **downloaded**. The JSON always keeps every link the
  crawler found — switching Video off removes the video files, not the video
  URLs. Only the Text toggle changes the JSON (it drops the page body).
  The PDF card has a second toggle, **Save extracted .txt file**: on (default)
  writes a `.txt` beside every downloaded PDF and copies it into `Documents/`;
  off downloads the PDF alone and creates no text file.
- **Crawl mode** — what is scraped:
  - `internal` (**Internal web scrap**) — only your own website, every menu,
    category and the pages inside them. External websites are never touched.
  - `external` (**External web scrap**) — only the other websites your site
    links to. Your own pages are read just to find those links; none of your
    own pages or files are saved. If the site has no external links, the job
    shows *"No external websites have been found…"*.
  - `full` (**Full web scraping**) — both: the whole internal site plus every
    external website it links to.
  Legacy jobs created before the rename still run: `deep` = internal, and
  `overview` = the start page plus its direct links only.
- **Re-check stored pages** — optional. Re-fetches pages that are already saved
  and rewrites only the ones whose title/text/file list changed. `after_days`
  limits it to pages not checked recently.
- **Advanced limits** — `max pages` stops after N saved pages (`0` = all);
  `pages deep` is how many link levels inside your own site to open (`0` = the
  whole site, `1` = the start page + its links, `2` = also their links);
  `external depth` is how many hops into an external website to follow and
  `max external pages` caps how many external pages are saved (both `0` = no
  limit; they only appear in External/Full modes). `delay`, `workers` and
  `max file size` control speed and safety. Proxy settings can be overridden
  here; leaving them empty uses the global list on the Scrape tab.
- **Proxy rotation** — the **Proxy rotation (all jobs)** card on the Scrape tab
  stores the shared proxy list. An empty list means the real IP is used. The
  default mode is **rotate when blocked**. Credentials are masked in logs.
- **JSON format** — paste one sample object. Every page is written in that
  shape. See below.
- **Output** — every scrape is delivered into a fixed tree under
  `Scraped_Data/<website_name>/`. There is nothing to configure; see *Output*
  below.

### Live scraping
Same toggles, plus a start date, an optional end date and a check interval.
Every cycle it crawls for new pages and — if *Re-check stored pages* is on —
updates the pages that changed. Leave the end date empty to watch forever.
Its output goes to its own `<site>/live/<job id>/` folder, never into the normal
scrape output.

### Jobs
Jobs are grouped by status with a short summary at the top; running and waiting
jobs come first. Each card shows the real progress from `<site>/data/state.json`
(pages done, remaining, percent), the folder report and the log.

- **Stop** — finishes the current request, saves state, then stops.
- **Resume** — continues from the exact page where the job stopped; the saved
  queue and visited list are reused, so nothing is fetched twice.
- **Run again** — for a finished job: fetch only new or changed pages.
- **Start over** — moves the saved site folder to `webapp/data/backups` and
  crawls from page one. Use this when the previous run used the wrong language
  or settings.
- **Remove** — removes the job from the list (the scraped files are kept).
- **Create another JSON** from the saved `master.json`.
- **View JSON** — opens `Json/Overall.json` (or another JSON file from this
  job) in a formatted, indented view, one field per line, so records are easy
  to check without leaving the app.

The **Folder report** dropdown stays open while the Jobs list refreshes, so you
can read the file counts without it closing every couple of seconds.

### De-dupe
The original scrape is never changed. This tab creates a separate de-duplicated
copy under a destination you choose, writes `dedupe_manifest.json` and
`dedupe_report.json`, and can restore the full copy later.

## Language

The crawler **saves every page** and tries to make the text English.

Before crawling it fetches the start page and looks for a language switch — an
`English` link, `hreflang="en"`, `?lang=en`, or a `/lang/en` endpoint. If the
site defaults to another language, it follows the switch first and shares the
resulting session cookie with every worker. Each external website is primed the
same way the first time it is reached.

Some sites still serve Hindi, Tamil, Telugu, … after the switch. When a page is
predominantly non-English, the crawler translates its title and text to English
with Google's public translate endpoint and saves the English version. The
original title/text are kept in `original_title` / `original_text`, and
`original_language` records what the page was written in. Non-English menu
labels are translated too, so category folders read in English.

**If translation fails** (no network, rate limit, service down), the page is
saved exactly as it was found — in Hindi or any other language. No page, image,
PDF or document is skipped because of its language.

The log reports the result:

```
Language : translated N non-English page(s) to English.
Language : kept the original language for N page(s) (translation unavailable).
```

The same counts appear in `scraping_summary.json` as `translated_pages` and
`kept_original_language`, and on the job card as **Translated to English** and
**Kept original language**.

Use `--no-translate` to save every page in its original language without trying
to translate it. Use `--no-language-switch` to keep the site's default language.

Old output from earlier runs is left as it was. If you want the older pages
translated too, run the job again (or use **Start over** for a clean folder).

## The JSON sample

Paste an object (or an array with one object). Each key is filled from the
page record:

```json
{
  "sno": 1,
  "title": "title",
  "page": "{{source_url}}",
  "images": [],
  "pdfs": [],
  "date": "date_time",
  "site": "website_name"
}
```

Rules:
- A bare field name (`"title"`) is replaced with the value.
- `{{field}}` works inside longer strings: `"{{title}} — {{date_time}}"`.
- An empty array (`"images": []`) is filled with the matching list field.
- A number under a key like `sno`/`id`/`no` becomes the running row number.
- Friendly aliases work: `url`/`link` → `source_url`, `date` → `date_time`,
  `images` → `image_url`, `pdfs` → `pdf_files`, `site` → `website_name`.
- Unknown keys are left as written, so extra constants survive.
- Use **Preview with sample data** before starting a long run.

Available fields: `prid`, `title`, `text`, `date_time`, `source_url`,
`website_name`, `image_url`, `pdf_files`, `language`, `collected_at`.

## Output

Every new scrape is delivered under
`<storage folder>/Scraped_Data/<website_name>/` (the default storage folder is
the project root). The tree is fixed — there is nothing else to configure:

```
Scraped_Data/<website_name>/
  categories/                 the ONLY content store (full, not de-duplicated)
    <category>/
      pages.jsonl
      html/
      media/
    external_sites/
  Json/
    Overall.json              every page in your JSON sample shape
    master.json               every raw record exactly as scraped
  data/                       crawler state, indexes, caches, coverage
  logs/                       crawler console output
  scraper/                    the crawler copy + saved config
  scraping_summary.json
  website_info.json
```

- `Internal_Pages/` and `External_Pages/` are no longer created. External pages
  live in `categories/external_sites/`.
- The original scrape is left non-de-duplicated. Use the **De-dupe** tab to
  create a separate de-duplicated copy somewhere else.
- `website_info.json` summarises the site; `scraping_summary.json` summarises
  the run (pages, media, translations, notice).
- Sites scraped by an older version of the app keep their existing folder at the
  project root; they are reused in place and never moved.

Live jobs keep their own output under `<site>/live/<job id>/` with the same
tree, and only contain records that live job collected itself.

### master.json — one file, many JSONs

`master.json` is the complete raw record for every page, with all fields the
crawler stored (`prid`, `title`, `text`, `date_time`, `source_url`,
`image_url`, `image_paths`, `pdf_files`, `pdf_paths`, `audio_*`, `video_*`,
`links`, `language`, `collected_at`, `content_hash`, …).

Any job in the Jobs tab has **Create another JSON**: paste a different sample
shape, give it a name, and it writes `<name>.json` from `master.json` instantly
— no re-crawling. That is how you get several JSON deliverables from one scrape.

## De-dupe

The original scrape is never modified. The **De-dupe** tab writes a separate
copy to a destination you choose:

```text
<chosen path>\<site>_deduped\
  Files\<kind>\<hash>\<hash>.<ext>
  categories\
  Json\
  dedupe_manifest.json
  dedupe_report.json
```

`dedupe_manifest.json` records original path, category, original name, source
URL, hash, stored path and size, and supports **Restore-full**.

## Repeat protection

- The crawler keeps `data/scraped_index.json` and skips pages it already has.
  Re-running a site only fetches what is new.
- `data/state.json` resumes an interrupted crawl.
- Only one job per site can run at a time.
- At most 2 jobs run at once by default (`SCRAPER_MAX_CONCURRENT_JOBS`).

## HTTPS certificates (TLS)

Many government sites send an incomplete certificate chain. A browser accepts
it, but Python's bundled certificate list can reject it with
`unable to get local issuer certificate`, which used to stop a crawl before a
single page was saved.

- The crawler now uses the **operating system certificate store** through
  `truststore` (listed in `requirements.txt`), so it trusts the same
  authorities as your browser.
- If a host's chain still fails, the crawler retries **that host only** without
  certificate verification, logs a warning, and continues. One broken
  certificate can no longer empty a whole crawl.
- `--strict-ssl` turns that fallback off and fails instead.
- `--insecure` turns verification off for every host (only for debugging).
- If the start page itself cannot be fetched, the crawler exits with code `3`
  and the app marks the job **error** instead of **done**, so an empty result is
  never silently reported as success.

## Notes

- Stopping a job sends a graceful break; the crawler finishes the current
  request, saves state, and the pages already saved are still packaged.
- `--external-max-pages 0` in the crawler means *unlimited*, which is what
  *Full scraping* uses.
- The app is a local single-user tool. It binds to `127.0.0.1` only.
