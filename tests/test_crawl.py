"""Offline integration fixtures: two hosts, real crawler CLI and output files."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path
        self.server.requests.append((self.headers["Host"], path))
        offsite = f"http://localhost:{self.server.server_port}"
        pages = {
            "/options": '<a href="/private">private</a><a href="/jump">jump</a>'
                        '<button data-url="/hidden">hidden</button><table><tr><th>विषय</th><th>File</th></tr>'
                        '<tr><td>Report</td><td><a href="/two.pdf">2</a></td></tr>'
                        '<tr><td>Report</td><td><a href="/one.pdf">1</a></td></tr></table>'
                        '<a href="/item.docx">Item</a>',
            "/": '<nav><ul><li><a href="/records">Records</a></li><li><a href="/news">News</a></li></ul></nav>',
            "/records": '<a href="?p=2">2</a><a href="/one.pdf">one</a>',
            "/records?p=2": '<button onclick="window.open(\'/records/detail\')">◇</button>',
            "/records/detail": '<iframe src="/viewer"></iframe>',
            "/viewer": '<a href="/two.pdf">Two</a><a href="/item.docx">Item</a>'
                       f'<button data-href="{offsite}/external">↗</button>',
            "/news": '<a href="/records">cycle</a>',
            "/external": '<a href="/external/deeper">sub</a>',
            "/external/deeper": '<a href="/off.pdf">report</a>',
            "/popup": '<button id="open">◇</button><script>'
                      f'document.querySelector("button").onclick=()=>window.open(new URL("/external", "{offsite}"))'
                      '</script>',
            "/new": '<a href="/new.pdf">New attachment</a>',
            "/js": '''<script>document.addEventListener('DOMContentLoaded', () => {
              document.querySelector('button').onclick = () => {
                document.querySelector('section').innerHTML = '<button id="child">◇</button><a href="/one.pdf">one</a>';
                document.querySelector('#child').onclick = () => {
                  document.querySelector('section').innerHTML = '<iframe src="/viewer"></iframe>';
                };
              };
            })</script><button>◎</button><section></section>''',
        }
        if path == "/jump":
            self.send_response(302)
            self.send_header("Location", offsite + "/external")
            self.end_headers()
            return
        if path == "/robots.txt":
            body, mime = b"User-agent: *\nDisallow: /private\n", "text/plain"
        elif path in pages:
            more = '<a href="/new">new</a>' if path == "/records" and self.server.updated else ""
            body = ('<html><head><title>Index</title></head><body><main>' +
                    "Readable page. " * 30 + pages[path] + more + '</main></body></html>').encode()
            mime = "text/html; charset=utf-8"
        elif path.endswith((".pdf", ".docx")):
            version = '2' if self.server.updated and path == '/two.pdf' else '1'
            if self.headers.get("If-None-Match") == version:
                self.send_response(304)
                self.end_headers()
                self.server.not_modified.append(path)
                return
            body, mime = (path + version).encode(), ("application/pdf" if path.endswith(".pdf") else
                                                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        else:
            self.send_error(404)
            return
        self.send_response(200)
        if path.endswith((".pdf", ".docx")):
            self.send_header("ETag", version)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Crawl(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        self.server.requests = []
        self.server.not_modified = []
        self.server.updated = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def crawl(self, *flags, start="/", browser=False):
        cmd = [sys.executable, str(ROOT/"scraper.py"), "--url",
               f"http://127.0.0.1:{self.server.server_port}{start}", "--root-dir", self.tmp.name,
               "--delay", "0.01", "--workers", "3", "--no-language-switch", "--no-translate",
               "--no-pdf-text", "--no-excel", *([] if browser else ["--no-js"]), *flags]
        result = subprocess.run(cmd, capture_output=True, timeout=150)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        files = list(Path(self.tmp.name).glob("*/categories/*/pages.jsonl"))
        rows = [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), len({r["source_url"] for r in rows}))
        self.assertNotIn(b"Unhandled error", result.stderr)
        return rows

    def test_internal_refresh(self):
        rows = self.crawl()
        self.assertEqual(len(rows), 6)
        requests = [p for _,p in self.server.requests]
        self.assertLess(requests.index("/viewer"), requests.index("/news"))
        self.assertNotIn("/external", requests)
        self.assertEqual(sum(len(r["pdf_paths"]) for r in rows), 2)
        self.server.updated = True
        # Simulate a saved but unfinished queue, with records already on disk.
        state_path = next(Path(self.tmp.name).rglob("state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(completed=False, queue=[{"url": rows[0]["source_url"], "link_only": True}])
        state_path.write_text(json.dumps(state), encoding="utf-8")
        rows = self.crawl("--refresh-existing", "--refresh-media")
        self.assertEqual(len(rows), 7)
        self.assertIn("/one.pdf", self.server.not_modified)
        files = list(Path(self.tmp.name).rglob("*.pdf"))
        self.assertEqual({p.read_bytes() for p in files}, {b"/one.pdf1", b"/two.pdf2", b"/new.pdf1"})

    def test_full(self):
        rows = self.crawl("--follow-external", "--max-depth", "0", "--no-documents")
        self.assertEqual(len(rows), 8)
        self.assertEqual(sum(len(r["pdf_paths"]) for r in rows), 3)
        self.assertNotIn("/item.docx", [p for _,p in self.server.requests])

    def test_external(self):
        rows = self.crawl("--external-only", "--follow-external")
        self.assertEqual({urlsplit(r["source_url"]).path for r in rows}, {"/external", "/external/deeper"})
        self.assertEqual(sum(len(r["pdf_paths"]) for r in rows), 1)

    def test_selections(self):
        rows = self.crawl("--no-text", "--no-hyperlinks", "--no-buttons",
                          "--formats", "document:xlsx", start="/options")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "")
        self.assertEqual(rows[0]["links"], [])
        self.assertEqual(set(rows[0]["pdf_names"]), {"Report.pdf", "Report-1.pdf"})
        self.assertEqual(set(rows[0]["pdf_files"]), {
            f"http://127.0.0.1:{self.server.server_port}/{n}.pdf" for n in ("one", "two")})
        self.assertFalse({"/private", "/jump", "/hidden", "/item.docx"} &
                         {p for _,p in self.server.requests})

    def test_redirect_and_robots(self):
        self.crawl("--no-media", "--no-buttons", start="/options")
        requests = {p for _,p in self.server.requests}
        self.assertIn("/jump", requests)
        self.assertFalse({"/external", "/private", "/hidden", "/one.pdf"} & requests)

    def test_browser(self):
        rows = self.crawl("--max-pages", "2", start="/js", browser=True)
        self.assertTrue(any(r["pdf_paths"] for r in rows))
        self.assertIn("/viewer", [urlsplit(r["source_url"]).path for r in rows])

    def test_external_popup(self):
        rows = self.crawl("--external-only", "--follow-external", start="/popup", browser=True)
        self.assertEqual({urlsplit(r["source_url"]).path for r in rows},
                         {"/external", "/external/deeper"})


if __name__ == "__main__":
    unittest.main()
