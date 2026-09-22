"""Tests for the HTTP layer, using a fake provider (no Anki needed)."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from anki_tui_bridge import bridge
from anki_tui_bridge.bridge import BridgeConfig, BridgeError, BridgeServer
from anki_tui_bridge.engine import av_filenames

CONFIG = BridgeConfig(port=0)

COUNTS = {"new": 3, "learn": 1, "review": 7}

DECKS = [
    {"id": 1, "name": "Default", "new_count": 0, "learn_count": 0, "review_count": 0},
    {"id": 2, "name": "Japanese", "new_count": 5, "learn_count": 1, "review_count": 9},
]

CARD = {
    "card_id": 42,
    "question": "<div>front [anki:play:q:0]</div>",
    "answer": "<div>back [anki:play:a:0]</div>",
    "front_audio": ["front.mp3"],
    "back_audio": ["back.mp3"],
    "intervals": ["<1m", "10m", "1d", "4d"],
    "max_taken_ms": 60000,
}


class FakeAnki:
    """Records what the routes ask for."""

    def __init__(self, card_id: int | None = 42, error: Exception | None = None) -> None:
        self.card_id = card_id
        self.error = error
        self.calls: list[tuple[str, tuple]] = []

    def _record(self, name: str, *args):
        self.calls.append((name, args))
        if self.error is not None:
            raise self.error

    def decks(self) -> list[dict]:
        self._record("decks")
        return DECKS

    def media_dir(self) -> str:
        self._record("media_dir")
        return "/tmp/collection.media"

    def next_card(
        self, deck_id: int, fetch_limit: int = 1, exclude: list[int] | None = None
    ) -> dict:
        self._record("next_card", deck_id, fetch_limit, tuple(exclude or ()))
        return {
            "card_id": self.card_id,
            "queue": "review",
            "counts": COUNTS,
            "front_audio": [],
            "back_audio": ["back.mp3"],
        }

    def card(self, card_id: int) -> dict:
        self._record("card", card_id)
        return CARD

    def answer(self, card_id: int, ease: int, elapsed_ms: int | None = None) -> dict:
        self._record("answer", card_id, ease, elapsed_ms)
        return {"answered": True, "elapsed_ms": elapsed_ms or 0}

    def undo(self) -> dict:
        self._record("undo")
        return {"operation": "Review"}

    def redo(self) -> dict:
        self._record("redo")
        return {"operation": "Bury"}

    def bury(self, card_id: int, note: bool) -> dict:
        self._record("bury", card_id, note)
        return {"count": 2 if note else 1}

    def suspend(self, card_id: int, note: bool) -> dict:
        self._record("suspend", card_id, note)
        return {"count": 1}


def call(
    method: str = "POST",
    path: str = "/next",
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    provider: bridge.Provider | None = None,
    config: BridgeConfig = CONFIG,
) -> bridge.Response:
    body = b"" if payload is None else json.dumps(payload).encode()
    return bridge.handle_request(method, path, body, headers or {}, provider or FakeAnki(), config)


def test_health_reports_name_and_version() -> None:
    response = call(method="GET", path="/health")
    assert response.status == 200
    assert response.payload == {"ok": True, "name": bridge.NAME, "version": bridge.VERSION}
    assert bridge.NAME == "anki-tui-bridge"


def test_decks_are_returned_as_is() -> None:
    provider = FakeAnki()
    response = call(method="GET", path="/decks", provider=provider)

    assert response.status == 200
    assert response.payload == {"decks": DECKS}
    assert provider.calls == [("decks", ())]


def test_media_dir_is_reported() -> None:
    response = call(method="GET", path="/media-dir")
    assert response.status == 200
    assert response.payload == {"media_dir": "/tmp/collection.media"}


def test_next_returns_provider_result() -> None:
    provider = FakeAnki(card_id=7)
    response = call(payload={"deck_id": 123}, provider=provider)

    assert response.status == 200
    assert response.payload["card_id"] == 7
    assert response.payload["counts"] == COUNTS
    assert provider.calls == [("next_card", (123, 1, ()))]


def test_next_forwards_fetch_limit_and_caps_it() -> None:
    provider = FakeAnki()
    call(payload={"deck_id": 1, "fetch_limit": 5}, provider=provider)
    call(payload={"deck_id": 1, "fetch_limit": 10_000}, provider=provider)
    call(payload={"deck_id": 1, "fetch_limit": 0}, provider=provider)

    assert provider.calls == [
        ("next_card", (1, 5, ())),
        ("next_card", (1, bridge.MAX_FETCH_LIMIT, ())),
        ("next_card", (1, 1, ())),
    ]


def test_next_passes_excluded_cards_for_preview_sessions() -> None:
    provider = FakeAnki()
    response = call(payload={"deck_id": 1, "exclude": [10, 11]}, provider=provider)

    assert response.status == 200
    assert provider.calls == [("next_card", (1, 1, (10, 11)))]


@pytest.mark.parametrize("exclude", ["10", [10, "11"], [True]])
def test_next_rejects_a_bad_exclude_list(exclude: object) -> None:
    response = call(payload={"deck_id": 1, "exclude": exclude})
    assert response.status == 400


def test_card_is_served_by_id() -> None:
    provider = FakeAnki()
    response = call(method="GET", path="/card/42", provider=provider)

    assert response.status == 200
    assert response.payload == CARD
    assert provider.calls == [("card", (42,))]


def test_card_requires_a_numeric_id() -> None:
    response = call(method="GET", path="/card/abc")
    assert response.status == 400


def test_answer_passes_ease_and_answer_time() -> None:
    provider = FakeAnki()
    response = call(
        path="/answer", payload={"card_id": 42, "ease": 3, "elapsed_ms": 12_345}, provider=provider
    )

    assert response.status == 200
    assert response.payload == {"answered": True, "elapsed_ms": 12_345}
    assert provider.calls == [("answer", (42, 3, 12_345))]


def test_answer_time_is_optional() -> None:
    provider = FakeAnki()
    response = call(path="/answer", payload={"card_id": 42, "ease": 1}, provider=provider)

    assert response.status == 200
    assert provider.calls == [("answer", (42, 1, None))]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"card_id": "42"},
        {"card_id": True, "ease": 3},
        {"card_id": 42},
        {"card_id": 42, "ease": 0},
        {"card_id": 42, "ease": 5},
        {"card_id": 42, "ease": 3, "elapsed_ms": -1},
        {"card_id": 42, "ease": 3, "elapsed_ms": "slow"},
    ],
)
def test_invalid_answer_requests_are_rejected(payload: object) -> None:
    response = call(path="/answer", payload=payload)
    assert response.status == 400
    assert "error" in response.payload


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"deck_id": "123"},
        {"deck_id": None},
        {"deck_id": True},
        {"deck_id": 1, "fetch_limit": "many"},
    ],
)
def test_invalid_next_requests_are_rejected(payload: object) -> None:
    response = call(payload=payload)
    assert response.status == 400


def test_malformed_json_is_rejected() -> None:
    response = bridge.handle_request("POST", "/next", b"{not json", {}, FakeAnki(), CONFIG)
    assert response.status == 400


def test_unknown_route_returns_404() -> None:
    assert call(method="GET", path="/nope").status == 404
    assert call(method="POST", path="/decks").status == 404


def test_undo_and_redo_report_the_operation() -> None:
    provider = FakeAnki()
    undone = call(path="/undo", payload={}, provider=provider)
    redone = call(path="/redo", payload={}, provider=provider)

    assert undone.status == 200
    assert undone.payload == {"operation": "Review"}
    assert redone.payload == {"operation": "Bury"}
    assert provider.calls == [("undo", ()), ("redo", ())]


def test_bury_and_suspend_pass_card_and_note_level() -> None:
    provider = FakeAnki()
    card = call(path="/bury", payload={"card_id": 42}, provider=provider)
    note = call(path="/bury", payload={"card_id": 42, "note": True}, provider=provider)
    suspended = call(path="/suspend", payload={"card_id": 42}, provider=provider)

    assert card.payload == {"count": 1}
    assert note.payload == {"count": 2}
    assert suspended.payload == {"count": 1}
    assert provider.calls == [
        ("bury", (42, False)),
        ("bury", (42, True)),
        ("suspend", (42, False)),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"card_id": "42"},
        {"card_id": True},
        {"card_id": 42, "note": "yes"},
    ],
)
@pytest.mark.parametrize("path", ["/bury", "/suspend"])
def test_invalid_bury_or_suspend_requests_are_rejected(path: str, payload: object) -> None:
    assert call(path=path, payload=payload).status == 400


def test_bridge_error_is_reported_to_the_client() -> None:
    provider = FakeAnki(error=BridgeError("no collection is open"))
    response = call(payload={"deck_id": 1}, provider=provider)
    # 409: the request was fine, Anki's state was not.
    assert response.status == 409
    assert response.payload == {"error": "no collection is open"}


def test_unexpected_provider_failure_returns_500() -> None:
    response = call(payload={"deck_id": 1}, provider=FakeAnki(error=RuntimeError("boom")))
    assert response.status == 500
    assert response.payload["error"] == "RuntimeError: boom"


def test_token_is_enforced_when_configured() -> None:
    config = BridgeConfig(port=0, token="s3cret")

    def health(headers: dict[str, str]) -> int:
        return call(method="GET", path="/health", config=config, headers=headers).status

    assert health({}) == 401
    assert health({"x-anki-tui-token": "nope"}) == 401
    assert health({bridge.TOKEN_HEADER: "s3cret"}) == 200


def test_config_refuses_to_expose_an_unauthenticated_api() -> None:
    # Loopback is the default and is fine.
    assert BridgeConfig().is_loopback
    assert BridgeConfig().security_problem() is None
    assert BridgeConfig(host=" localhost ").security_problem() is None
    assert BridgeConfig(host="::1").security_problem() is None

    # Anything else needs a token.
    exposed = BridgeConfig(host="0.0.0.0")
    assert not exposed.is_loopback
    problem = exposed.security_problem()
    assert problem is not None
    assert "reachable from the network" in problem

    assert BridgeConfig(host="0.0.0.0", token="s3cret").security_problem() is None
    assert BridgeConfig(host="192.168.1.5", token="s3cret").security_problem() is None
    assert BridgeConfig(host="0.0.0.0", allow_insecure_remote=True).security_problem() is None


def test_config_from_mapping_reads_the_remote_optin() -> None:
    assert BridgeConfig.from_mapping({"allow_insecure_remote": True}).allow_insecure_remote
    # Only a real boolean counts.
    assert not BridgeConfig.from_mapping({"allow_insecure_remote": "yes"}).allow_insecure_remote


def test_config_from_mapping_ignores_junk() -> None:
    assert BridgeConfig.from_mapping(None) == BridgeConfig()
    assert BridgeConfig.from_mapping({}) == BridgeConfig()
    assert BridgeConfig.from_mapping({"port": "not a port"}) == BridgeConfig()
    assert BridgeConfig.from_mapping({"port": 9000, "token": ""}) == BridgeConfig(port=9000)
    assert BridgeConfig.from_mapping({"host": "0.0.0.0", "token": "abc"}) == BridgeConfig(
        host="0.0.0.0", token="abc"
    )


class SoundTag:
    def __init__(self, filename: str) -> None:
        self.filename = filename


class VideoTag:
    """Anki's video tags carry no `filename`, and cannot be played as audio."""


def test_av_filenames_keeps_order_and_drops_videos() -> None:
    tags = [SoundTag("a.mp3"), VideoTag(), SoundTag("b.mp3"), SoundTag("a.mp3")]
    assert av_filenames(tags) == ["a.mp3", "b.mp3"]
    assert av_filenames([]) == []


def test_addon_refuses_to_serve_an_unauthenticated_network_endpoint(monkeypatch) -> None:
    """The add-on must fail closed when configured to expose itself."""
    import sys
    import types

    import anki_tui_bridge as addon

    aqt = types.ModuleType("aqt")
    aqt.mw = types.SimpleNamespace(
        addonManager=types.SimpleNamespace(getConfig=lambda name: {"host": "0.0.0.0", "port": 0}),
        # Enough of Anki's taskman for the main-thread runner.
        taskman=types.SimpleNamespace(run_on_main=lambda job: job()),
    )
    monkeypatch.setitem(sys.modules, "aqt", aqt)
    monkeypatch.setattr(addon, "_server", None)
    warned: list[str] = []
    monkeypatch.setattr(addon, "_warn_in_anki", warned.append)

    assert addon.start_server() is None, "must not bind off loopback without a token"
    assert warned and "refusing to listen" in warned[0]

    # With a token it is allowed through (binding on port 0 then fails or works,
    # either way it is no longer the security check that stops us).
    aqt.mw.addonManager.getConfig = lambda name: {
        "host": "127.0.0.1",
        "port": 0,
        "token": "s3cret",
    }
    server = addon.start_server()
    assert server is not None
    server.stop()
    monkeypatch.setattr(addon, "_server", None)


def test_oversized_bodies_are_refused_before_reading() -> None:
    server = BridgeServer(BridgeConfig(port=0), FakeAnki(), max_body_bytes=1024)
    server.start()
    try:
        request = urllib.request.Request(
            f"{server.endpoint}/next",
            data=b"x" * 4096,
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request, timeout=5)
        assert failure.value.code == 413
        assert "larger than 1024 bytes" in json.load(failure.value)["error"]
    finally:
        server.stop()


def test_invalid_content_length_is_rejected() -> None:
    server = BridgeServer(BridgeConfig(port=0), FakeAnki())
    server.start()
    try:
        host, port = server.endpoint.removeprefix("http://").split(":")
        with socket.create_connection((host, int(port)), timeout=5) as connection:
            connection.sendall(b"POST /next HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n")
            reply = connection.recv(4096)
        assert b" 400 " in reply.split(b"\r\n")[0]
    finally:
        server.stop()


def test_connections_beyond_the_worker_limit_get_503() -> None:
    """One stalled client must not be able to block the endpoint forever."""
    server = BridgeServer(BridgeConfig(port=0), FakeAnki(), max_workers=1, request_timeout=2.0)
    server.start()
    try:
        host, port = server.endpoint.removeprefix("http://").split(":")
        stalled = socket.create_connection((host, int(port)), timeout=5)
        # Announce a body and never send it: the worker is now busy.
        stalled.sendall(b"POST /next HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n\r\n")
        time.sleep(0.2)

        with socket.create_connection((host, int(port)), timeout=5) as second:
            second.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            reply = second.recv(4096)
        assert b" 503 " in reply.split(b"\r\n")[0], reply

        # Once the stalled request times out the slot is free again.
        time.sleep(2.2)
        with urllib.request.urlopen(f"{server.endpoint}/health", timeout=5) as response:
            assert json.load(response)["ok"] is True
        stalled.close()
    finally:
        server.stop()


def test_server_round_trip_over_http() -> None:
    provider = FakeAnki(card_id=99)
    server = BridgeServer(BridgeConfig(port=0), provider)
    server.start()
    try:
        endpoint = server.endpoint
        with urllib.request.urlopen(f"{endpoint}/health", timeout=5) as response:
            assert json.load(response)["name"] == bridge.NAME

        request = urllib.request.Request(
            f"{endpoint}/next",
            data=json.dumps({"deck_id": 5}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.load(response)["card_id"] == 99

        answer = urllib.request.Request(
            f"{endpoint}/answer",
            data=json.dumps({"card_id": 99, "ease": 4, "elapsed_ms": 2500}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(answer, timeout=5) as response:
            assert json.load(response)["elapsed_ms"] == 2500

        with urllib.request.urlopen(f"{endpoint}/card/99", timeout=5) as response:
            assert json.load(response)["card_id"] == 42  # the fake's canned card

        assert [name for name, _ in provider.calls] == ["next_card", "answer", "card"]

        # Errors must reach the client as JSON too, not as an HTML error page.
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(f"{endpoint}/card/nope", timeout=5)
        assert failure.value.code == 400
        assert "error" in json.load(failure.value)
    finally:
        server.stop()
