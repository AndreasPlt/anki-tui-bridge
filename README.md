# anki-queue-bridge

An Anki add-on that exposes **Anki's real review queue** over a localhost HTTP
endpoint: the card the scheduler would show next for a deck, plus the remaining
counts. Any local program can use it — scripts, editor plugins, status bars,
reviewer front-ends.

It exists because Anki holds an exclusive lock on the collection while it runs,
so external tools cannot open the collection file, and AnkiConnect exposes
searches and the GUI bridge but *not* the scheduler's queue.

The reference client is [`anki-tui`](https://github.com/AndreasPlt/anki-tui):
with the add-on installed it studies in Anki's exact card order while still
using AnkiConnect for everything else (card HTML, media, answering).

## What the queue decides

Rebuilding "what is due" from search results is not the same thing:

- daily new/review limits and the order they are consumed in
- learning cards that came due again after a step
- sibling burying
- new-card gather order (sequential or random) and review order (due date or random)
- interleaving of learning cards between reviews

This add-on answers that question from Anki itself, so none of it has to be
re-implemented (or approximated) by a client.

## Requirements

- Anki 2.1.50+ (tested with Anki 25.9)
- Python 3.9+ (Anki bundles its own interpreter)
- No third-party packages: the add-on is pure standard library

## Install

From a checkout of this repository:

```bash
uv run python scripts/install.py
```

This copies `src/anki_queue_bridge/` into your `addons21/` folder
(macOS: `~/Library/Application Support/Anki2/addons21`, Linux:
`~/.local/share/Anki2/addons21`, Windows: `%APPDATA%\Anki2\addons21`).
Restart Anki afterwards.

Verify it is up:

```bash
curl http://127.0.0.1:8766/health
# {"ok": true, "name": "anki-queue-bridge", "version": "0.1.0"}
```

Uninstall with:

```bash
uv run python scripts/install.py --uninstall
```

## Using it from anki-tui

anki-tui probes this endpoint automatically when it runs in AnkiConnect mode:

```bash
anki-tui --anki-connect
```

The review header then shows a dim `anki-queue` label instead of
`anki-connect`. Point it elsewhere with `--anki-queue-bridge host:port`, override
with `ANKI_QUEUE_BRIDGE`, supply the token via `ANKI_QUEUE_BRIDGE_TOKEN`, or
disable the probe with `--no-anki-queue-bridge`.

## HTTP API

All responses are JSON. Everything is read-only: no endpoint answers cards or
changes scheduling.

### `GET /health`

```json
{"ok": true, "name": "anki-queue-bridge", "version": "0.1.0"}
```

Clients should check `name` before trusting the endpoint, so an unrelated
service on the same port cannot take over.

### `POST /next`

Request:

```json
{"deck_id": 1789107299286, "fetch_limit": 1}
```

`fetch_limit` is optional (default `1`, capped at `32`).

Response:

```json
{"card_id": 1726331376174, "queue": "review", "counts": {"new": 20, "learn": 0, "review": 83}}
```

`card_id` is `null` when Anki considers the deck finished (including learning
cards that are not due yet). `counts` are the scheduler's remaining counts for
that deck. The call is idempotent — it does not consume cards, so a client can
ask again freely; answering happens elsewhere (e.g. AnkiConnect's `answerCards`).

Status codes: `400` invalid request, `401` bad/missing token, `404` unknown
route, `409` Anki has no open collection, `500` unexpected failure.

## Configuration

Edit the add-on's options in Anki (*Tools → Add-ons → Anki Queue Bridge → Config*):

| Key | Default | Meaning |
|-----|---------|---------|
| `host` | `127.0.0.1` | Interface to bind. Keep it on loopback unless you really want remote access. |
| `port` | `8766` | Listening port. |
| `token` | `null` | Optional shared secret; when set, requests must send it in the `X-Anki-Queue-Token` header. |

Changes need an Anki restart.

## Implementation notes

- `col.sched.get_queued_cards(fetch_limit=..., intraday_learning_only=False)`
  is the same backend call Anki's own reviewer uses.
- The scheduler computes its queue for the **currently selected deck**, so the
  add-on selects the requested deck for the duration of the query and restores
  the previous selection immediately afterwards. That touches Anki's `curDeck`
  config value for a moment; nothing else changes.
- `anki_queue_bridge/bridge.py` contains no Anki imports, so routing,
  validation and auth are covered by tests that run without Anki installed.
- The server runs on one daemon thread, logs nothing per request, and is started
  when the profile opens.

## Development

```bash
uv run pytest          # unit tests (no Anki required)
uv run ruff check .
uv run ruff format .
uv run python scripts/install.py --addons-dir PATH   # install into a test profile
```

## License

MIT
