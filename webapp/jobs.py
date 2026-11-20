"""Job manager: one thread per scrape, live watchers, persistence."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import packager
import paths
import runner


# How many websites may be crawled at the same time.  Each crawl keeps its own
# delay/rate limiter, so this is a machine/network safety cap rather than a
# per-site politeness setting.  Override with SCRAPER_MAX_CONCURRENT_JOBS.
def _env_limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


MAX_CONCURRENT_JOBS = _env_limit("SCRAPER_MAX_CONCURRENT_JOBS", 2)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return "job_" + uuid.uuid4().hex[:10]


class JobManager:
    def __init__(self) -> None:
        paths.ensure_dirs()
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._runs: dict[str, runner.ScrapeRun] = {}
        self._stops: dict[str, threading.Event] = {}
        self._repair_stops: dict[str, threading.Event] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not paths.JOBS_FILE.is_file():
            return
        try:
            data = json.loads(paths.JOBS_FILE.read_text(encoding="utf-8"))
            for job in data.get("jobs") or []:
                if job.get("status") in ("running", "waiting"):
                    job["status"] = "stopped"
                    job["error"] = "app restarted"
                self._jobs[job["id"]] = job
        except Exception:
            pass

    def _save(self) -> None:
        with self._lock:
            payload = {"jobs": list(self._jobs.values())}
        tmp = paths.JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(paths.JOBS_FILE)

    # -- queries -----------------------------------------------------------
    def list(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return [self._with_live_progress(j) for j in jobs]

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
        return self._with_live_progress(job) if job else None

    def _with_live_progress(self, job: dict) -> dict:
        run = self._runs.get(job["id"])
        if run:
            try:
                job = dict(job)
                job["stats"] = {**(job.get("stats") or {}), "progress": run.progress()}
            except Exception:
                pass
        return job

    # -- create ------------------------------------------------------------
    def create(self, payload: dict) -> dict:
        url = (payload.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")

        kind = payload.get("kind") or "scrape"
        config = payload.get("config") or {}

        # Storage folder chosen in the UI. It must already exist; the website
        # folder itself is created inside <storage>/Scraped_Data/<site_name>/.
        raw_root = str(payload.get("output_root") or config.get("output_root") or "").strip()
        root = paths.resolve_path(raw_root) if raw_root else paths.output_root()
        error = paths.output_root_error(root)
        if error:
            raise ValueError(error)
        if raw_root:
            root = paths.set_output_root(root)

        site = paths.site_dir(url, root)
        site_name = site.name

        with self._lock:
            active = [
                existing for existing in self._jobs.values()
                if existing.get("status") in ("running", "waiting", "queued")
            ]
            for existing in active:
                if existing.get("site") == site_name:
                    raise ValueError(
                        f"A job is already running for {site_name}. Stop it first."
                    )
            for existing in self._jobs.values():
                if existing.get("site") == site_name and existing.get("repairing"):
                    raise ValueError(
                        f"Empty-page repair is running for {site_name}. Wait for it "
                        "to finish before starting a new job."
                    )
            if len(active) >= MAX_CONCURRENT_JOBS:
                raise ValueError(
                    f"{len(active)} websites are already running. Up to "
                    f"{MAX_CONCURRENT_JOBS} can run at the same time; wait for "
                    "one to finish or stop it first."
                )

        job = {
            "id": _new_id(),
            "kind": kind,
            "url": url,
            "site": site_name,
            "site_dir": str(site),
            "output_root": str(root),
            "display_name": paths.pretty_name(url),
            "status": "queued",
            "created_at": utc_now(),
            "started_at": "",
            "finished_at": "",
            "config": config,
            "live": payload.get("live") or {},
            "stats": {},
            "error": "",
            "log": str(site / "logs" / "webapp.log"),
            "delivery": "",
        }
        job["delivery"] = str(self.delivery_for(job))
        with self._lock:
            self._jobs[job["id"]] = job
        self._save()
        return job

    # -- control -----------------------------------------------------------
    def start(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError("job not found")
            if job["status"] in ("running", "waiting"):
                return job
            others = [
                other for other in self._jobs.values()
                if other.get("id") != job_id
                and other.get("status") in ("running", "waiting", "queued")
            ]
            if any(other.get("site") == job.get("site") and other.get("repairing")
                   for other in self._jobs.values() if other.get("id") != job_id):
                raise ValueError(
                    f"Empty-page repair is running for {job.get('site')}. "
                    "Wait for it to finish first."
                )
            if len(others) >= MAX_CONCURRENT_JOBS:
                raise ValueError(
                    f"{len(others)} websites are already running. Up to "
                    f"{MAX_CONCURRENT_JOBS} can run at the same time; wait for "
                    "one to finish or stop it first."
                )
            job["status"] = "running"
            job["error"] = ""
            job["started_at"] = utc_now()
            job["finished_at"] = ""
            stop_event = threading.Event()
            self._stops[job_id] = stop_event
        self._save()

        thread = threading.Thread(target=self._run, args=(job_id, stop_event), daemon=True)
        thread.start()
        return self.get(job_id) or job

    def stop(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError("job not found")
            event = self._stops.get(job_id)
            run = self._runs.get(job_id)
        if event:
            event.set()
        if run:
            run.stop()
        return self.get(job_id) or job

    # -- empty-page repair -------------------------------------------------
    def repair_empty(self, job_id: str, limit: int = 50) -> dict:
        """Re-render a bounded number of empty pages without re-crawling."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError("job not found")
            if job.get("repairing"):
                return self.get(job_id) or job
            if job.get("status") in ("running", "waiting", "queued"):
                raise ValueError("Stop this job before repairing its saved pages.")
            for other in self._jobs.values():
                if (other.get("id") != job_id and other.get("site") == job.get("site")
                        and (other.get("repairing")
                             or other.get("status") in ("running", "waiting", "queued"))):
                    raise ValueError(
                        "Another job or repair is active for this site. Stop it first."
                    )
            job["repairing"] = True
            job["repair"] = {
                "status": "running",
                "limit": max(0, int(limit)),
                "started_at": utc_now(),
            }
        self._save()
        stop = threading.Event()
        self._repair_stops[job_id] = stop
        threading.Thread(
            target=self._repair_worker,
            args=(job_id, dict(job), stop),
            daemon=True,
        ).start()
        return self.get(job_id) or job

    def stop_repair(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            stop = self._repair_stops.get(job_id)
        if not job:
            raise ValueError("job not found")
        if stop:
            stop.set()
        return self.get(job_id) or job

    def _repair_worker(self, job_id: str, job: dict, stop: threading.Event) -> None:
        site = self.site_for(job)
        log_path = Path(job["log"])
        progress_path = site / "data" / "repair_progress.json"
        cmd = [
            sys.executable, "-u", str(paths.SCRAPER),
            "--url", job.get("url", ""),
            "--root-dir", str(site.parent),
            "--repair-empty",
            "--repair-limit", str((job.get("repair") or {}).get("limit", 50)),
            "--render-timeout", "45",
            "--delay", "1.5",
            "--log-level", "INFO",
        ]
        cmd.extend(runner.proxy_args(job.get("config") or {}))
        exit_code = -1
        error = ""
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write("\n" + "=" * 78 + "\n")
                handle.write(time.strftime("REPAIR STARTED %Y-%m-%d %H:%M:%S\n"))
                handle.write(runner.redact_command(cmd) + "\n")
                handle.flush()
                kwargs: dict = {
                    "cwd": str(paths.ROOT),
                    "stdout": handle,
                    "stderr": subprocess.STDOUT,
                    "env": runner._scraper_env(),
                }
                if os.name == "nt":
                    kwargs["creationflags"] = runner.CREATE_GROUP
                process = subprocess.Popen(cmd, **kwargs)
                while process.poll() is None:
                    if stop.is_set():
                        process.terminate()
                        break
                    if progress_path.is_file():
                        try:
                            progress = json.loads(progress_path.read_text(encoding="utf-8"))
                            progress["running"] = True
                            with self._lock:
                                current = self._jobs.get(job_id)
                                if current:
                                    current["stats"] = {
                                        **(current.get("stats") or {}),
                                        "repair_progress": progress,
                                    }
                            self._save()
                        except Exception:
                            pass
                    time.sleep(1)
                exit_code = process.wait()
        except Exception as exc:
            error = str(exc)

        status = "stopped" if stop.is_set() else ("error" if (error or exit_code) else "done")
        with self._lock:
            current = self._jobs.get(job_id)
            if current:
                current["repairing"] = False
                current["repair"] = {
                    **(current.get("repair") or {}),
                    "status": status,
                    "finished_at": utc_now(),
                    "exit_code": exit_code,
                    "error": error,
                }
        self._repair_stops.pop(job_id, None)
        self._save()

        # Refresh the generated JSON and the empty-page warning from the
        # rewritten pages.jsonl files.
        try:
            delivery = self.delivery_for(job)
            collected_after = job.get("started_at", "") if job.get("kind") == "live" else ""
            stats = packager.build(
                site, job.get("config") or {}, delivery=delivery,
                collected_after=collected_after, start_url=job.get("url", ""),
            )
            stats["folders"] = packager.folder_report(Path(stats["delivery"]))
            self._set_warning(stats)
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current["stats"] = {**(current.get("stats") or {}), **stats,
                                        "last_run": utc_now()}
                    current["delivery"] = stats["delivery"]
            self._save()
        except Exception as exc:
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current["repair"] = {
                        **(current.get("repair") or {}),
                        "refresh_error": str(exc),
                    }
            self._save()

    def resume(self, job_id: str) -> dict:
        """Continue a stopped or failed job from the crawler's saved state.

        The crawler keeps ``data/state.json`` (visited pages + pending queue),
        so starting the same job again continues at the page where it stopped
        instead of beginning from scratch.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError("job not found")
            if job.get("status") in ("running", "waiting"):
                return self.get(job_id) or job
            job["error"] = ""
            job["stats"] = {
                **(job.get("stats") or {}),
                "resumed_at": utc_now(),
            }
        return self.start(job_id)

    def restart(self, job_id: str) -> dict:
        """Throw away this job's scraped folder and crawl from page one.

        The old folder is moved (not deleted) to ``webapp/data/backups`` so
        it can still be recovered.  Useful when the previous run used the
        wrong language or the wrong settings.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError("job not found")
            if job.get("status") in ("running", "waiting"):
                raise ValueError("Stop the job before starting over.")
            if job.get("kind") == "live":
                raise ValueError("Live jobs cannot be restarted; remove it and start a new live job.")

        site = self.site_for(job)
        if site.exists():
            backups = paths.DATA_DIR / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = backups / f"{site.name}_{stamp}"
            counter = 1
            while target.exists():
                counter += 1
                target = backups / f"{site.name}_{stamp}_{counter}"
            shutil.move(str(site), str(target))

        with self._lock:
            job["stats"] = {}
            job["error"] = ""
            job["finished_at"] = ""
            job["delivery"] = str(self.delivery_for(job))
        self._save()
        return self.start(job_id)

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._runs.pop(job_id, None)
            self._stops.pop(job_id, None)
        self._save()

    # -- worker ------------------------------------------------------------
    def site_for(self, job: dict) -> Path:
        """The site folder this job writes to (stored when the job was made)."""
        stored = job.get("site_dir")
        if stored:
            return Path(stored)
        return paths.site_path(job["site"])

    def delivery_for(self, job: dict) -> Path:
        """The site root for normal jobs; a private folder for live jobs."""
        site = self.site_for(job)
        if job.get("kind") == "live":
            return site / "live" / job["id"]
        return site

    def _run(self, job_id: str, stop_event: threading.Event) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        site = self.site_for(job)
        config = job["config"] or {}

        try:
            if job["kind"] == "live":
                self._run_live(job, site, config, stop_event)
            else:
                self._run_once(job, site, config, stop_event, date_window=None)
        except Exception as exc:
            with self._lock:
                job["status"] = "error"
                job["error"] = str(exc)
                job["finished_at"] = utc_now()
            self._save()
            return

        with self._lock:
            if job["status"] not in ("error",):
                job["status"] = "stopped" if stop_event.is_set() else "done"
            job["finished_at"] = utc_now()
            self._runs.pop(job_id, None)
            self._stops.pop(job_id, None)
        self._save()

    def _set_warning(self, stats: dict) -> None:
        """Explain empty pages precisely instead of one vague warning."""
        records = int(stats.get("records") or 0)
        empty = int(stats.get("empty_records") or 0)
        empty_failed = int(stats.get("empty_render_failed") or 0)
        empty_external = int(stats.get("empty_external") or 0)
        if records and empty >= records:
            stats["warning"] = (
                f"All {records} saved page(s) have no title or text. The site "
                "probably builds its content with JavaScript and it could not "
                "be rendered. Check the job log for 'JavaScript render' lines."
            )
            return
        if not empty:
            stats.pop("warning", None)
            return
        parts = [f"{empty} of {records} saved page(s) have no title or text."]
        if empty_failed:
            parts.append(
                f"{empty_failed} failed JavaScript rendering (render budget or "
                "the site blocked the browser)."
            )
        unflagged = empty - empty_failed
        if unflagged:
            parts.append(
                f"{unflagged} have no render status (saved by an older scraper "
                "or a page that blocked the browser)."
            )
        if empty_external:
            parts.append(f"{empty_external} are external pages.")
        parts.append(
            "Use 'Repair empty pages' on the Jobs page to retry them; their "
            "pages and links are still saved."
        )
        stats["warning"] = " ".join(parts)

    def _run_once(self, job: dict, site: Path, config: dict,
                  stop_event: threading.Event, date_window) -> dict:
        run = runner.ScrapeRun(job["url"], config, site, Path(job["log"]))
        with self._lock:
            self._runs[job["id"]] = run
        run.start()
        exit_code = run.wait()
        final_progress = run.progress()
        stopped = stop_event.is_set()
        with self._lock:
            self._runs.pop(job["id"], None)

        delivery = self.delivery_for(job)
        # live runs only keep what they collected themselves, never older data
        collected_after = job.get("started_at", "") if job.get("kind") == "live" else ""

        # Even a stopped run gets packaged, so the pages already saved are usable.
        stats = packager.build(site, config, date_window=date_window,
                               delivery=delivery, collected_after=collected_after,
                               start_url=job.get("url", ""))
        stats["folders"] = packager.folder_report(Path(stats["delivery"]))
        stats["progress"] = final_progress
        stats["exit_code"] = exit_code
        self._set_warning(stats)
        if stopped:
            stats["partial"] = True
        with self._lock:
            job["stats"] = {**(job.get("stats") or {}), **stats, "last_run": utc_now()}
            job["delivery"] = stats["delivery"]
            # A non-zero exit is a real failure: never report it as "done".
            # Exit 3 means the start page itself could not be fetched.
            if not stopped:
                if exit_code:
                    job["status"] = "error"
                    job["error"] = (
                        "The start page could not be fetched, so nothing was "
                        "scraped. Check the job log and logs/failed_urls.txt."
                        if exit_code == 3 else
                        f"The scraper failed (exit code {exit_code}). "
                        "See the job log for details."
                    )
                else:
                    job["error"] = ""
        self._save()
        return stats

    def _run_live(self, job: dict, site: Path, config: dict,
                  stop_event: threading.Event) -> None:
        live = job.get("live") or {}
        start = (live.get("start") or "")[:10]
        end = (live.get("end") or "")[:10]
        interval = max(60, int(float(live.get("interval_min", 30) or 30) * 60))
        cycles = 0

        while not stop_event.is_set():
            cycles += 1
            with self._lock:
                job["status"] = "running"
                job["stats"] = {**(job.get("stats") or {}), "cycle": cycles}
            self._save()

            self._run_once(job, site, config, stop_event, date_window=(start, end))

            if stop_event.is_set():
                break
            today = datetime.now().strftime("%Y-%m-%d")
            if end and today > end:
                with self._lock:
                    job["stats"] = {**(job.get("stats") or {}),
                                    "note": f"end date {end} reached"}
                break

            with self._lock:
                job["status"] = "waiting"
                job["stats"] = {**(job.get("stats") or {}),
                                "next_run_in": interval}
            self._save()

            waited = 0
            while waited < interval and not stop_event.is_set():
                time.sleep(min(5, interval - waited))
                waited += 5
                with self._lock:
                    job["stats"]["next_run_in"] = max(0, interval - waited)


manager = JobManager()
