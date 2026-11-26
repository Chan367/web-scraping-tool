"""Read-only navigation discovery shared by the static and browser crawlers."""
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


CONTROLS = "button,[role=button],[role=tab],summary,[aria-controls],[onclick],input[type=button],a:not([href]),a[href^='#'],a[href^='javascript:']"
ROUTE_ATTRS = ("data-href", "data-url", "data-link", "data-route", "routerlink", "to")
LITERAL = re.compile(
    r"""(?:\b(?:window\.)?location(?:\.href)?\s*=|\b(?:window\.open|location\.assign|location\.replace|router\.push|navigate)\s*\()\s*(['"])([^'"]+)\1(?=\s*[,;) ]|\s*$)""")


def discover(soup, hyperlinks=True, buttons=True):
    """Yield declared destinations without evaluating arbitrary JS or submitting forms."""
    for node in soup.select("a,area,iframe[src],embed[src],object[data],link[rel],"
                            "[onclick],[data-href],[data-url],[data-link],[data-route],[routerlink],[to]"):
        label = node.get("aria-label") or node.get("title") or node.get_text(" ", strip=True)
        href = str(node.get("href", "")).strip()
        if node.has_attr("data-crawl-discovered") and buttons and href:
            yield href, label
        if hyperlinks and (node.name in ("a", "area") or
                           set(node.get("rel", ())) & {"next", "prev"}):
            if href and not href.startswith(("#", "javascript:")):
                yield href, label
        if node.name in ("iframe", "embed", "object"):
            yield node.get("src") or node.get("data"), label
        if buttons:
            for attr in ROUTE_ATTRS:
                value = node.get(attr)
                if value and not value.startswith(("#", "{", "[")):
                    yield value, label
            for match in LITERAL.finditer(node.get("onclick", "") + ";" +
                                          (href if href.startswith("javascript:") else "")):
                yield match[2], label


def explore(page, url, timeout_ms, buttons, allowed):
    """Observe navigation instead of letting a button replace the listing page.

    Route document requests (including popup requests) back through the crawl
    queue. Retain evidence from transient panels before another click hides it.
    """
    context = page.context
    found = {}
    origin = urlsplit(url).netloc
    initial = True

    def route(r):
        nonlocal initial
        req = r.request
        if req.method not in ("GET", "HEAD"):
            return r.abort()
        if req.is_navigation_request():
            if initial and req.url == url:
                initial = False
            else:
                found[req.url] = None
                return r.abort()
        if urlsplit(req.url).netloc != origin or not allowed(req.url):
            return r.abort()
        return r.continue_()

    context.route("**/*", route)
    page.on("download", lambda d: found.setdefault(d.url, None))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=min(3500, timeout_ms))
    except Exception:
        pass
    original = BeautifulSoup(page.content(), "lxml")
    extra = original.new_tag("section")
    seen = set()
    def capture(baseline=False):
        # Copy link/media elements with resolved URLs and table rows for Topic names.
        nodes = page.evaluate("""() => Array.from(document.querySelectorAll(
          'tr,a[href],area[href],img,video,audio,source,iframe,embed,object,[data-url],[data-href]'
        ), el => { const c = el.cloneNode(true);
          for (const n of [c,...c.querySelectorAll('*')]) for (const k of ['href','src','data','data-url','data-href'])
            if(n.hasAttribute(k)) {try { n.setAttribute(k,new URL(n.getAttribute(k), document.baseURI).href) } catch {}}
          return c.outerHTML })""")
        text = page.locator("body").inner_text(timeout=1500).splitlines()
        for kind, values in (("node", nodes), ("text", text)):
            for value in values:
                key = (kind, value)
                if key in seen:
                    continue
                seen.add(key)
                if baseline:
                    continue
                if kind == "text":
                    p = original.new_tag("p")
                    p.string = value
                    extra.append(p)
                else:
                    fragment = BeautifulSoup(value, "html.parser").find()
                    if fragment:
                        extra.append(fragment)

    capture(True)
    deadline = time.monotonic() + min(20, timeout_ms / 1000)
    previous_height = None
    for _ in range(80):
        if time.monotonic() >= deadline:
            break
        page.evaluate("window.scrollTo(0,document.documentElement.scrollHeight)")
        page.wait_for_timeout(200)
        capture()
        height = page.evaluate("document.documentElement.scrollHeight")
        grew = height != previous_height
        previous_height = height
        if not buttons:
            if grew:
                continue
            break
        # DOM identity, not labels: two anonymous sibling buttons are distinct.
        controls = page.locator(CONTROLS)
        acted = False
        for el in controls.element_handles():
            if time.monotonic() >= deadline:
                break
            if not el.is_visible() or not el.is_enabled():
                continue
            if not el.evaluate("""e => {
              if(e.closest('form,[contenteditable=true]') || e.matches('a[href]:not([href^="#"]):not([href^="javascript:"])')) return false;
              const visited = window.__crawlerVisited ||= new WeakMap();
              const states = visited.get(e) || new Set();
              const state = location.href + '\\n' + document.body.innerText + '\\n' +
                Array.from(document.links, a => a.href).join('\\n');
              if(states.has(state)) return false;
              states.add(state); visited.set(e,states); return true;
            }"""):
                continue
            try:
                el.click(timeout=1000, no_wait_after=True)
                page.wait_for_timeout(250)
                if page.url != url:
                    found[page.url] = None
                capture()
            except Exception:
                pass
            acted = True
            break
        if not acted and not grew:
            break
    for target in found:
        if urlsplit(urljoin(url, target)).scheme in ("http", "https"):
            a = original.new_tag("a", href=target)
            a["data-crawl-discovered"] = "button"
            extra.append(a)
    (original.select_one("main,article") or original.body or original).append(extra)
    return str(original)
