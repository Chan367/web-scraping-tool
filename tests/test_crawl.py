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
            "/catalog": '<nav><ul><li><button>Sections</button><ul>'
                        '<li><a href="/shelves">Archives</a></li>'
                        '<li><a href="/shelves?group=B">Bulletins</a></li>'
                        '</ul></li></ul></nav>',
            "/shelves/": ''.join(f'<a href="item?n=1&n={i}">item {i}</a>'
                                 for i in range(9)),
            "/shelves?group=B": '<a href="/bulletin">Bulletin detail</a>',
            "/bulletin": '',
            "/switches": '<button id="a">A</button><button id="b">B</button>'
                         '<script>for(const id of ["a","b"]) document.getElementById(id).onclick=()=>{'
                         'location.href="/chosen/"+id;}</script>',
            "/chosen/a": '', "/chosen/b": '',
            "/app": '<nav><a href="#/library">Library</a></nav>'
                    '<a id="original" href="/bulletin">Server link</a>'
                    '<script>document.querySelector("#original").remove();'
                    'if(location.hash==="#/library")document.write(\'<a href="/shelf">Shelf</a>\');</script>',
            "/shelf": '',
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
        if path == "/shelves":
            self.send_response(301)
            self.send_header("Location", "/shelves/")
            self.end_headers()
            return
        if path.startswith("/shelves/item?") or path.startswith("/step/"):
            level = int(path.rsplit("/", 1)[1]) if path.startswith("/step/") else 0
            last = 8 if self.server.updated else 7
            pages[path] = f'<a href="/step/{level + 1}">Next</a>' if level < last else ''
        if path == "/jump":
            self.send_response(302)
            self.send_header("Location", offsite + "/external")
            self.end_headers()
            return
        if path == "/robots.txt" and getattr(self.server, "redirect_robots", False):
            self.send_response(302)
            self.send_header("Location", "/robots-final.txt")
            self.end_headers()
            return
        if path in ("/robots.txt", "/robots-final.txt"):
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
        self.server.redirect_robots = False
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

    def test_web_full_after_limited_internal(self):
        # Exercise the same command builder the Flask job runner calls.
        sys.path.insert(0, str(ROOT / "webapp"))
        try:
            import runner
        finally:
            sys.path.pop(0)
        self.crawl("--max-pages", "2")
        cmd = runner.build_command(self.base + "/", {
            "modes": {"full": True}, "limits": {"delay": 0.5, "workers": 3},
            "types": {"text": True},
        }, root_dir=Path(self.tmp.name))
        result = subprocess.run(cmd + ["--no-js", "--no-excel", "--no-language-switch", "--no-translate"],
                                capture_output=True, timeout=100)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        rows = [json.loads(line) for path in Path(self.tmp.name).rglob("pages.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({row["source_url"] for row in rows}), 8)
        self.assertTrue(any(row["source_url"].endswith("/external/deeper") for row in rows))

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

    def test_robots_redirect(self):
        # dof.gov.in redirects its robots.txt to www.dof.gov.in.  A 302 must
        # not make the crawler treat the whole origin as disallowed.
        self.server.redirect_robots = True
        rows = self.crawl("--no-media", "--no-buttons", start="/options")
        paths = {path for _, path in self.server.requests}
        self.assertIn("/robots-final.txt", paths)
        self.assertIn("/options", paths)
        self.assertNotIn("/private", paths)
        self.assertEqual(len(rows), 1)

    def test_browser(self):
        rows = self.crawl("--max-pages", "2", start="/js", browser=True)
        self.assertTrue(any(r["pdf_paths"] for r in rows))
        self.assertIn("/viewer", [urlsplit(r["source_url"]).path for r in rows])

    def test_deep_category_fanout(self):
        rows = self.crawl("--follow-external", start="/catalog")
        by_category = {}
        for row in rows:
            by_category.setdefault(row["category"], set()).add(row["source_url"].split(self.base, 1)[1])
        self.assertEqual(by_category["Archives"], {"/shelves/"} |
                         {f"/shelves/item?n=1&n={i}" for i in range(9)} |
                         {f"/step/{i}" for i in range(1, 8)})
        self.assertEqual(by_category["Bulletins"], {"/shelves?group=B", "/bulletin"})
        paths = [p for _, p in self.server.requests]
        self.assertLess(paths.index("/step/7"), paths.index("/shelves?group=B"))
        self.server.updated = True
        rows = self.crawl("--rediscover", "--follow-external", start="/catalog")
        self.assertIn("/step/8", {urlsplit(row["source_url"]).path for row in rows})

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def test_sibling_navigation_buttons(self):
        rows = self.crawl(start="/switches", browser=True)
        self.assertEqual({urlsplit(row["source_url"]).path for row in rows},
                         {"/switches", "/chosen/a", "/chosen/b"})

    def test_client_route_and_server_links(self):
        rows = self.crawl("--render-budget", "0", start="/app", browser=True)
        self.assertEqual({row["source_url"][len(self.base):] for row in rows},
                         {"/app", "/app#/library", "/shelf", "/bulletin"})

    def test_external_popup(self):
        rows = self.crawl("--external-only", "--follow-external", start="/popup", browser=True)
        self.assertEqual({urlsplit(r["source_url"]).path for r in rows},
                         {"/external", "/external/deeper"})


if __name__ == "__main__":
    unittest.main()
