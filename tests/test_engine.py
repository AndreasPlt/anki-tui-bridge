"""Engine tests for the queue lookup's deck selection.

No Anki needed: the collection is faked down to the handful of methods
`AnkiEngine.next_card` touches.

The selection behaviour is a regression guard: switching the deck around the
query (select + restore) is what broke undo and redo in Anki, because every
switch is its own undoable operation. The lookup must therefore select the
requested deck at most once and never touch the selection again.
"""

from __future__ import annotations

from types import SimpleNamespace

from anki_tui_bridge import engine


class FakeDecks:
    def __init__(self, current: int) -> None:
        self.current = current
        self.selections: list[int] = []

    def get_current_id(self) -> int:
        return self.current

    def set_current(self, deck_id: int) -> None:
        self.selections.append(deck_id)
        self.current = deck_id


class FakeSched:
    def __init__(self, card_ids: list[int]) -> None:
        self.card_ids = card_ids
        self.fetches: list[int] = []

    def get_queued_cards(self, *, fetch_limit: int, intraday_learning_only: bool) -> object:
        self.fetches.append(fetch_limit)
        cards = [
            SimpleNamespace(card=SimpleNamespace(id=card_id), queue=2)
            for card_id in self.card_ids[:fetch_limit]
        ]
        return SimpleNamespace(
            cards=cards,
            new_count=0,
            learning_count=0,
            review_count=len(self.card_ids),
        )


class FakeCard:
    def question_av_tags(self) -> list:
        return []

    def answer_av_tags(self) -> list:
        return []


class FakeCollection:
    def __init__(self, current: int, card_ids: list[int]) -> None:
        self.decks = FakeDecks(current)
        self.sched = FakeSched(card_ids)

    def get_card(self, card_id: int) -> FakeCard:
        return FakeCard()


def engine_with(monkeypatch, col: FakeCollection) -> engine.AnkiEngine:
    monkeypatch.setattr(engine, "_collection", lambda: col)
    return engine.AnkiEngine()


def test_next_card_selects_the_requested_deck_and_leaves_it(monkeypatch) -> None:
    col = FakeCollection(current=1, card_ids=[42])
    anki = engine_with(monkeypatch, col)

    result = anki.next_card(2)

    assert result["card_id"] == 42
    assert result["queue"] == "review"
    assert col.decks.selections == [2]
    assert col.decks.current == 2


def test_repeated_lookups_add_no_further_undo_entries(monkeypatch) -> None:
    """Only the first lookup may touch the selection.

    Any later `set_current` would be a new undoable operation, and Anki clears
    its redo stack on those — the client fetches the next card right after an
    undo, so redo would stop working.
    """
    col = FakeCollection(current=1, card_ids=[10, 11])
    anki = engine_with(monkeypatch, col)

    anki.next_card(2)
    anki.next_card(2)
    anki.next_card(2)

    assert col.decks.selections == [2], "the deck is selected once, never restored"


def test_next_card_does_not_touch_the_selection_when_already_there(monkeypatch) -> None:
    col = FakeCollection(current=2, card_ids=[42])
    anki = engine_with(monkeypatch, col)

    anki.next_card(2)

    assert col.decks.selections == []


def test_next_card_avoids_offering_excluded_cards(monkeypatch) -> None:
    col = FakeCollection(current=2, card_ids=[10, 11, 12])
    anki = engine_with(monkeypatch, col)

    result = anki.next_card(2, exclude=[10, 11])

    assert result["card_id"] == 12
    # The fetch asks for the excluded cards plus the one that is wanted.
    assert col.sched.fetches == [3]
    assert col.decks.selections == []
