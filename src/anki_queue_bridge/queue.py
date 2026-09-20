"""Anki-side provider: asks the scheduler for the next queued card.

Everything that touches Anki is imported lazily so this module can be imported
(packaged, type-checked) without an Anki installation.
"""

from __future__ import annotations

from typing import Any

from .bridge import BridgeError

# `QueuedCards.Queue` enum values from anki/scheduler.proto.
QUEUE_NAMES = {0: "new", 1: "learn", 2: "review"}


class AnkiQueue:
    """Reads Anki's own queue for a deck."""

    def next_card(self, deck_id: int, fetch_limit: int = 1) -> dict[str, Any]:
        from aqt import mw

        col = getattr(mw, "col", None)
        if col is None:
            raise BridgeError("Anki has no open collection right now")

        # The scheduler computes its queue for the *currently selected* deck, so
        # we select ours (and restore the previous one) around the query.
        # `get_queued_cards` itself is idempotent and does not consume cards.
        previous = col.decks.get_current_id()
        switched = previous != deck_id
        if switched:
            col.decks.set_current(deck_id)
        try:
            queued = col.sched.get_queued_cards(
                fetch_limit=fetch_limit, intraday_learning_only=False
            )
        finally:
            if switched:
                col.decks.set_current(previous)

        cards = list(queued.cards)
        front = cards[0] if cards else None
        return {
            "card_id": int(front.card.id) if front is not None else None,
            "queue": QUEUE_NAMES.get(int(front.queue)) if front is not None else None,
            "counts": {
                "new": int(queued.new_count),
                "learn": int(queued.learning_count),
                "review": int(queued.review_count),
            },
        }
