"""Run Anki-facing work on Anki's main thread.

Anki's collection and GUI are not thread-safe: touching them from the HTTP
server's thread makes Anki react from the wrong thread, which shows up as
strange GUI/audio behaviour rather than a clean error. AnkiConnect avoids this
by driving its whole server from a `QTimer` on the main thread; we keep the
stdlib server but hand every Anki call to the main thread and wait for it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

DEFAULT_TIMEOUT = 10.0


class MainThreadRunner:
    """Marshals callables onto Anki's main thread."""

    def __init__(
        self,
        post: Callable[[Callable[[], None]], None],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._post = post
        self._timeout = timeout

    @classmethod
    def for_anki(cls) -> MainThreadRunner:
        """Runner backed by `mw.taskman.run_on_main`."""
        from aqt import mw

        return cls(mw.taskman.run_on_main)

    def call(self, work: Callable[[], T]) -> T:
        """Run `work` on the main thread and return (or re-raise) its result."""
        if threading.current_thread() is threading.main_thread():
            return work()

        outcome: dict[str, Any] = {}
        done = threading.Event()

        def job() -> None:
            try:
                outcome["value"] = work()
            except BaseException as error:  # noqa: BLE001 - re-raised in the caller
                outcome["error"] = error
            finally:
                done.set()

        self._post(job)
        if not done.wait(self._timeout):
            raise TimeoutError("Anki's main thread did not pick up the request in time")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]
