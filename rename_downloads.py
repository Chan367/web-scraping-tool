"""Rename existing PDF/document files using Topic-column or fallback names.

Default CLI mode is a preview: it prints what would change and writes nothing.
Use ``--apply`` to copy the new named files and update JSONL path fields.
The original hashed-name files are kept until ``cleanup_originals.py`` verifies
that a new named copy exists.

The web app also calls :func:`rename_target` when a stopped job is resumed.
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bs4 import BeautifulSoup  # noqa: E402

from scraper import (  # noqa: E402
    MAX_PATH_LEN,
    extract_topic_download_names,
    fallback_download_name,
    safe_topic_stem,
)

DEFAULT_TARGET = Path(
    r"C:\Users\chan8\OneDrive\Desktop\sample_web_sit_test\Scraped_Data\dare_gov_in\categories"
)
PROTECTED = {"india_gov_in", "nhm_gov_in"}

PAIRS = (
    ("pdf_files", "pdf_paths", "pdf_names"),
    ("document_files", "document_paths", "document_names"),
)
HASH_NAME = re.compile(r"^[0-9a-f]{10}_")


def site_rel(site_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(site_root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def resolve_path(site_root: Path, rel: str) -> Path | None:
    if not rel:
        return None
    path = Path(str(rel).replace("\\", "/"))
    return path if path.is_absolute() else site_root / path


def topic_names_for(
    record: dict, site_root: Path, log=print
) -> dict[str, str]:
    """Read URL -> Topic text only from a real Topic column in saved HTML.

    Deliberately no link-text fallback: a generic link label such as
    "Download" or a sidebar word must never become a filename.
    """
    html = resolve_path(site_root, str(record.get("html_path") or ""))
    if html is None or not html.is_file():
        return {}
    try:
        raw = html.read_bytes()
        lowered = raw.lower()
        if (
            b"topic" not in lowered
            and "विषय".encode("utf-8") not in raw
        ):
            return {}
        soup = BeautifulSoup(raw, "lxml")
        return extract_topic_download_names(
            soup, str(record.get("source_url") or "")
        )
    except Exception as exc:
        log(f"  WARN could not parse {html}: {exc}")
        return {}


def choose_new_path(old: Path, base: str, reserved: set[Path]) -> Path:
    """Pick a topic-named path that still fits inside Windows' path limit."""
    ext = old.suffix.lower()
    digest = old.name.split("_", 1)[0] if "_" in old.name else "file"
    number = 1
    while True:
        suffix = "" if number == 1 else f"-{number - 1}"
        budget = MAX_PATH_LEN - len(str(old.parent)) - 1
        if budget < 12:
            stem = digest
        else:
            stem = base[: max(1, budget - len(ext) - len(suffix))].strip(" .")
            if not stem:
                stem = digest
        candidate = old.with_name(f"{stem}{suffix}{ext}")
        key = candidate.resolve()
        if key not in reserved:
            if not candidate.exists() or filecmp.cmp(old, candidate, shallow=False):
                reserved.add(key)
                return candidate
        number += 1


def plan_record(
    record: dict, site_root: Path, reserved: set[Path], log=print
) -> list[dict]:
    names = topic_names_for(record, site_root, log)
    changes: list[dict] = []

    for url_field, path_field, name_field in PAIRS:
        urls = list(record.get(url_field) or [])
        paths = list(record.get(path_field) or [])

        # A failed download can make URLs and paths different lengths, so never
        # pair them by index.  The old filename starts with sha1(url)[:10].
        url_by_digest: dict[str, str] = {}
        index_by_digest: dict[str, int] = {}
        for position, url in enumerate(urls):
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
            url_by_digest.setdefault(digest, url)
            index_by_digest.setdefault(digest, position)

        for index, rel in enumerate(paths):
            old = resolve_path(site_root, str(rel))
            if old is None or not old.is_file():
                continue
            digest = old.name.split("_", 1)[0] if "_" in old.name else ""
            url = url_by_digest.get(digest, "")
            url_index = index_by_digest.get(digest, index)
            # Already-named files have no URL hash in the filename.  When the
            # URL and path lists line up, use that order so a bad earlier name
            # (for example "Tenders - <hash>") can be corrected in place.
            if (
                not url and not HASH_NAME.match(old.name)
                and len(urls) == len(paths) and index < len(urls)
            ):
                url = urls[index]
                url_index = index
            kind = "pdf" if path_field == "pdf_paths" else "document"
            topic = str(names.get(url) or "").strip()
            if not topic and url:
                topic = fallback_download_name(
                    record.get("title"), record.get("links"), url,
                    kind, str(record.get("prid") or ""),
                    text=record.get("text"),
                    index=url_index,
                )
            if len(topic) < 2:
                continue
            base = safe_topic_stem(topic, max_len=100, default=kind, seed=url)
            new = choose_new_path(old, base, reserved)
            if new.resolve() == old.resolve():
                continue
            changes.append({
                "url_field": url_field,
                "path_field": path_field,
                "name_field": name_field,
                "index": index,
                "old": old,
                "new": new,
                "new_rel": site_rel(site_root, new),
                "topic": topic,
                "url": url,
            })

    return changes


def apply_changes(changes: list[dict], log=print) -> tuple[list[dict], int]:
    """Copy new files; return only the changes that are safe to record."""
    done: list[dict] = []
    copied = 0
    for change in changes:
        old: Path = change["old"]
        new: Path = change["new"]
        if new.exists():
            done.append(change)  # already copied by an earlier partial run
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
            done.append(change)
            copied += 1
        except OSError as exc:
            log(f"  WARN could not copy {new}: {exc}")
    return done, copied


def update_record(record: dict, changes: list[dict]) -> None:
    for path_field in {c["path_field"] for c in changes}:
        name_field = next(c["name_field"] for c in changes
                          if c["path_field"] == path_field)
        paths = list(record.get(path_field) or [])
        names = list(record.get(name_field) or [])
        while len(names) < len(paths):
            names.append(Path(paths[len(names)]).name)
        for change in changes:
            if change["path_field"] != path_field:
                continue
            index = change["index"]
            paths[index] = change["new_rel"]
            names[index] = Path(change["new_rel"]).name
        record[path_field] = paths
        record[name_field] = names


def rename_target(
    target: Path | str,
    apply: bool = True,
    sample: int = 15,
    max_jsonls: int = 0,
    log=print,
) -> dict:
    """Rename PDF/document files in one ``categories`` folder.

    Returns a small stats dict so the web app can record what happened.
    Raises ``ValueError`` for an unsafe or missing target.
    """
    target = Path(target).expanduser().resolve()
    if not target.is_dir():
        raise ValueError(f"target folder not found: {target}")
    if PROTECTED.intersection(part.lower() for part in target.parts):
        raise ValueError(f"protected site in path, refusing to touch: {target}")

    site_root = target.parent
    jsonls = sorted(target.glob("*/pages.jsonl"))
    if max_jsonls > 0:
        jsonls = jsonls[:max_jsonls]
    if not jsonls:
        raise ValueError(f"no pages.jsonl files found under {target}")

    reserved: set[Path] = set()
    plans: list[tuple[Path, dict, list[dict]]] = []
    total_pdf_paths = total_doc_paths = 0
    scanned = 0

    for jsonl in jsonls:
        with jsonl.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                scanned += 1
                pdf_paths = record.get("pdf_paths") or []
                doc_paths = record.get("document_paths") or []
                total_pdf_paths += len(pdf_paths)
                total_doc_paths += len(doc_paths)
                if scanned % 1000 == 0:
                    log(f"  scanned {scanned} records...")
                # Most pages have no downloadable file; do not parse their HTML.
                if not pdf_paths and not doc_paths:
                    continue
                changes = plan_record(record, site_root, reserved, log)
                if changes:
                    plans.append((jsonl, record, changes))

    total_changes = sum(len(changes) for _, _, changes in plans)
    stats = {
        "target": str(target),
        "jsonl_files": len(jsonls),
        "pdf_paths": total_pdf_paths,
        "document_paths": total_doc_paths,
        "changes": total_changes,
        "applied_new_files": 0,
        "mode": "APPLY" if apply else "PREVIEW",
    }

    log(f"Target: {target}")
    log(f"JSONL files: {stats['jsonl_files']}")
    log(f"PDF paths found: {stats['pdf_paths']}")
    log(f"Document paths found: {stats['document_paths']}")
    log(f"Files to rename/copy: {stats['changes']}")
    log(f"Mode: {'APPLY' if apply else 'PREVIEW (no files changed)'}")
    log("")

    shown = 0
    for jsonl, record, changes in plans:
        for change in changes:
            if shown >= max(0, sample):
                break
            log(f"[{change['topic']}]")
            log(f"  from: {change['old']}")
            log(f"  to:   {change['new']}")
            shown += 1
        if shown >= max(0, sample):
            break

    if not apply:
        log("Preview only. Re-run with --apply to write changes.")
        return stats

    copied = 0
    touched: dict[Path, dict[tuple[str, str], list[dict]]] = {}
    for jsonl, record, changes in plans:
        key = (str(record.get("prid") or ""), str(record.get("source_url") or ""))
        touched.setdefault(jsonl, {})[key] = changes

    for jsonl, by_key in touched.items():
        lines: list[str] = []
        with jsonl.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    lines.append(line)
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    lines.append(line)
                    continue
                key = (str(record.get("prid") or ""), str(record.get("source_url") or ""))
                changes = by_key.get(key)
                if changes is not None:
                    done, new_copies = apply_changes(changes, log)
                    copied += new_copies
                    update_record(record, done)
                lines.append(json.dumps(
                    record, ensure_ascii=False, separators=(", ", ": ")
                ))
        tmp = jsonl.with_name(jsonl.name + ".tmp")
        tmp.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        os.replace(tmp, jsonl)

    stats["applied_new_files"] = copied
    log("")
    log(f"Applied. New files copied: {copied}")
    log("Original hashed files were kept. JSON URLs were not changed.")
    return stats


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=str(DEFAULT_TARGET),
                        help="categories folder to process")
    parser.add_argument("--apply", action="store_true",
                        help="actually copy files and update JSONL (default: preview)")
    parser.add_argument("--sample", type=int, default=15,
                        help="preview rows to print")
    parser.add_argument("--max-jsonls", type=int, default=0,
                        help="only process the first N JSONL files (quick check)")
    args = parser.parse_args()

    try:
        rename_target(
            args.target,
            apply=args.apply,
            sample=args.sample,
            max_jsonls=args.max_jsonls,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
