"""Tests for the main-thread marshalling helper (no Anki needed)."""

from __future__ import annotations

import queue as queue_module
import threading

import pytest

from anki_tui_bridge.main_thread import MainThreadRunner


class FakeMainThread:
    """Stands in for Anki's event loop: runs posted jobs when pumped."""

    def __init__(self) -> None:
        self.jobs: queue_module.Queue = queue_module.Queue()

    def post(self, job) -> None:
        self.jobs.put(job)

    def pump(self, count: int = 1) -> None:
        for _ in range(count):
            self.jobs.get(timeout=1)()


def test_work_runs_on_the_main_thread_and_returns_its_value() -> None:
    main = FakeMainThread()
    runner = MainThreadRunner(main.post)
    outcome: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            outcome.append(runner.call(lambda: 42))
        except BaseException as error:  # pragma: no cover - failure path
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    main.pump()
    thread.join(timeout=2)

    assert outcome == [42]
    assert errors == []
    assert main.jobs.empty()


def test_exceptions_propagate_to_the_caller() -> None:
    main = FakeMainThread()
    runner = MainThreadRunner(main.post)
    errors: list[BaseException] = []

    def boom() -> None:
        raise RuntimeError("no collection")

    def worker() -> None:
        try:
            runner.call(boom)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    main.pump()
    thread.join(timeout=2)

    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "no collection"


def test_times_out_when_the_main_thread_never_runs_the_job() -> None:
    runner = MainThreadRunner(lambda job: None, timeout=0.05)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            runner.call(lambda: 1)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)

    assert isinstance(errors[0], TimeoutError)


def test_calls_from_the_main_thread_run_inline() -> None:
    runner = MainThreadRunner(lambda job: pytest.fail("should not be posted"))
    assert runner.call(lambda: "inline") == "inline"
