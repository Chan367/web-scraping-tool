"""Flask server for the scraping web app."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory

import dedupe
import jobs
import packager
import paths
import runner
import template as tpl

app = Flask(__name__, static_folder=str(paths.STATIC_DIR), static_url_path="/static")

MODES = [
    {"key": "internal", "label": "Internal web scrap",
     "hint": "Only your website: every menu/category and every page inside them. "
             "Other websites are never touched."},
    {"key": "external", "label": "External web scrap",
     "hint": "Only the other websites linked from your site. Your own pages are "
             "used just to find those links and are not saved. If there are none, "
             "the job tells you so."},
    {"key": "full", "label": "Full web scraping",
     "hint": "Both: your whole site plus every external website it links to."},
]

TYPE_LABELS = {
    "text": "Text",
    "image": "Images",
    "pdf": "PDF",
    "audio": "Audio",
    "video": "Video",
    "documents": "Other documents",
}


@app.get("/")
def index():
    return send_from_directory(paths.STATIC_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "scraper": str(paths.SCRAPER), "scraper_exists": paths.SCRAPER.is_file()})


@app.get("/api/config")
def config():
    root = paths.output_root()
    usage = shutil.disk_usage(str(root if root.is_dir() else paths.ROOT))
    return jsonify({
        "modes": MODES,
        "types": TYPE_LABELS,
        "formats": paths.MEDIA_FORMATS,
        "storage": paths.storage_info(),
        "defaults": {
            "delay": 1.0,
            "workers": 4,
            "max_pages": 0,
            "max_depth": 0,
            "internal_depth": 0,
            "render_budget": 0,
            "max_file_mb": 250,
            "external_max_pages": 0,
            "live_interval_min": 30,
            "category_complete": True,
        },
        "disk": {"free": usage.free, "total": usage.total},
        "proxies": paths.proxy_settings(),
        "proxy_modes": list(paths.PROXY_MODES),
    })


@app.post("/api/storage/check")
def check_storage():
    """Check a folder the user typed and remember it when it is usable."""
    body = request.get_json(silent=True) or {}
    raw = str(body.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "found": False, "writable": False,
                        "message": "Enter a folder path first."})

    path = paths.resolve_path(raw)
    if not path.exists():
        return jsonify({"ok": False, "found": False, "writable": False,
                        "path": str(path),
                        "message": f"Folder not found: {path}"})
    if not path.is_dir():
        return jsonify({"ok": False, "found": True, "writable": False,
                        "path": str(path),
                        "message": f"That path is a file, not a folder: {path}"})
    if not paths.is_writable(path):
        return jsonify({"ok": False, "found": True, "writable": False,
                        "path": str(path),
                        "message": f"Folder found, but it is not writable: {path}"})

    paths.set_output_root(path)
    info = paths.storage_info()
    return jsonify({
        "ok": True,
        "found": True,
        "writable": True,
        "path": info["output_root"],
        "storage": info,
        "message": f"Folder found. Website folders will be stored in {info['scraped_dir']}.",
    })


@app.post("/api/template/preview")
def template_preview():
    body = request.get_json(silent=True) or {}
    try:
        rendered = tpl.preview(body.get("template") or "")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "preview": rendered})


@app.get("/api/proxies")
def get_proxies():
    return jsonify({"ok": True, **paths.proxy_settings(mask=True)})


@app.post("/api/proxies")
def save_proxies():
    body = request.get_json(silent=True) or {}
    if "proxies" in body and body.get("proxies") is not None:
        proxies = body.get("proxies")
    else:
        proxies = paths.proxy_settings(mask=False)["proxies"]
    settings = paths.set_proxy_settings(
        proxies,
        str(body.get("proxy_mode") or "rotate_on_block"),
    )
    return jsonify({"ok": True, **settings})


@app.post("/api/proxies/test")
def test_proxies():
    """Check proxy syntax and, when possible, make one small request per proxy."""
    body = request.get_json(silent=True) or {}
    raw = body.get("proxies")
    if raw is None:
        proxies = paths.proxy_settings(mask=False)["proxies"]
    elif isinstance(raw, str):
        proxies = [line.strip() for line in raw.splitlines() if line.strip()]
    else:
        proxies = [str(p).strip() for p in raw if str(p).strip()]

    results = []
    for proxy in proxies:
        label = paths.mask_proxy(proxy)
        try:
            parts = urlsplit(proxy)
            valid = (parts.scheme in ("http", "https", "socks5", "socks5h")
                     and bool(parts.hostname) and bool(parts.port))
        except Exception:
            valid = False
        if not valid:
            results.append({"proxy": label, "ok": False,
                            "error": "Use http://user:pass@IP:PORT format."})
            continue
        started = time.perf_counter()
        try:
            resp = requests.get(
                "https://example.com",
                proxies={"http": proxy, "https": proxy},
                timeout=(8, 12),
            )
            ok = resp.status_code < 500
            results.append({
                "proxy": label,
                "ok": ok,
                "status": resp.status_code,
                "ms": int((time.perf_counter() - started) * 1000),
            })
        except Exception as exc:
            results.append({
                "proxy": label,
                "ok": False,
                "error": f"{type(exc).__name__}: connection failed",
            })

    if not results:
        return jsonify({
            "ok": True,
            "results": [],
            "message": "No proxies configured; jobs will use the real connection.",
        })
    return jsonify({"ok": all(r["ok"] for r in results), "results": results})


@app.post("/api/jobs")
def create_job():
    body = request.get_json(silent=True) or {}
    try:
        job = jobs.manager.create(body)
        jobs.manager.start(job["id"])
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "job": jobs.manager.get(job["id"])}), 201


@app.get("/api/jobs")
def list_jobs():
    return jsonify({"ok": True, "jobs": jobs.manager.list()})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = jobs.manager.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.post("/api/jobs/<job_id>/stop")
def stop_job(job_id: str):
    try:
        job = jobs.manager.stop(job_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "job": job})


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id: str):
    """Continue a stopped or failed job from the crawler's saved state."""
    try:
        job = jobs.manager.resume(job_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "job": job})


@app.post("/api/jobs/<job_id>/restart")
def restart_job(job_id: str):
    """Delete this job's scraped folder (backed up) and crawl from page one."""
    try:
        job = jobs.manager.restart(job_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    return jsonify({"ok": True, "job": job})


@app.post("/api/jobs/<job_id>/repair-empty")
def repair_empty_pages(job_id: str):
    """Retry only stored pages that still have no title or text."""
    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    try:
        job = jobs.manager.repair_empty(job_id, limit=limit)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    return jsonify({"ok": True, "job": job})


@app.post("/api/jobs/<job_id>/repair-stop")
def repair_stop(job_id: str):
    try:
        job = jobs.manager.stop_repair(job_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "job": job})


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id: str):
    jobs.manager.remove(job_id)
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>/log")
def job_log(job_id: str):
    job = jobs.manager.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    lines = int(request.args.get("lines", 300))
    text = runner.tail(Path(job["log"]), lines)
    return jsonify({"ok": True, "log": text})


@app.post("/api/jobs/<job_id>/open")
def open_job_folder(job_id: str):
    """Open the job's delivery folder in the file manager."""
    job = jobs.manager.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    raw = job.get("delivery") or job.get("site_dir") or ""
    target = Path(raw) if raw else None
    if not target or not target.is_dir():
        return jsonify({"ok": False, "error": "folder not found"}), 404
    try:
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "path": str(target)})


@app.get("/api/jobs/<job_id>/folders")
def job_folders(job_id: str):
    job = jobs.manager.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    delivery = Path(job.get("delivery") or "")
    rows = packager.folder_report(delivery) if delivery.is_dir() else []
    return jsonify({"ok": True, "folders": rows, "delivery": str(delivery)})


def _delivery_of(job: dict) -> Path:
    return Path(job.get("delivery") or "")


@app.get("/api/jobs/<job_id>/json")
def job_json(job_id: str):
    """Return one of the job's JSON files pretty-printed for the UI viewer."""
    job = jobs.manager.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    json_dir = _delivery_of(job) / "Json"
    if not json_dir.is_dir():
        return jsonify({"ok": False, "error": "nothing has been saved for this job yet"}), 400
    files = sorted(p.name for p in json_dir.glob("*.json") if p.is_file())
    if not files:
        return jsonify({"ok": False, "error": "no JSON files for this job yet"}), 400

    name = Path(request.args.get("file") or "").name
    if name not in files:
        name = "Overall.json" if "Overall.json" in files else files[0]
    path = json_dir / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return jsonify({"ok": False, "error": f"cannot read {name}: {exc}"}), 400

    total = len(data) if isinstance(data, list) else 1
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    shown = total
    truncated = False
    if isinstance(data, list) and limit > 0 and len(data) > limit:
        data = data[:limit]
        shown = limit
        truncated = True

    text = (
        packager.dumps_json_records(data)
        if isinstance(data, list)
        else json.dumps(data, ensure_ascii=False, indent=2)
    )
    return jsonify({
        "ok": True,
        "file": name,
        "files": files,
        "text": text,
        "records": total,
        "shown": shown,
        "truncated": truncated,
        "bytes": path.stat().st_size,
    })


@app.get("/api/jobs/<job_id>/duplicates")
def job_duplicates(job_id: str):
    """The original folder is never changed; use the De-dupe page instead."""
    job = jobs.manager.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    delivery = _delivery_of(job)
    if not delivery.is_dir():
        return jsonify({"ok": False, "error": "nothing has been saved for this job yet"}), 400
    return jsonify({
        "ok": True,
        "delivery": str(delivery),
        "message": (
            "The original folder is kept exactly as scraped. Open the De-dupe "
            "page to create a separate de-duplicated copy in a folder you choose."
        ),
        "report": {"groups": [], "duplicate_files": 0,
                   "wasted_bytes": 0, "scanned": 0},
    })


@app.post("/api/jobs/<job_id>/duplicates/remove")
def job_duplicates_remove(job_id: str):
    """Kept for old clients, but intentionally does not delete anything."""
    job = jobs.manager.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    delivery = _delivery_of(job)
    if not delivery.is_dir():
        return jsonify({"ok": False, "error": "nothing has been saved for this job yet"}), 400
    result = packager.remove_duplicates(delivery)
    result["folders"] = packager.folder_report(delivery)
    return jsonify({"ok": True, "result": result})


@app.post("/api/jobs/<job_id>/rebuild")
def job_rebuild(job_id: str):
    job = jobs.manager.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    body = request.get_json(silent=True) or {}
    name = _json_name(body.get("name"))
    json_dir = _delivery_of(job) / "Json"
    master = json_dir / "master.json"
    try:
        result = packager.rebuild_from_master(master, body.get("template") or "",
                                              json_dir / f"{name}.json")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "result": result})


def _json_name(raw) -> str:
    name = "".join(ch for ch in str(raw or "custom") if ch.isalnum() or ch in "-_ ").strip()
    name = name.replace(" ", "_") or "custom"
    return name[:60]


@app.get("/api/sites/<site_name>/master")
def site_master(site_name: str):
    delivery = paths.site_path(site_name)
    master = delivery / "Json" / "master.json"
    if not master.is_file():
        return jsonify({"ok": False, "error": "no master.json for this site yet"}), 404
    try:
        data = json.loads(master.read_text(encoding="utf-8"))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "records": len(data), "bytes": master.stat().st_size,
                    "file": str(master), "fields": sorted(data[0].keys()) if data else []})


@app.post("/api/sites/<site_name>/rebuild")
def site_rebuild(site_name: str):
    body = request.get_json(silent=True) or {}
    delivery = paths.site_path(site_name)
    json_dir = delivery / "Json"
    master = json_dir / "master.json"
    try:
        result = packager.rebuild_from_master(master, body.get("template") or "",
                                              json_dir / f"{_json_name(body.get('name'))}.json")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "result": result})


@app.get("/api/sites")
def sites():
    """Folders already scraped, so the UI can offer them."""
    out = []
    for child in paths.all_site_dirs():
        delivery = child
        files = sum(1 for f in packager._output_files(delivery)) if delivery.is_dir() else 0
        start_url = ""
        state_file = child / "data" / "state.json"
        if state_file.is_file():
            try:
                start_url = json.loads(state_file.read_text(encoding="utf-8")).get("start_url", "")
            except Exception:
                pass
        pages = 0
        index_file = child / "data" / "scraped_index.json"
        if index_file.is_file():
            try:
                pages = int(json.loads(index_file.read_text(encoding="utf-8")).get("count") or 0)
            except Exception:
                pages = 0
        out.append({
            "site": child.name,
            "start_url": start_url,
            "delivery": str(delivery),
            "files": files,
            "pages": pages,
        })
    return jsonify({"ok": True, "sites": out})


# -------------------------------------------------------------- de-dupe ---
@app.get("/api/dedupe/targets")
def dedupe_targets():
    """Scraped sites that can be de-duped (stopped, completed or failed)."""
    return jsonify({"ok": True, "targets": dedupe.manager.targets()})


@app.get("/api/dedupe/tasks")
def dedupe_tasks():
    return jsonify({"ok": True, "tasks": dedupe.manager.list_tasks()})


@app.get("/api/dedupe/tasks/<task_id>")
def dedupe_task(task_id: str):
    task = dedupe.manager.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "task not found"}), 404
    return jsonify({"ok": True, "task": task})


@app.post("/api/dedupe/start")
def dedupe_start():
    body = request.get_json(silent=True) or {}
    mode = "restore" if body.get("mode") == "restore" else "dedupe"
    try:
        task = dedupe.manager.start(
            str(body.get("site") or ""),
            str(body.get("destination") or ""),
            mode=mode,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "task": task}), 201


@app.post("/api/dedupe/tasks/<task_id>/stop")
def dedupe_stop(task_id: str):
    try:
        task = dedupe.manager.stop(task_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "task": task})


@app.get("/api/dedupe/tasks/<task_id>/manifest")
def dedupe_manifest(task_id: str):
    task = dedupe.manager.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "task not found"}), 404
    from_task = str(((task.get("result") or {}).get("manifest") or ""))
    path = Path(from_task) if from_task else Path(task.get("destination") or "") / dedupe.MANIFEST_NAME
    if not path.is_file():
        return jsonify({"ok": False, "error": "manifest not written yet"}), 404
    return send_file(path, as_attachment=True,
                     download_name=f"{task.get('site', 'site')}_dedupe_manifest.json")


def _open_folder(target: Path) -> None:
    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


@app.post("/api/dedupe/open")
def dedupe_open():
    body = request.get_json(silent=True) or {}
    raw = str(body.get("path") or "").strip()
    target = paths.resolve_path(raw) if raw else None
    if not target or not target.is_dir():
        return jsonify({"ok": False, "error": "folder not found"}), 404
    try:
        _open_folder(target)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "path": str(target)})


@app.post("/api/dedupe/cleanup")
def dedupe_cleanup():
    body = request.get_json(silent=True) or {}
    try:
        result = dedupe.manager.cleanup(str(body.get("site") or ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "result": result})


def main() -> None:
    import os
    import threading
    import webbrowser

    paths.ensure_dirs()
    port = int(os.environ.get("PORT", "5000"))
    url = f"http://127.0.0.1:{port}"
    print("=" * 66)
    print("  Scraping Web App")
    print(f"  open:  {url}")
    print(f"  scraper: {paths.SCRAPER}")
    print("=" * 66)
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
