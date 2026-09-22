"""Anki-facing side of the bridge: decks, cards, the queue, and answering.

Everything here runs inside Anki's process, so it can use the real scheduler
objects instead of re-deriving their state from searches.

Anki's collection and GUI are not thread-safe, so every call is marshalled onto
Anki's main thread by :class:`~anki_tui_bridge.main_thread.MainThreadRunner`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from .bridge import BridgeError
from .main_thread import MainThreadRunner

T = TypeVar("T")

# `QueuedCards.Queue` enum values from anki/scheduler.proto.
QUEUE_NAMES = {0: "new", 1: "learn", 2: "review"}

EASES = (1, 2, 3, 4)


def av_filenames(tags: Iterable[Any]) -> list[str]:
    """Sound files behind a side's AV tags, in render order, without repeats.

    Anki renders `[sound:file.mp3]` as a `[anki:play:<side>:<index>]` marker
    rather than leaving the filename in the HTML, so this is the only reliable
    way for a client to know what to play. Video tags carry no filename.
    """
    names: list[str] = []
    for tag in tags:
        filename = getattr(tag, "filename", None)
        if filename and str(filename) not in names:
            names.append(str(filename))
    return names


def _collection() -> Any:
    from aqt import mw

    col = getattr(mw, "col", None)
    if col is None:
        raise BridgeError("Anki has no open collection right now")
    return col


class AnkiEngine:
    """Implements the bridge's provider protocol against Anki."""

    def __init__(self, runner: MainThreadRunner | None = None) -> None:
        self._runner = runner

    def call(self, work: Callable[[], T]) -> T:
        """Run `work` on Anki's main thread (inline when no runner is set)."""
        if self._runner is None:
            return work()
        return self._runner.call(work)

    # ---------------------------------------------------------------- decks
    def decks(self) -> list[dict[str, Any]]:
        """Every deck with the counts Anki would show today."""

        def work() -> list[dict[str, Any]]:
            col = _collection()
            rows: list[dict[str, Any]] = []

            def walk(node: Any) -> None:
                deck_id = int(getattr(node, "deck_id", 0) or 0)
                if deck_id:
                    rows.append(
                        {
                            "id": deck_id,
                            "name": str(col.decks.name(deck_id)),
                            "new_count": int(getattr(node, "new_count", 0) or 0),
                            "learn_count": int(getattr(node, "learn_count", 0) or 0),
                            "review_count": int(getattr(node, "review_count", 0) or 0),
                        }
                    )
                for child in getattr(node, "children", []) or []:
                    walk(child)

            walk(col.sched.deck_due_tree())
            rows.sort(key=lambda row: [part.lower() for part in row["name"].split("::")])
            return rows

        return self.call(work)

    def media_dir(self) -> str:
        return self.call(lambda: str(_collection().media.dir()))

    # ----------------------------------------------------------------- queue
    def next_card(
        self,
        deck_id: int,
        fetch_limit: int = 1,
        exclude: Iterable[int] = (),
    ) -> dict[str, Any]:
        """The card Anki's scheduler would show next, plus its counts.

        `exclude` skips cards the client already has (used by preview/dry-run
        sessions, which never schedule anything, so the queue would otherwise
        keep offering the same front card).
        """
        skip = {int(card_id) for card_id in exclude}

        def work() -> dict[str, Any]:
            col = _collection()

            # The scheduler computes its queue for the *selected* deck, so we
            # select ours and restore the previous one around the query.
            # `get_queued_cards` itself is idempotent and consumes nothing.
            previous = col.decks.get_current_id()
            switched = previous != deck_id
            if switched:
                col.decks.set_current(deck_id)
            try:
                # Ask for enough cards to step over everything excluded.
                queued = col.sched.get_queued_cards(
                    fetch_limit=max(1, fetch_limit) + len(skip),
                    intraday_learning_only=False,
                )
            finally:
                if switched:
                    col.decks.set_current(previous)

            cards = [card for card in queued.cards if int(card.card.id) not in skip]
            front = cards[0] if cards else None

            result: dict[str, Any] = {
                "card_id": int(front.card.id) if front is not None else None,
                "queue": QUEUE_NAMES.get(int(front.queue)) if front is not None else None,
                "counts": {
                    "new": int(queued.new_count),
                    "learn": int(queued.learning_count),
                    "review": int(queued.review_count),
                },
                "front_audio": [],
                "back_audio": [],
            }

            if front is not None:
                card = col.get_card(front.card.id)
                result["front_audio"] = av_filenames(card.question_av_tags())
                result["back_audio"] = av_filenames(card.answer_av_tags())

            return result

        return self.call(work)

    # ------------------------------------------------------------------ card
    def card(self, card_id: int) -> dict[str, Any]:
        """Rendered content of one card, with everything a client needs."""

        def work() -> dict[str, Any]:
            col = _collection()
            try:
                card = col.get_card(int(card_id))
            except Exception as error:  # noqa: BLE001 - Anki raises its own type
                raise BridgeError(f"card {card_id} is gone: {error}") from error

            states = col._backend.get_scheduling_states(card.id)
            intervals = [str(value) for value in col._backend.describe_next_states(states)]

            return {
                "card_id": int(card.id),
                "question": card.question(),
                "answer": card.answer(),
                "front_audio": av_filenames(card.question_av_tags()),
                "back_audio": av_filenames(card.answer_av_tags()),
                "intervals": intervals,
                "max_taken_ms": int(card.time_limit()),
            }

        return self.call(work)

    # ---------------------------------------------------------------- answer
    def answer(self, card_id: int, ease: int, elapsed_ms: int | None = None) -> dict[str, Any]:
        """Schedule `card_id` with `ease`, recording the client's answer time.

        The client measures how long the card was on screen; Anki derives the
        logged duration from the card timer, so we rewind `timer_started` to
        `now - elapsed_ms` before answering. Without an explicit duration the
        answer is recorded as instant (0 ms).

        AnkiConnect cannot do this: its `answerCards` starts the timer
        immediately before answering, which is why reviews made through it show
        up with no answer time.
        """
        if ease not in EASES:
            raise BridgeError(f"ease must be one of {EASES}, got {ease!r}")
        taken = max(int(elapsed_ms or 0), 0)

        def work() -> dict[str, Any]:
            col = _collection()
            card = col.get_card(int(card_id))
            card.start_timer()
            card.timer_started = time.time() - taken / 1000.0
            col.sched.answerCard(card, ease)
            return {"answered": True, "elapsed_ms": taken}

        return self.call(work)

    # ------------------------------------------------------------- undo/redo
    def undo(self) -> dict[str, Any]:
        """Undo the last operation; `{"operation": None}` when there is none."""

        return self.call(lambda: self._undo_or_redo("undo"))

    def redo(self) -> dict[str, Any]:
        """Redo the last undone operation."""

        return self.call(lambda: self._undo_or_redo("redo"))

    def _undo_or_redo(self, kind: str) -> dict[str, Any]:
        col = _collection()
        try:
            out = getattr(col, kind)()
        except Exception as error:  # noqa: BLE001 - Anki raises its own UndoEmpty
            if type(error).__name__ == "UndoEmpty":
                return {"operation": None}
            raise
        self._refresh_gui(out, undo=kind == "undo")
        operation = str(getattr(out, "operation", "") or "")
        return {"operation": operation or None}

    def _refresh_gui(self, out: Any, *, undo: bool) -> None:
        """Let Anki's windows follow the change, as aqt's own undo does.

        Best effort: the undo has already happened, so a GUI hiccup must not
        turn it into an HTTP error.
        """
        try:  # pragma: no cover - only inside Anki
            from aqt import gui_hooks, mw

            if undo:
                gui_hooks.state_did_undo(out)
            mw.reset()
        except Exception:  # noqa: BLE001 - never fail a completed undo
            pass

    # -------------------------------------------------------- bury/suspend
    def bury(self, card_id: int, note: bool) -> dict[str, Any]:
        return self.call(lambda: self._bury_or_suspend(card_id, note, bury=True))

    def suspend(self, card_id: int, note: bool) -> dict[str, Any]:
        return self.call(lambda: self._bury_or_suspend(card_id, note, bury=False))

    def _bury_or_suspend(self, card_id: int, note: bool, *, bury: bool) -> dict[str, Any]:
        col = _collection()
        try:
            card = col.get_card(int(card_id))
        except Exception as error:  # noqa: BLE001 - Anki raises its own type
            raise BridgeError(f"card {card_id} is gone: {error}") from error
        sched = col.sched
        if bury:
            result = sched.bury_notes([card.nid]) if note else sched.bury_cards([card.id])
        else:
            result = sched.suspend_notes([card.nid]) if note else sched.suspend_cards([card.id])
        return {"count": int(getattr(result, "count", 0) or 0)}
