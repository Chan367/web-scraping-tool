"""Separate, lossless de-duplication of a scraped website.

The original ``Scraped_Data/<site>`` folder is NEVER modified.  A de-duped copy
is written to ``<chosen folder>/<site>_deduped`` with::

    Files/<kind>/<hash>/<hash>.<ext>   one physical copy per unique file
    categories/<category>/pages.jsonl the per-category record files
    Json/                              the generated JSON files
    dedupe_manifest.json               every original path -> hash -> stored path
    dedupe_report.json                 counts and space saved

The manifest is enough to rebuild the original, non-de-duplicated folder later
(``restore`` below), so both versions can always be handed over.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paths

# These sites are never touched by de-dupe, restore or legacy cleanup.
PROTECTED_SITES = {"india_gov_in", "nhm_gov_in"}

MANIFEST_NAME = "dedupe_manifest.json"
REPORT_NAME = "dedupe_report.json"
TASKS_FILE = paths.DATA_DIR / "dedupe_tasks.json"

MEDIA_DIRS = {
    "images": "images",
    "pdfs": "pdfs",
    "audio": "audio",
    "video": "video",
    "documents": "documents",
}
URL_FIELDS = (
    "image_paths", "pdf_paths", "audio_paths", "video_paths",
    "document_paths", "pdf_text_paths", "html_path",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _guard(site_name: str) -> None:
    name = str(site_name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("Enter a valid website folder name.")
    if name in PROTECTED_SITES:
        raise ValueError(f"{name} is protected and cannot be modified.")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return
    shutil.copy2(src, dst)


def _safe_rel(raw: str) -> Path:
    """Relative path from a manifest, with traversal segments removed."""
    parts = [p for p in str(raw or "").replace("\\", "/").split("/")
             if p and p not in (".", "..")]
    return Path(*parts)


def _size_of(root: Path) -> tuple[int, int]:
    count = 0
    total = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        count += 1
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return count, total


def _url_lookup(source: Path) -> dict[str, str]:
    """Map a stored relative media path back to the page that linked to it."""
    lookup: dict[str, str] = {}
    try:
        jsonl_files = sorted((source / "categories").glob("*/pages.jsonl"))
    except OSError:
        return lookup
    for jsonl in jsonl_files:
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
                    url = str(record.get("source_url") or "")
                    for field in URL_FIELDS:
                        value = record.get(field)
                        if isinstance(value, str):
                            lookup[value.replace("\\", "/").lstrip("/")] = url
                        elif isinstance(value, list):
                            for rel in value:
                                lookup[str(rel).replace("\\", "/").lstrip("/")] = url
        except OSError:
            continue
    return lookup


class DedupeManager:
    def __init__(self) -> None:
        paths.ensure_dirs()
        self._lock = threading.RLock()
        self._tasks: dict[str, dict] = {}
        self._stops: dict[str, threading.Event] = {}
        self._target_cache: dict[str, tuple[float, dict]] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not TASKS_FILE.is_file():
            return
        try:
            data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
            for task in data.get("tasks") or []:
                if task.get("status") in ("running", "queued"):
                    task["status"] = "stopped"
                    task["message"] = "App restarted before the task finished."
                self._tasks[task["id"]] = task
        except Exception:
            pass

    def _save(self) -> None:
        with self._lock:
            payload = {"tasks": list(self._tasks.values())}
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TASKS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(TASKS_FILE)

    # -- queries -----------------------------------------------------------
    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def list_tasks(self, site: str = "") -> list[dict]:
        with self._lock:
            tasks = [dict(t) for t in self._tasks.values()]
        if site:
            tasks = [t for t in tasks if t.get("site") == site]
        tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        return tasks

    def targets(self) -> list[dict]:
        """Sites that are ready to de-dupe (stopped, completed or failed)."""
        import jobs  # lazy: jobs imports packager/runner, no circular import

        try:
            job_list = jobs.manager.list()
        except Exception:
            job_list = []
        by_name = {str(j.get("site") or ""): j for j in job_list}

        candidates: dict[str, tuple[Path, dict]] = {}
        for child in paths.all_site_dirs():
            candidates[child.name] = (child, by_name.get(child.name) or {})
        for job in job_list:
            name = str(job.get("site") or "")
            stored = job.get("site_dir") or ""
            if not name or name in candidates:
                continue
            path = Path(stored) if stored else paths.site_path(name)
            if path.is_dir():
                candidates[name] = (path, job)

        out: list[dict] = []
        now = time.time()
        for name, (source, job) in sorted(candidates.items()):
            if name in PROTECTED_SITES:
                continue
            cached = self._target_cache.get(name)
            if cached and now - cached[0] < 30:
                info = dict(cached[1])
            else:
                count, total = _size_of(source)
                categories = sorted(
                    p.parent.name
                    for p in (source / "categories").glob("*/pages.jsonl")
                ) if (source / "categories").is_dir() else []
                records = 0
                for cat in categories:
                    try:
                        with (source / "categories" / cat / "pages.jsonl").open(
                                encoding="utf-8") as fh:
                            records += sum(1 for line in fh if line.strip())
                    except OSError:
                        pass
                info = {
                    "site": name,
                    "source": str(source),
                    "files": count,
                    "bytes": total,
                    "categories": len(categories),
                    "category_names": categories,
                    "records": records,
                    "legacy": [r for r in ("Internal_Pages", "External_Pages")
                               if (source / r).is_dir()],
                }
                self._target_cache[name] = (now, info)
            info = dict(info)
            status = str(job.get("status") or "")
            info["status"] = status if status in ("running", "waiting", "stopped", "done", "error") else "scraped"
            info["job_id"] = job.get("id") or ""
            info["start_url"] = job.get("url") or ""
            info["protected"] = False
            tasks = self.list_tasks(name)
            info["tasks"] = tasks[:5]
            info["latest_task"] = tasks[0] if tasks else None
            out.append(info)
        out.sort(key=lambda t: (t["status"] not in ("stopped", "done", "error"),
                                t["site"]))
        return out

    # -- destination -------------------------------------------------------
    def _output_path(self, site_name: str, destination: str) -> Path:
        _guard(site_name)
        raw = str(destination or "").strip().strip('"')
        if not raw:
            raise ValueError("Enter the folder where the de-duped copy should be saved.")
        base = paths.resolve_path(raw)
        error = paths.output_root_error(base)
        if error:
            raise ValueError(error)
        expected = f"{site_name}_deduped"
        # Accept both "<chosen>\<site>_deduped" and a bare parent folder.
        if base.name == expected:
            return base
        return base / expected

    # -- start -------------------------------------------------------------
    def start(self, site_name: str, destination: str, mode: str = "dedupe") -> dict:
        _guard(site_name)
        source = paths.site_path(site_name)
        if not (source / "categories").is_dir():
            raise ValueError(f"No scraped categories found for {site_name}.")
        output = self._output_path(site_name, destination)
        if mode == "restore":
            manifest = output / MANIFEST_NAME
            if not manifest.is_file():
                raise ValueError(f"Manifest not found in {output}. Run de-dupe first.")
            target_base = output.parent
            target = target_base / f"{site_name}_full"
            if target_base.name.endswith("_deduped"):
                target = output.parent / f"{site_name}_full"
        else:
            target = output

        task_id = "ded_" + uuid.uuid4().hex[:10]
        stop = threading.Event()
        task = {
            "id": task_id,
            "type": mode,
            "site": site_name,
            "source": str(source),
            "destination": str(target if mode == "restore" else output),
            "status": "running",
            "created_at": utc_now(),
            "finished_at": "",
            "processed": 0,
            "total": 0,
            "unique": 0,
            "duplicates": 0,
            "percent": 0,
            "message": "Preparing…",
            "result": {},
            "error": "",
        }
        with self._lock:
            self._tasks[task_id] = task
            self._stops[task_id] = stop
        self._save()
        worker_target = self._run_restore if mode == "restore" else self._run_dedupe
        thread = threading.Thread(
            target=worker_target,
            args=(task_id, source, output, target, stop),
            daemon=True,
        )
        thread.start()
        return dict(task)

    def stop(self, task_id: str) -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
            stop = self._stops.get(task_id)
        if not task:
            raise ValueError("task not found")
        if stop:
            stop.set()
        return self.get(task_id) or task

    def _update(self, task_id: str, **fields) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.update(fields)
        self._save()

    def _progress(self, task_id: str, processed: int, total: int,
                  unique: int, duplicates: int) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task["processed"] = processed
            task["total"] = total
            task["unique"] = unique
            task["duplicates"] = duplicates
            task["percent"] = min(100, round(processed * 100 / max(1, total), 1))
        self._save()

    # -- de-dupe worker ----------------------------------------------------
    def _run_dedupe(self, task_id: str, source: Path, output: Path,
                    target: Path, stop: threading.Event) -> None:
        try:
            output.mkdir(parents=True, exist_ok=True)
            categories = source / "categories"
            files = [p for p in categories.rglob("*") if p.is_file()]
            total = len(files)
            # Counts are relative to the output that exists when this task
            # starts.  Re-running de-dupe on the same folder only copies newly
            # added originals, so 0 new unique files is correct and not an
            # error (the manifest and stored files stay complete).
            existing = 0
            if (output / MANIFEST_NAME).is_file():
                try:
                    old = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
                    existing = len(old.get("entries") or [])
                except Exception:
                    existing = 0
            self._update(task_id, total=total, message="Hashing files…")
            urls = _url_lookup(source)

            manifest_entries: list[dict] = []
            errors: list[str] = []
            per_kind: dict[str, int] = {}
            per_category: dict[str, int] = {}
            bytes_scanned = 0
            bytes_after = 0
            unique = duplicates = 0

            for index, path in enumerate(files, 1):
                if stop.is_set():
                    self._update(task_id, status="stopped",
                                 message="Stopped. Partial output was kept.",
                                 finished_at=utc_now())
                    return
                rel = path.relative_to(source).as_posix()
                parts = rel.split("/")
                category = parts[1] if len(parts) > 2 and parts[0] == "categories" else ""
                try:
                    size = path.stat().st_size
                    if len(parts) == 3 and parts[0] == "categories" and parts[2] == "pages.jsonl":
                        stored_rel = rel
                        dest = output / _safe_rel(stored_rel)
                        _copy(path, dest)
                        digest = _hash_file(path)
                        kind = "pages"
                    else:
                        lower = rel.lower()
                        kind = "other"
                        for marker, name in MEDIA_DIRS.items():
                            if f"/media/{marker}/" in lower:
                                kind = name
                                break
                        else:
                            if "/html/" in lower:
                                kind = "html"
                        digest = _hash_file(path)
                        ext = path.suffix.lower()
                        stored_rel = f"Files/{kind}/{digest}/{digest}{ext}"
                        dest = output / _safe_rel(stored_rel)
                        if dest.is_file():
                            duplicates += 1
                        else:
                            _copy(path, dest)
                            unique += 1
                            bytes_after += size
                    bytes_scanned += size
                    per_kind[kind] = per_kind.get(kind, 0) + 1
                    if category:
                        per_category[category] = per_category.get(category, 0) + 1
                    manifest_entries.append({
                        "original": rel,
                        "category": category,
                        "name": path.name,
                        "url": urls.get(rel, ""),
                        "hash": digest,
                        "stored": stored_rel,
                        "size": size,
                        "kind": kind,
                    })
                except Exception as exc:  # keep going: one bad file must not lose the rest
                    errors.append(f"{rel}: {exc}")
                if index % 25 == 0 or index == total:
                    self._progress(task_id, index, total, unique, duplicates)
                    self._update(task_id, message=f"Hashing files… {index}/{total}")

            self._update(task_id, message="Writing manifest and JSON files…")

            # JSON files are small text; copy them as-is for a usable output.
            json_src = source / "Json"
            if json_src.is_dir():
                shutil.copytree(json_src, output / "Json", dirs_exist_ok=True)
            for name in ("website_info.json", "scraping_summary.json"):
                src = source / name
                if src.is_file():
                    _copy(src, output / name)

            manifest = {
                "version": 1,
                "created_at": utc_now(),
                "source": str(source),
                "destination": str(output),
                "entries": sorted(manifest_entries, key=lambda e: e["original"]),
            }
            (output / MANIFEST_NAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            report = {
                "created_at": utc_now(),
                "source": str(source),
                "destination": str(output),
                "files_scanned": total,
                "unique_files": unique if not existing else unique + existing,
                "new_unique_files": unique,
                "duplicate_files": duplicates,
                "manifest_entries": len(manifest_entries),
                "bytes_scanned": bytes_scanned,
                "bytes_after": bytes_after,
                "bytes_saved": max(0, bytes_scanned - bytes_after),
                "per_kind": per_kind,
                "per_category": per_category,
                "categories": len(per_category),
                "errors": errors[:100],
                "error_count": len(errors),
            }
            (output / REPORT_NAME).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self._target_cache.pop(source.name, None)
            self._update(
                task_id,
                status="done",
                percent=100,
                processed=total,
                total=total,
                unique=unique,
                duplicates=duplicates,
                finished_at=utc_now(),
                message=(f"Done — {total} files scanned, {unique} unique kept, "
                         f"{duplicates} duplicates removed from the copy."),
                result={
                    "destination": str(output),
                    "manifest": str(output / MANIFEST_NAME),
                    "report": str(output / REPORT_NAME),
                    "bytes_saved": report["bytes_saved"],
                    "errors": len(errors),
                },
            )
        except Exception as exc:
            self._update(task_id, status="error", finished_at=utc_now(),
                         message=str(exc), error=str(exc))

    # -- restore worker ----------------------------------------------------
    def _run_restore(self, task_id: str, source: Path, output: Path,
                     target: Path, stop: threading.Event) -> None:
        try:
            manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
            entries = manifest.get("entries") or []
            self._update(task_id, total=len(entries), message="Rebuilding full folder…")
            target.mkdir(parents=True, exist_ok=True)
            copied = 0
            for index, entry in enumerate(entries, 1):
                if stop.is_set():
                    self._update(task_id, status="stopped", finished_at=utc_now(),
                                 message="Stopped. Partial restore was kept.")
                    return
                stored = output / _safe_rel(str(entry.get("stored") or ""))
                dest = target / _safe_rel(str(entry.get("original") or ""))
                if stored.is_file():
                    _copy(stored, dest)
                    copied += 1
                if index % 25 == 0 or index == len(entries):
                    self._progress(task_id, index, len(entries), copied, 0)
                    self._update(task_id, message=f"Rebuilding files… {index}/{len(entries)}")
            json_src = output / "Json"
            if json_src.is_dir():
                shutil.copytree(json_src, target / "Json", dirs_exist_ok=True)
            for name in ("website_info.json", "scraping_summary.json"):
                src = output / name
                if src.is_file():
                    _copy(src, target / name)
            self._update(
                task_id,
                status="done", percent=100, finished_at=utc_now(),
                message=f"Restored {copied} original file(s) with their original names.",
                result={"destination": str(target), "files": copied},
            )
        except Exception as exc:
            self._update(task_id, status="error", finished_at=utc_now(),
                         message=str(exc), error=str(exc))

    # -- legacy folder cleanup --------------------------------------------
    def cleanup(self, site_name: str) -> dict:
        """Remove old Internal_Pages/External_Pages after verifying contents.

        Every non-generated file must still exist in categories/ (same name and
        size).  If any file is missing, that folder is left untouched and the
        report explains why.  Protected sites are always refused.
        """
        _guard(site_name)
        source = paths.site_path(site_name)
        categories = source / "categories"
        if not categories.is_dir():
            raise ValueError(f"No categories folder found for {site_name}.")
        has_records = any(True for _ in categories.glob("*/pages.jsonl"))
        if not has_records:
            raise ValueError("No pages.jsonl files found in categories — nothing verified.")

        index: dict[str, set[int]] = {}
        for path in categories.rglob("*"):
            if path.is_file():
                try:
                    index.setdefault(path.name, set()).add(path.stat().st_size)
                except OSError:
                    pass

        results = []
        for name in ("Internal_Pages", "External_Pages"):
            folder = source / name
            if not folder.is_dir():
                results.append({"folder": name, "present": False, "deleted": False,
                                "reason": "not present"})
                continue
            files = [p for p in folder.rglob("*") if p.is_file()]
            matched = generated = 0
            unmatched: list[str] = []
            for path in files:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size in index.get(path.name, set()):
                    matched += 1
                elif path.suffix.lower() == ".json":
                    # Generated bucket/Links JSON can be rebuilt from pages.jsonl.
                    generated += 1
                else:
                    unmatched.append(str(path.relative_to(source)).replace("\\", "/"))
            if unmatched:
                results.append({
                    "folder": name, "present": True, "deleted": False,
                    "files": len(files), "matched": matched, "generated": generated,
                    "unmatched": len(unmatched),
                    "examples": unmatched[:10],
                    "reason": "Some files were not found in categories; nothing deleted.",
                })
                continue
            try:
                shutil.rmtree(folder)
                deleted = not folder.exists()
            except OSError as exc:
                deleted = False
                reason = f"Could not delete: {exc}"
            else:
                reason = "All content verified in categories/ before removal."
            results.append({
                "folder": name, "present": True, "deleted": deleted,
                "files": len(files), "matched": matched, "generated": generated,
                "unmatched": 0, "reason": reason,
            })
        self._target_cache.pop(site_name, None)
        return {"site": site_name, "results": results}


manager = DedupeManager()
