"""Reusable latest-wins background worker used by preload/export/decode."""

from __future__ import annotations

from threading import Condition, Lock, Thread
from typing import Any, Callable


class LatestOnlyWorker:
    """Run jobs on one daemon thread; a new job replaces any not-yet-started one."""

    def __init__(self, name: str) -> None:
        self._lock = Lock()
        self._cond = Condition(self._lock)
        self._job: tuple[int, Callable[..., Any], tuple[Any, ...], dict[str, Any]] | None = None
        self._closed = False
        self._thread = Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, generation: int, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        with self._cond:
            if self._closed:
                return
            self._job = (generation, fn, args, kwargs)
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._job = None
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._job is None and not self._closed:
                    self._cond.wait()
                if self._closed and self._job is None:
                    return
                generation, fn, args, kwargs = self._job  # type: ignore[misc]
                self._job = None
            try:
                fn(generation, *args, **kwargs)
            except Exception:
                # Workers must not die on a single decode failure; UI surfaces errors via events.
                pass
