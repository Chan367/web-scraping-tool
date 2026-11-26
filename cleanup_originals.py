"""Delete old hash-named downloads after topic/fallback renaming.

Preview mode is the default.  An original file is deleted only when a new
topic/fallback-named copy with identical bytes is present and referenced in
``pages.jsonl``.  Files without a verified copy are left alone.

The web app also calls :func:`cleanup_target` after the automatic rename when
a stopped job is resumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

DEFAULT_TARGET = Path(
    r"C:\Users\chan8\OneDrive\Desktop\sample_web_sit_test\Scraped_Data\dare_gov_in\categories"
)
PROTECTED = {"india_gov_in", "nhm_gov_in"}
HASH_NAME = re.compile(r"^[0-9a-f]{10}_")
PATH_FIELDS = ("pdf_paths", "document_paths")
DOWNLOAD_DIRS = ("/media/pdfs/", "/media/documents/")
DOCUMENT_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".zip", ".csv", ".txt", ".bin", ".exe", ".xml",
}


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_key(path: Path) -> tuple[str, str, int, str]:
    stat = path.stat()
    return (
        str(path.parent.resolve()),
        path.suffix.lower(),
        stat.st_size,
        file_digest(path),
    )


def referenced_new_files(target: Path) -> set[Path]:
    site_root = target.parent
    refs: set[Path] = set()
    for jsonl in sorted(target.glob("*/pages.jsonl")):
        with jsonl.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for field in PATH_FIELDS:
                    for rel in record.get(field) or []:
                        if not rel:
                            continue
                        path = Path(str(rel).replace("\\", "/"))
                        if not path.is_absolute():
                            path = site_root / path
                        refs.add(path.resolve())
    return refs


def cleanup_target(
    target: Path | str,
    apply: bool = True,
    sample: int = 10,
    log=print,
) -> dict:
    """Delete verified duplicate originals in one ``categories`` folder.

    Raises ``ValueError`` for an unsafe target or missing named files.
    """
    target = Path(target).expanduser().resolve()
    if not target.is_dir():
        raise ValueError(f"folder not found: {target}")
    if PROTECTED.intersection(part.lower() for part in target.parts):
        raise ValueError(f"protected site in path, refusing: {target}")

    refs = referenced_new_files(target)
    missing_refs = [p for p in refs if not p.is_file()]
    if missing_refs:
        raise ValueError(f"{len(missing_refs)} JSON path targets are missing")

    verified_keys: set[tuple[str, str, int, str]] = set()
    for path in refs:
        if HASH_NAME.match(path.name):
            continue
        verified_keys.add(copy_key(path))

    to_delete: list[Path] = []
    kept: list[Path] = []
    old_count = 0

    for path in target.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        posix = resolved.as_posix()
        if not any(folder in posix for folder in DOWNLOAD_DIRS):
            continue
        if path.suffix.lower() not in DOCUMENT_EXTS:
            continue
        old_count += 1
        if resolved in refs:
            kept.append(path)
            continue
        if copy_key(path) in verified_keys:
            to_delete.append(path)
        else:
            kept.append(path)

    stats = {
        "target": str(target),
        "referenced_named_files": len(refs),
        "old_unreferenced_files": old_count,
        "safe_to_delete": len(to_delete),
        "kept_without_copy": len(kept),
        "deleted": 0,
        "failed": 0,
        "mode": "APPLY" if apply else "PREVIEW",
    }

    log(f"Target: {target}")
    log(f"Referenced named files: {stats['referenced_named_files']}")
    log(f"Old unreferenced downloads: {stats['old_unreferenced_files']}")
    log(f"Safe to delete: {stats['safe_to_delete']}")
    log(f"Kept without verified copy: {stats['kept_without_copy']}")
    log(f"Mode: {'APPLY' if apply else 'PREVIEW (no files deleted)'}")
    log("")

    if kept and sample > 0:
        log("Examples kept:")
        for path in kept[:sample]:
            log(f"  {path}")
        log("")

    if not apply:
        log("Preview only. Re-run with --apply to delete the verified files.")
        return stats

    deleted = 0
    failed = 0
    for path in to_delete:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            failed += 1
            log(f"  WARN could not delete {path}: {exc}")

    still_missing = sum(1 for path in refs if not path.is_file())
    stats["deleted"] = deleted
    stats["failed"] = failed
    stats["missing_after_cleanup"] = still_missing
    log("")
    log(f"Deleted: {deleted}")
    log(f"Failed: {failed}")
    log(f"Named files still missing after cleanup: {still_missing}")
    return stats


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=str(DEFAULT_TARGET))
    parser.add_argument("--apply", action="store_true",
                        help="delete verified old files (default: preview only)")
    parser.add_argument("--sample", type=int, default=10)
    args = parser.parse_args()

    try:
        stats = cleanup_target(
            args.target,
            apply=args.apply,
            sample=args.sample,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    if not args.apply:
        return 0
    if stats["failed"] or stats.get("missing_after_cleanup", 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
