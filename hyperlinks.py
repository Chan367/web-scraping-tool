"""Declared HTML navigation, independent of JavaScript control discovery."""


def discover_links(soup):
    for node in soup.select("a[href],area[href],link[rel~=next],link[rel~=prev]"):
        href = node["href"].strip() if node.has_attr("href") else ""
        if href and (href.startswith(("#/", "#!/")) or not href.lower().startswith(("#", "javascript:"))):
            yield href, node.get("aria-label") or node.get("title") or node.get_text(" ", strip=True)


def viewer_sources(soup):
    for node in soup.select("iframe[src],embed[src],object[data]"):
        yield node.get("src") or node.get("data"), node.get("title", "")
