"""Coordinator-owned page frontier. Workers return children; they never mutate this queue."""
from collections import deque
import logging


class CrawlQueue:
    def __init__(self, items, category_complete, internal_first):
        self.category_complete = category_complete
        self.internal_first = internal_first
        self.groups = {}
        self.active = None
        for item in items:
            self.append(item)

    def append(self, item):
        if self.category_complete:
            key = item.category_hint.casefold()
        elif self.internal_first:
            key = item.external
        else:
            key = "fifo"
        if key not in self.groups:
            self.groups[key] = deque()
        self.groups[key].append(item)

    def __bool__(self):
        return bool(self.groups)

    def __iter__(self):
        # Used by checkpoints; keep the current category first on resume.
        yield from self.groups.get(self.active, ())
        for key, group in self.groups.items():
            if key != self.active:
                yield from group

    def take(self, busy):
        if self.category_complete and self.active in self.groups:
            key = self.active
        elif self.category_complete and busy:
            # An active worker may return more descendants after its downloads.
            return None
        else:
            key = next(iter(self.groups))
            if self.internal_first or self.category_complete:
                key = next((k for k, group in self.groups.items() if not group[0].external), key)
            if self.category_complete:
                logging.getLogger("site_scraper").info("Crawling category: %s", self.groups[key][0].category_hint)
            self.active = key
        item = self.groups[key].popleft()
        if not self.groups[key]:
            del self.groups[key]
        return item
