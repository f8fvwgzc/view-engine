"""Tiny thread-safe in-memory TTL cache with per-key locks (no thundering herd)."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

# TTLs in seconds
TTL_INTRADAY = 60
TTL_DAILY = 30 * 60
TTL_OPTIONS = 5 * 60
TTL_CALENDAR = 30 * 60
TTL_MODEL = 6 * 60 * 60


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[Any, threading.Lock] = {}

    def get(self, key: Any) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            expires, value = hit
            if expires < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: Any, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)

    def _key_lock(self, key: Any) -> threading.Lock:
        with self._lock:
            lk = self._key_locks.get(key)
            if lk is None:
                lk = self._key_locks[key] = threading.Lock()
            return lk

    def get_or_set(self, key: Any, ttl: float, fn: Callable[[], Any]) -> Any:
        value = self.get(key)
        if value is not None:
            return value
        with self._key_lock(key):
            value = self.get(key)  # another thread may have filled it
            if value is not None:
                return value
            value = fn()
            if value is not None:
                self.set(key, value, ttl)
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._data)}


cache = TTLCache()
