"""Tests for the HTTP layer, using a fake provider (no Anki needed)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from anki_queue_bridge import bridge
from anki_queue_bridge.bridge import BridgeConfig, BridgeError, BridgeServer

CONFIG = BridgeConfig(port=0)

COUNTS = {"new": 3, "learn": 1, "review": 7}


class FakeQueue:
    def __init__(self, card_id: int | None = 42, error: Exception | None = None) -> None:
        self.card_id = card_id
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def next_card(self, deck_id: int, fetch_limit: int = 1) -> dict:
        self.calls.append((deck_id, fetch_limit))
        if self.error is not None:
            raise self.error
        return {"card_id": self.card_id, "queue": "review", "counts": COUNTS}


def call(
    method: str = "POST",
    path: str = "/next",
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    provider: bridge.QueueProvider | None = None,
    config: BridgeConfig = CONFIG,
) -> bridge.Response:
    body = b"" if payload is None else json.dumps(payload).encode()
    return bridge.handle_request(method, path, body, headers or {}, provider or FakeQueue(), config)


def test_health_reports_name_and_version() -> None:
    response = call(method="GET", path="/health")
    assert response.status == 200
    assert response.payload == {"ok": True, "name": bridge.NAME, "version": bridge.VERSION}


def test_next_returns_provider_result() -> None:
    provider = FakeQueue(card_id=7)
    response = call(payload={"deck_id": 123}, provider=provider)

    assert response.status == 200
    assert response.payload == {"card_id": 7, "queue": "review", "counts": COUNTS}
    assert provider.calls == [(123, 1)]


def test_next_forwards_fetch_limit_and_caps_it() -> None:
    provider = FakeQueue()
    call(payload={"deck_id": 1, "fetch_limit": 5}, provider=provider)
    call(payload={"deck_id": 1, "fetch_limit": 10_000}, provider=provider)
    call(payload={"deck_id": 1, "fetch_limit": 0}, provider=provider)

    assert provider.calls == [
        (1, 5),
        (1, bridge.MAX_FETCH_LIMIT),
        (1, 1),
    ]


def test_finished_deck_returns_null_card() -> None:
    response = call(payload={"deck_id": 1}, provider=FakeQueue(card_id=None))
    assert response.status == 200
    assert response.payload["card_id"] is None


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
def test_invalid_requests_are_rejected(payload: object) -> None:
    response = call(payload=payload)
    assert response.status == 400
    assert "error" in response.payload


def test_malformed_json_is_rejected() -> None:
    response = bridge.handle_request("POST", "/next", b"{not json", {}, FakeQueue(), CONFIG)
    assert response.status == 400


def test_unknown_route_returns_404() -> None:
    response = call(method="GET", path="/nope")
    assert response.status == 404


def test_bridge_error_is_reported_to_the_client() -> None:
    provider = FakeQueue(error=BridgeError("no collection is open"))
    response = call(payload={"deck_id": 1}, provider=provider)
    # 409: the request was fine, Anki's state was not.
    assert response.status == 409
    assert response.payload == {"error": "no collection is open"}


def test_unexpected_provider_failure_returns_500() -> None:
    response = call(payload={"deck_id": 1}, provider=FakeQueue(error=RuntimeError("boom")))
    assert response.status == 500
    assert response.payload["error"] == "RuntimeError: boom"


def test_token_is_enforced_when_configured() -> None:
    config = BridgeConfig(port=0, token="s3cret")

    def health(headers: dict[str, str]) -> int:
        return call(method="GET", path="/health", config=config, headers=headers).status

    assert health({}) == 401
    assert health({"x-anki-queue-token": "nope"}) == 401
    assert health({bridge.TOKEN_HEADER: "s3cret"}) == 200


def test_config_from_mapping_ignores_junk() -> None:
    assert BridgeConfig.from_mapping(None) == BridgeConfig()
    assert BridgeConfig.from_mapping({}) == BridgeConfig()
    assert BridgeConfig.from_mapping({"port": "not a port"}) == BridgeConfig()
    assert BridgeConfig.from_mapping({"port": 9000, "token": ""}) == BridgeConfig(port=9000)
    assert BridgeConfig.from_mapping({"host": "0.0.0.0", "token": "abc"}) == BridgeConfig(
        host="0.0.0.0", token="abc"
    )


def test_server_round_trip_over_http() -> None:
    provider = FakeQueue(card_id=99)
    server = BridgeServer(BridgeConfig(port=0), provider)
    server.start()
    try:
        endpoint = server.endpoint
        with urllib.request.urlopen(f"{endpoint}/health", timeout=5) as response:
            assert json.load(response)["ok"] is True

        request = urllib.request.Request(
            f"{endpoint}/next",
            data=json.dumps({"deck_id": 5}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        assert payload["card_id"] == 99
        assert provider.calls == [(5, 1)]

        # Errors must reach the client as JSON too, not as an HTML error page.
        request = urllib.request.Request(
            f"{endpoint}/next", data=b"{}", headers={"Content-Type": "application/json"}
        )
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request, timeout=5)
        assert failure.value.code == 400
        assert "error" in json.load(failure.value)
    finally:
        server.stop()
