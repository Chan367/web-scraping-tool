"""Turn scraped records into whatever JSON shape the user pasted.

The user writes a sample object (or array) in the UI. Every string that is a
known field name, or contains ``{{field}}``, is replaced with the real value.

    {"title": "title", "url": "{{source_url}}", "images": []}

becomes

    {"title": "Actual page title", "url": "https://...", "images": ["https://..."]}
"""
from __future__ import annotations

import json
import re

# Names people naturally write -> real record field
ALIASES = {
    "id": "__index__",
    "sno": "__index__",
    "no": "__index__",
    "index": "__index__",
    "url": "source_url",
    "link": "source_url",
    "page_url": "source_url",
    "source": "source_url",
    "source_url": "source_url",
    "title": "title",
    "heading": "title",
    "name": "title",
    "text": "text",
    "content": "text",
    "description": "text",
    "body": "text",
    "date": "date_time",
    "date_time": "date_time",
    "datetime": "date_time",
    "published": "date_time",
    "published_at": "date_time",
    "website": "website_name",
    "site": "website_name",
    "website_name": "website_name",
    "domain": "website_name",
    "images": "image_url",
    "image": "image_url",
    "image_url": "image_url",
    "images_url": "image_url",
    "pdf": "pdf_files",
    "pdfs": "pdf_files",
    "pdf_files": "pdf_files",
    "pdf_links": "pdf_files",
    "audio": "audio_files",
    "audios": "audio_files",
    "audio_files": "audio_files",
    "audio_links": "audio_files",
    "video": "video_files",
    "videos": "video_files",
    "video_files": "video_files",
    "video_links": "video_files",
    "documents": "document_files",
    "document": "document_files",
    "docs": "document_files",
    "document_files": "document_files",
    "document_links": "document_files",
    "language": "language",
    "lang": "language",
    "collected_at": "collected_at",
    "scraped_at": "collected_at",
    "scraped_on": "collected_at",
    "prid": "prid",
}

TOKEN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def parse_template(text: str) -> dict:
    """Return the prototype object (first element when the sample is a list)."""
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Sample JSON is not valid: line {exc.lineno}, column {exc.colno}: {exc.msg}")
    if isinstance(data, list):
        if not data:
            raise ValueError("Sample JSON is an empty array - add one example object inside it.")
        proto = data[0]
    else:
        proto = data
    if not isinstance(proto, dict):
        raise ValueError("Sample JSON must be an object or an array of objects.")
    return proto


def _resolve(name: str, record: dict, index: int):
    field = ALIASES.get(name.lower(), name)
    if field == "__index__":
        return index
    if field in record:
        return record[field]
    return None


def _fill(node, record: dict, index: int, key: str | None = None):
    if isinstance(node, dict):
        return {k: _fill(v, record, index, k) for k, v in node.items()}
    if isinstance(node, list):
        if not node:
            # "image_url": [] -> fill with the matching list field
            if key:
                value = _resolve(key, record, index)
                if isinstance(value, list):
                    return value
            return []
        return [_fill(node[0], record, index, key)]
    if isinstance(node, str):
        # whole string is a {{token}}
        m = TOKEN.fullmatch(node.strip())
        if m:
            return _resolve(m.group(1), record, index)
        # inline tokens inside a longer string
        if TOKEN.search(node):
            def repl(match):
                value = _resolve(match.group(1), record, index)
                if isinstance(value, list):
                    return ", ".join(str(v) for v in value)
                return "" if value is None else str(value)
            return TOKEN.sub(repl, node)
        # bare field name
        if node.strip().lower() in ALIASES or node in record:
            value = _resolve(node.strip(), record, index)
            if value is not None:
                return value
        return node
    # numeric id placeholders: {"id": 1} -> running number
    if isinstance(node, (int, float)) and key and key.lower() in (
            "id", "sno", "sr", "srno", "sr_no", "no", "index", "number", "num"):
        return index
    return node


def render(records: list[dict], template_text: str) -> list[dict]:
    proto = parse_template(template_text)
    if not proto:
        return records
    out = []
    for i, record in enumerate(records, 1):
        item = _fill(proto, record, i)
        if not isinstance(item, dict):
            item = {"value": item}
        out.append(item)
    return out


def preview(template_text: str) -> dict:
    sample = {
        "prid": "06635a71a73a",
        "title": "Example page title",
        "text": "Example body text from the page.",
        "date_time": "2024-07-15",
        "source_url": "https://example.gov.in/page",
        "website_name": "example.gov.in",
        "image_url": ["https://example.gov.in/a.jpg"],
        "pdf_files": ["https://example.gov.in/a.pdf"],
        "audio_files": ["https://example.gov.in/a.mp3"],
        "video_files": ["https://example.gov.in/a.mp4"],
        "document_files": ["https://example.gov.in/a.xlsx"],
        "language": "English",
        "collected_at": "2026-11-18T10:00:00Z",
    }
    return render([sample], template_text)[0]
