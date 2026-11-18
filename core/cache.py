# -*- coding: utf-8 -*-
"""
Thread-safe bounded in-memory cache with OrderedDict LRU eviction and TTL expiration.
"""

import time
import threading
from collections import OrderedDict
from typing import Any, Optional


class BoundedTTLCache:
    """Thread-safe bounded in-memory cache with OrderedDict LRU eviction and TTL expiration."""

    def __init__(self, max_size: int = 2048, ttl_seconds: float = 86400.0):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    @property
    def maxsize(self) -> int:
        return self.max_size

    @property
    def ttl(self) -> float:
        return self.ttl_seconds

    def _purge_expired(self, now: float):
        while self._cache:
            k, (_, exp) = next(iter(self._cache.items()))
            if exp <= now:
                del self._cache[k]
            else:
                break

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            now = time.time()
            if key not in self._cache:
                return default
            val, exp = self._cache[key]
            if exp <= now:
                del self._cache[key]
                return default
            self._cache.move_to_end(key)
            return val

    def __getitem__(self, key: str) -> Any:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def incr(self, key: str, amount: int = 1, ttl_seconds: Optional[float] = None) -> int:
        """Fixed-window counter: the TTL starts on the first hit and is NOT extended.

        Plain ``get`` + ``__setitem__`` refreshes the expiry on every request, so a
        client that polls faster than the window never lets its counter expire and
        eventually locks itself out forever. Use this for rate limiters.
        """
        with self._lock:
            now = time.time()
            ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
            entry = self._cache.get(key)
            if entry is not None and entry[1] > now:
                value, expires_at = entry
                self._cache[key] = (value + amount, expires_at)
                self._cache.move_to_end(key)
                return value + amount
            if key in self._cache:
                del self._cache[key]
            self._purge_expired(now)
            if len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            self._cache[key] = (amount, now + ttl)
            return amount

    def __setitem__(self, key: str, value: Any):
        with self._lock:
            now = time.time()
            self._purge_expired(now)
            if key in self._cache:
                del self._cache[key]
            elif len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            self._cache[key] = (value, now + self.ttl_seconds)
    def clear(self):
        with self._lock:
            self._cache.clear()

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired(time.time())
            return len(self._cache)
