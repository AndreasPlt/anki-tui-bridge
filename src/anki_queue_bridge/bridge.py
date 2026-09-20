"""Localhost HTTP layer of the bridge.

No Anki imports live here on purpose: the request handling can be exercised
with a fake provider, so the tricky parts (routing, validation, auth) are
covered by tests that do not need an Anki installation.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

VERSION = "0.1.0"
NAME = "anki-queue-bridge"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
TOKEN_HEADER = "X-Anki-Queue-Token"

#: Cap on `fetch_limit`; the reviewers only ever need the front card.
MAX_FETCH_LIMIT = 32


class QueueProvider(Protocol):
    """Answers "which card does Anki consider next?"."""

    def next_card(self, deck_id: int, fetch_limit: int = 1) -> dict[str, Any]:
        """Return ``{"card_id", "queue", "counts"}`` for the deck's queue."""
        ...


class BridgeError(RuntimeError):
    """Raised by a provider for expected, reportable failures."""


@dataclass(frozen=True)
class BridgeConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str | None = None

    @classmethod
    def from_mapping(cls, data: Any) -> BridgeConfig:
        """Build a config from Anki's add-on config (which may be anything)."""
        data = data if isinstance(data, dict) else {}
        host = str(data.get("host") or DEFAULT_HOST)
        try:
            port = int(data.get("port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        token = data.get("token")
        token = str(token) if token else None
        return cls(host=host, port=port, token=token)


@dataclass
class Response:
    status: int
    payload: dict[str, Any]


def handle_request(
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    provider: QueueProvider,
    config: BridgeConfig,
) -> Response:
    """Route one request. Pure apart from calling the provider."""
    if config.token is not None:
        supplied = headers.get(TOKEN_HEADER.lower()) or headers.get(TOKEN_HEADER)
        if supplied != config.token:
            return Response(401, {"error": "invalid or missing bridge token"})

    route = path.split("?", 1)[0].rstrip("/") or "/"

    if method == "GET" and route == "/health":
        return Response(200, {"ok": True, "name": NAME, "version": VERSION})

    if method == "POST" and route == "/next":
        return _next_card(body, provider)

    return Response(404, {"error": f"no route for {method} {route}"})


def _next_card(body: bytes, provider: QueueProvider) -> Response:
    try:
        request = json.loads(body or b"{}")
    except json.JSONDecodeError as error:
        return Response(400, {"error": f"invalid JSON body: {error}"})

    if not isinstance(request, dict):
        return Response(400, {"error": "body must be a JSON object"})

    deck_id = request.get("deck_id")
    if not isinstance(deck_id, int) or isinstance(deck_id, bool):
        return Response(400, {"error": "deck_id must be an integer"})

    fetch_limit = request.get("fetch_limit", 1)
    if not isinstance(fetch_limit, int) or isinstance(fetch_limit, bool):
        return Response(400, {"error": "fetch_limit must be an integer"})
    fetch_limit = max(1, min(fetch_limit, MAX_FETCH_LIMIT))

    try:
        result = provider.next_card(deck_id, fetch_limit)
    except BridgeError as error:
        return Response(409, {"error": str(error)})
    except Exception as error:  # noqa: BLE001 - surfaced to the client as-is
        return Response(500, {"error": f"{type(error).__name__}: {error}"})

    if not isinstance(result, dict):
        return Response(500, {"error": "provider returned a non-object"})
    return Response(200, result)


class BridgeServer:
    """Threaded HTTP server bound to localhost."""

    def __init__(self, config: BridgeConfig, provider: QueueProvider) -> None:
        self.config = config
        self.provider = provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        if self._server is not None:
            host, port = self._server.server_address[:2]
            return f"http://{host}:{port}"
        return f"http://{self.config.host}:{self.config.port}"

    def start(self) -> None:
        handler = _make_handler(self.provider, self.config)
        # Binding can fail (port in use); the caller decides what to do.
        server = _Server((self.config.host, self.config.port), handler)
        thread = threading.Thread(
            target=server.serve_forever, name="anki-queue-bridge", daemon=True
        )
        thread.start()
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(provider: QueueProvider, config: BridgeConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"{NAME}/{VERSION}"

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._respond("GET", b"")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            self._respond("POST", body)

        def _respond(self, method: str, body: bytes) -> None:
            headers = {key.lower(): value for key, value in self.headers.items()}
            response = handle_request(method, self.path, body, headers, provider, config)
            payload = json.dumps(response.payload).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            # Anki's console should stay quiet; start-up and errors are printed
            # by the add-on itself.
            pass

    return Handler
