"""Read-only navigation discovery shared by the static and browser crawlers."""
import re
import time
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup
from hyperlinks import discover_links, viewer_sources


CONTROLS = "button,[role=button],[role=tab],summary,[aria-controls],[onclick],input[type=button],a:not([href]),a[href^='#'],a[href^='javascript:']"
ROUTE_ATTRS = ("data-href", "data-url", "data-link", "data-route", "routerlink", "to")
LITERAL = re.compile(
    r"""(?:\b(?:window\.)?location(?:\.href)?\s*=|\b(?:window\.open|location\.assign|location\.replace|router\.push|navigate)\s*\()\s*(['"])([^'"]+)\1(?=\s*[,;) ]|\s*$)""")


def discover(soup, hyperlinks=True, buttons=True):
    """Yield declared destinations without evaluating arbitrary JS or submitting forms."""
    if hyperlinks:
        yield from discover_links(soup)
    yield from viewer_sources(soup)
    if buttons:
        yield from discover_buttons(soup)


def discover_buttons(soup):
    for node in soup.select("a[href^='javascript:' i],[data-crawl-discovered],"
                            "[onclick],[data-href],[data-url],[data-link],[data-route],[routerlink],[to]"):
        label = node.get("aria-label") or node.get("title") or node.get_text(" ", strip=True)
        href = str(node.get("href", "")).strip()
        if node.has_attr("data-crawl-discovered") and href:
            yield href, label
        for attr in ROUTE_ATTRS:
            value = node.get(attr)
            if value and not value.startswith(("#", "{", "[")):
                yield value, label
        for match in LITERAL.finditer(node.get("onclick", "") + ";" +
                                      (href if href.lower().startswith("javascript:") else "")):
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
            if initial and urldefrag(req.url).url == urldefrag(url).url:
                initial = False
            else:
                found[req.url] = None
                # A 204 keeps the current document alive; aborting replaces it
                # with Chromium's error document and loses sibling controls.
                return r.fulfill(status=204)
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
          'nav,[role=navigation],[role=menu],tr,a[href],area[href],link[rel],img,video,audio,source,iframe,embed,object,[data-url],[data-href],[data-route],[onclick],[routerlink]'
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
    deadline = time.monotonic() + timeout_ms / 1000
    previous_height = None
    while time.monotonic() < deadline:
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
        # Prefer newly revealed children over switches that could hide them.
        # Among siblings try untouched controls before revisiting pagination.
        handle = page.evaluate_handle("""selector => {
          const history = window.__crawlControlHistory ||= new WeakMap();
          const previous = window.__crawlVisible || new Set();
          const visible = [...document.querySelectorAll(selector)].filter(e =>
            e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' &&
            !e.matches(':disabled,[aria-disabled=true]') &&
            !e.closest('form,[contenteditable=true]') && !e.matches('a[href^="#/"],a[href^="#!/"]') &&
            !e.matches('a[href]:not([href^="#"]):not([href^="javascript:" i])'));
          window.__crawlVisible = new Set(visible);
          const state = JSON.stringify([location.href, document.body.innerText,
                                        Array.from(document.links, a => a.href)]);
          const candidates = visible.filter(e => !history.get(e)?.has(state));
          candidates.sort((a,b) => Number(previous.has(a))-Number(previous.has(b)) ||
                             (history.get(a)?.size || 0)-(history.get(b)?.size || 0));
          const el = candidates[0];
          if (!el) return null;
          const states = history.get(el) || new Set();
          states.add(state); history.set(el, states);
          return el;
        }""", CONTROLS)
        try:
            el = handle.as_element()
            if el is None:
                if grew:
                    continue
                break
            el.click(timeout=1000, no_wait_after=True)
            page.wait_for_timeout(250)
            if page.url != url:
                found[page.url] = None
            capture()
        except Exception:
            # A control may detach while a framework redraws its parent.
            if page.is_closed():
                break
        finally:
            handle.dispose()
    for target in found:
        if urlsplit(urljoin(url, target)).scheme in ("http", "https"):
            a = original.new_tag("a", href=target)
            a["data-crawl-discovered"] = "button"
            extra.append(a)
    (original.select_one("main,article") or original.body or original).append(extra)
    return str(original)
