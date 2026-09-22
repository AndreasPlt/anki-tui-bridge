"""Localhost HTTP layer of the bridge.

No Anki imports live here on purpose: request handling can be exercised with a
fake provider, so routing, validation and auth are covered by tests that run
without an Anki installation.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

VERSION = "0.3.0"
NAME = "anki-tui-bridge"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
TOKEN_HEADER = "X-Anki-Tui-Token"

#: Cap on `fetch_limit`; reviewers only ever need the front card.
MAX_FETCH_LIMIT = 32

#: Hosts that are not reachable from the network.
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

#: Our biggest request is a `/next` with a long `exclude` list (~10 KB for 512
#: ids), so this is generous while still bounding what a caller can allocate.
MAX_BODY_BYTES = 256 * 1024

#: Concurrent connections served at once; beyond this we answer 503.
MAX_WORKERS = 8

#: Seconds a connection may take to send a complete request.
REQUEST_TIMEOUT = 15.0


class Provider(Protocol):
    """What the HTTP layer needs from Anki."""

    def decks(self) -> list[dict[str, Any]]:
        """Every deck with today's counts."""

    def media_dir(self) -> str:
        """Directory Anki keeps media in."""

    def next_card(
        self, deck_id: int, fetch_limit: int = 1, exclude: list[int] | None = None
    ) -> dict[str, Any]:
        """The card the scheduler would show next, with counts and AV tags."""

    def card(self, card_id: int) -> dict[str, Any]:
        """Rendered question/answer HTML, AV tags, answer previews."""

    def answer(self, card_id: int, ease: int, elapsed_ms: int | None = None) -> dict[str, Any]:
        """Schedule a card, recording how long it was on screen."""

    def undo(self) -> dict[str, Any]:
        """Undo the last collection operation.

        Returns `{"operation": <label or None>}`; `None` means there was
        nothing to undo.
        """

    def redo(self) -> dict[str, Any]:
        """Redo the last undone operation, same reply as :meth:`undo`."""

    def bury(self, card_id: int, note: bool) -> dict[str, Any]:
        """Bury the card, or every card of its note. Returns `{"count": n}`."""

    def suspend(self, card_id: int, note: bool) -> dict[str, Any]:
        """Suspend the card, or every card of its note. Returns `{"count": n}`."""


class BridgeError(RuntimeError):
    """Raised by a provider for expected, reportable failures."""


@dataclass(frozen=True)
class BridgeConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str | None = None
    #: Opt out of the off-loopback refusal below, for people who really do want
    #: an unauthenticated API on their network.
    allow_insecure_remote: bool = False

    @classmethod
    def from_mapping(cls, data: Any) -> BridgeConfig:
        """Build a config from Anki's add-on config (which may be anything)."""
        data = data if isinstance(data, dict) else {}
        host = str(data.get("host") or DEFAULT_HOST)
        configured_port = data.get("port")
        try:
            port = DEFAULT_PORT if configured_port is None else int(configured_port)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        token = data.get("token")
        token = str(token) if token else None
        return cls(
            host=host,
            port=port,
            token=token,
            allow_insecure_remote=data.get("allow_insecure_remote") is True,
        )

    @property
    def is_loopback(self) -> bool:
        """Whether the configured host is only reachable from this machine."""
        return self.host.strip().lower() in LOOPBACK_HOSTS

    def security_problem(self) -> str | None:
        """Why this endpoint must not be served as configured, if at all.

        This API reads your cards and changes your scheduling, and it has no
        CORS layer, so binding it anywhere but loopback means anyone who can
        reach the machine can use it. Serve it only when a token protects it.
        """
        if self.is_loopback or self.token is not None or self.allow_insecure_remote:
            return None
        return (
            f"host {self.host!r} is reachable from the network and no token is set; "
            'set "token" in the add-on config, or "allow_insecure_remote": true to '
            "accept that anyone on the network can read your cards and answer them"
        )


@dataclass
class Response:
    status: int
    payload: dict[str, Any]


def http_error_response(status: int, message: str) -> bytes:
    """A complete HTTP response, for cases handled outside the router."""
    body = json.dumps({"error": message}).encode("utf-8")
    head = (
        f"HTTP/1.1 {status} Error\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("utf-8") + body


def handle_request(
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    provider: Provider,
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

    if method == "GET" and route == "/decks":
        return _guard(lambda: {"decks": provider.decks()})

    if method == "GET" and route == "/media-dir":
        return _guard(lambda: {"media_dir": provider.media_dir()})

    if method == "POST" and route == "/next":
        return _next_card(body, provider)

    if method == "GET" and route.startswith("/card/"):
        return _card(route, provider)

    if method == "POST" and route == "/answer":
        return _answer(body, provider)

    if method == "POST" and route == "/undo":
        return _guard(provider.undo)

    if method == "POST" and route == "/redo":
        return _guard(provider.redo)

    if method == "POST" and route == "/bury":
        return _bury_or_suspend(body, provider, bury=True)

    if method == "POST" and route == "/suspend":
        return _bury_or_suspend(body, provider, bury=False)

    return Response(404, {"error": f"no route for {method} {route}"})


def _guard(work) -> Response:
    try:
        payload = work()
    except BridgeError as error:
        return Response(409, {"error": str(error)})
    except Exception as error:  # noqa: BLE001 - surfaced to the client as-is
        return Response(500, {"error": f"{type(error).__name__}: {error}"})
    if not isinstance(payload, dict):
        return Response(500, {"error": "provider returned a non-object"})
    return Response(200, payload)


def _json_body(body: bytes) -> dict[str, Any] | Response:
    try:
        request = json.loads(body or b"{}")
    except json.JSONDecodeError as error:
        return Response(400, {"error": f"invalid JSON body: {error}"})
    if not isinstance(request, dict):
        return Response(400, {"error": "body must be a JSON object"})
    return request


#: Sentinel telling `_integer` that a field has no default and must be present.
_REQUIRED = object()


def _integer(request: dict[str, Any], key: str, default: Any = _REQUIRED) -> Any:
    if key not in request:
        if default is _REQUIRED:
            return Response(400, {"error": f"{key} is required and must be an integer"})
        return default
    value = request[key]
    if not isinstance(value, int) or isinstance(value, bool):
        return Response(400, {"error": f"{key} must be an integer"})
    return value


def _next_card(body: bytes, provider: Provider) -> Response:
    request = _json_body(body)
    if isinstance(request, Response):
        return request

    deck_id = _integer(request, "deck_id")
    if isinstance(deck_id, Response):
        return deck_id

    fetch_limit = _integer(request, "fetch_limit", 1)
    if isinstance(fetch_limit, Response):
        return fetch_limit
    fetch_limit = max(1, min(fetch_limit, MAX_FETCH_LIMIT))

    exclude = request.get("exclude", [])
    if not isinstance(exclude, list) or any(
        not isinstance(card_id, int) or isinstance(card_id, bool) for card_id in exclude
    ):
        return Response(400, {"error": "exclude must be a list of card ids"})

    return _guard(lambda: provider.next_card(deck_id, fetch_limit, exclude))


def _card(route: str, provider: Provider) -> Response:
    raw_id = route[len("/card/") :]
    if not raw_id.isdigit():
        return Response(400, {"error": "card id must be an integer"})
    return _guard(lambda: provider.card(int(raw_id)))


def _answer(body: bytes, provider: Provider) -> Response:
    request = _json_body(body)
    if isinstance(request, Response):
        return request

    card_id = _integer(request, "card_id")
    if isinstance(card_id, Response):
        return card_id

    ease = _integer(request, "ease")
    if isinstance(ease, Response):
        return ease
    if ease not in (1, 2, 3, 4):
        return Response(400, {"error": "ease must be 1, 2, 3 or 4"})

    elapsed = _integer(request, "elapsed_ms", None)
    if isinstance(elapsed, Response):
        return elapsed
    if elapsed is not None and elapsed < 0:
        return Response(400, {"error": "elapsed_ms must not be negative"})

    return _guard(lambda: provider.answer(card_id, ease, elapsed))


def _bury_or_suspend(body: bytes, provider: Provider, *, bury: bool) -> Response:
    request = _json_body(body)
    if isinstance(request, Response):
        return request

    card_id = _integer(request, "card_id")
    if isinstance(card_id, Response):
        return card_id

    note = request.get("note", False)
    if not isinstance(note, bool):
        return Response(400, {"error": "note must be a boolean"})

    work = provider.bury if bury else provider.suspend
    return _guard(lambda: work(card_id, note))


class BridgeServer:
    """Threaded HTTP server bound to localhost.

    Concurrency, request size and request duration are all bounded so a local
    process cannot exhaust Anki's memory or threads through this endpoint.
    """

    def __init__(
        self,
        config: BridgeConfig,
        provider: Provider,
        max_workers: int = MAX_WORKERS,
        request_timeout: float = REQUEST_TIMEOUT,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self.config = config
        self.provider = provider
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        self.max_body_bytes = max_body_bytes
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        if self._server is not None:
            host, port = self._server.server_address[:2]
            return f"http://{host}:{port}"
        return f"http://{self.config.host}:{self.config.port}"

    def start(self) -> None:
        handler = _make_handler(
            self.provider,
            self.config,
            request_timeout=self.request_timeout,
            max_body_bytes=self.max_body_bytes,
        )
        # Binding can fail (port in use); the caller decides what to do.
        server = _Server(
            (self.config.host, self.config.port), handler, max_workers=self.max_workers
        )
        thread = threading.Thread(target=server.serve_forever, name=NAME, daemon=True)
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

    def __init__(self, address, handler, max_workers: int = MAX_WORKERS) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max(1, max_workers))

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            # Answer instead of dropping the connection, so callers see why.
            try:
                request.sendall(http_error_response(503, "too many concurrent bridge requests"))
            except OSError:
                pass
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _make_handler(
    provider: Provider,
    config: BridgeConfig,
    request_timeout: float = REQUEST_TIMEOUT,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> type[BaseHTTPRequestHandler]:
    # Class bodies do not close over same-named locals, hence the alias.
    stalled_connection_timeout = request_timeout

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"{NAME}/{VERSION}"
        # A stalled connection must not hold a worker slot forever.
        timeout = stalled_connection_timeout

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._respond("GET", b"")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or 0)
            except ValueError:
                self._send(400, {"error": "invalid Content-Length"})
                return
            if length > max_body_bytes:
                # Refuse before reading, so a huge body cannot be allocated.
                self.close_connection = True
                self._send(
                    413,
                    {"error": f"body larger than {max_body_bytes} bytes"},
                )
                return
            body = self.rfile.read(length) if length > 0 else b""
            self._respond("POST", body)

        def _respond(self, method: str, body: bytes) -> None:
            headers = {key.lower(): value for key, value in self.headers.items()}
            response = handle_request(method, self.path, body, headers, provider, config)
            self._send(response.status, response.payload)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Anki's console should stay quiet; start-up and errors are printed
            # by the add-on itself.
            pass

    return Handler
