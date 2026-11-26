"""Slow status probes answered from a cache. A probe of a local port that nothing listens on takes about 2 s on Windows
(the connect is retried before it is refused), and the window's state call must never wait for that: the last answer is
returned at once and a stale one is refreshed in the background."""
import threading
import time


class Cached:
    def __init__(self, fn, ttl=5.0, default=None):
        self.fn, self.ttl, self.default = fn, ttl, default
        self.value, self.at = default, 0.0
        self.lock = threading.Lock()
        self.running = None              # the refresh in progress (a thread), if any

    def get(self, wait=0.0):
        """The last answer. A stale one starts a background refresh; with no answer yet, wait up to `wait` seconds for
        the first one."""
        with self.lock:
            fresh = time.time() - self.at < self.ttl
            t = self.running
            if not fresh and not (t and t.is_alive()):
                t = self.running = threading.Thread(target=self.refresh, daemon=True)
                t.start()
            first = self.at == 0.0
        if first and wait and t:
            t.join(wait)
        return self.value

    def refresh(self):
        """Probe now (blocking) and remember the answer."""
        try:
            v = self.fn()
        except Exception:
            v = self.default
        with self.lock:
            self.value, self.at = v, time.time()
        return v
