"""Small cache facade: Redis when REDIS_URL is set, otherwise an in-process TTL dict.

Analysis results contain dates and decrypted values, so with Redis they are pickled and
kept only for a short TTL inside the private network; the in-memory default never leaves the process.
"""
from __future__ import annotations

import pickle
import threading
import time

from .config import REDIS_URL

TTL_SECONDS = 300


class _MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, value = item
            if expires < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: object, ttl: int = TTL_SECONDS) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class _RedisCache:
    def __init__(self, url: str) -> None:
        import redis  # imported lazily so Redis stays optional

        self._r = redis.Redis.from_url(url)

    def get(self, key: str):
        raw = self._r.get(key)
        return pickle.loads(raw) if raw else None  # noqa: S301 - only data this service wrote

    def set(self, key: str, value: object, ttl: int = TTL_SECONDS) -> None:
        self._r.setex(key, ttl, pickle.dumps(value))

    def delete(self, key: str) -> None:
        self._r.delete(key)


def _build():
    if REDIS_URL:
        try:
            c = _RedisCache(REDIS_URL)
            c._r.ping()
            return c
        except Exception:  # noqa: BLE001 - fall back to memory if Redis is unreachable
            pass
    return _MemoryCache()


cache = _build()
