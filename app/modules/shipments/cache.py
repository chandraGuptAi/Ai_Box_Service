"""Short-lived cache of cleaned fetch results, so "Run again" and follow-ups do not call Bigship again.

Keyed by a hash of the seller's token and the exact query: one seller never reads another's entry.
Only cleaned rows (no personal data) are stored, in memory, for ``ttl_seconds``.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any


class FetchCache:
    def __init__(self, ttl_seconds: float = 600, max_entries: int = 50) -> None:
        self.ttl, self.max_entries = ttl_seconds, max_entries
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(credential: str, query: dict[str, Any]) -> str:
        return hashlib.sha256((credential + "\0" + json.dumps(query, sort_keys=True)).encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if time.monotonic() - entry[0] > self.ttl:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return entry[1]

    def put(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._items[key] = (time.monotonic(), value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
