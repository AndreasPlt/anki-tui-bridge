# anki-tui-bridge

An Anki add-on that serves Anki's own review machinery over a localhost HTTP API: the
scheduler's queue, the rendered card with its audio, deck counts, and answers **with the
answer time**. It exists so that clients like [`anki-tui`](https://github.com/AndreasPlt/anki-tui)
can study while Anki Desktop keeps running — without approximating anything.

Anki holds an exclusive lock on the collection, so external programs cannot open the
collection file while Anki runs. AnkiConnect covers a lot of that ground but not two things
this add-on provides:

| | `sidecar` (no add-on) | AnkiConnect | **anki-tui-bridge** |
|---|---|---|---|
| Works while Anki runs | no (collection locked) | yes | yes |
| Card order | Anki's scheduler | approximated from searches | **Anki's scheduler** |
| Answer time recorded | yes | no (~0 ms) | **yes** |
| Audio files resolved | from fields | approximated from fields | **from Anki's AV tags** |
| Undo / redo | yes | undo only (via the GUI) | **yes** |
| Bury / suspend card or note | yes | suspend only | **yes** |
| Rendered card, counts, media dir | yes | yes | yes |

Reviews made through AnkiConnect end up with ~0 ms of answer time, because its
`answerCards` starts the card timer immediately before answering; tools that require a
non-zero time (Anki Leaderboard, study-time statistics) ignore them. This add-on takes the
duration the client measured and hands it to Anki's scheduler.

## Requirements

- Anki 2.1.50+ (tested with Anki 25.9)
- Python 3.9+ (Anki bundles its own interpreter)
- No third-party packages: the add-on is pure standard library

## Install

From a checkout of this repository:

```bash
uv run python scripts/install.py
```

This copies `src/anki_tui_bridge/` into your `addons21/` folder
(macOS: `~/Library/Application Support/Anki2/addons21`, Linux:
`~/.local/share/Anki2/addons21`, Windows: `%APPDATA%\Anki2\addons21`). Restart Anki
afterwards.

Verify it is up:

```bash
curl http://127.0.0.1:8766/health
# {"ok": true, "name": "anki-tui-bridge", "version": "0.3.0"}
```

Uninstall with `uv run python scripts/install.py --uninstall`. If you had the earlier
queue-only add-on installed (`anki_queue_bridge`), remove it — it binds the same port:

```bash
uv run python scripts/install.py --clean     # deletes stale older installs
```

## Using it from anki-tui

```bash
anki-tui --backend anki-tui-bridge                  # 127.0.0.1:8766 by default
anki-tui --backend anki-tui-bridge --endpoint host:port
```

The review header shows `anki-tui-bridge` while this backend is active. `--dry-run` walks
the queue without scheduling anything.

## HTTP API

All responses are JSON. `POST /answer`, `/undo`, `/redo`, `/bury` and `/suspend` change
the collection; everything else only reads.

### `GET /health`

```json
{"ok": true, "name": "anki-tui-bridge", "version": "0.3.0"}
```

Clients should check `name` before trusting the endpoint, so an unrelated service on the
same port cannot take over.

### `GET /decks`

Every deck with the counts Anki would show today (parents include their subdecks):

```json
{"decks": [{"id": 1789107299286, "name": "Mandarin", "new_count": 20, "learn_count": 3, "review_count": 70}]}
```

### `GET /media-dir`

```json
{"media_dir": "/Users/you/Library/Application Support/Anki2/User 1/collection.media"}
```

### `POST /next`

```json
{"deck_id": 1789107299286, "fetch_limit": 1, "exclude": [1726331376174]}
```

Returns the card the scheduler would show next, plus its remaining counts. `exclude` skips
cards the client already has, which is what lets a preview/dry-run session walk the queue
without changing it. `fetch_limit` defaults to 1 and is capped at 32.

```json
{
  "card_id": 1726331376174,
  "queue": "review",
  "counts": {"new": 20, "learn": 0, "review": 83},
  "front_audio": [],
  "back_audio": ["ni3.mp3"]
}
```

`card_id` is `null` when Anki considers the deck finished (including learning cards that
are not due yet). The call is idempotent — asking does not consume the card.

### `GET /card/<card_id>`

The rendered card, everything a client needs to display it:

```json
{
  "card_id": 1726331376174,
  "question": "<style>…</style>…",
  "answer": "<style>…</style>…",
  "front_audio": [],
  "back_audio": ["ni3.mp3"],
  "intervals": ["<1m", "10m", "1d", "4d"],
  "max_taken_ms": 60000
}
```

`front_audio` / `back_audio` are the files behind the card's `[anki:play:q:<n>]` /
`[anki:play:a:<n>]` markers — modern Anki no longer leaves `[sound:file]` in the HTML, and
only the add-on can see the tag lists. `intervals` are Anki's own answer previews (with its
bidi formatting marks removed), and `max_taken_ms` is the deck preset's cap on recorded
answer time.

### `POST /answer`

```json
{"card_id": 1726331376174, "ease": 3, "elapsed_ms": 12345}
```

`ease` is 1–4 (Again/Hard/Good/Easy). `elapsed_ms` is how long the card was on screen, as
measured by the client; Anki logs exactly that as the answer time. Omit it and the review
is recorded as instant.

```json
{"answered": true, "elapsed_ms": 12345}
```

### `POST /undo` and `POST /redo`

No body. Undo/redo the last collection operation through Anki's own undo stack, then let
Anki's windows refresh.

```json
{"operation": "Review"}
```

`operation` is the label of the reverted (undo) or re-applied (redo) operation, or `null`
when there is nothing to undo/redo.

### `POST /bury` and `POST /suspend`

```json
{"card_id": 1726331376174, "note": false}
```

Buries or suspends the card, or — when `note` is `true` — every card of its note. Both are
undoable through Anki's undo stack.

```json
{"count": 1}
```

`count` is how many cards were affected.

Status codes: `400` invalid request, `401` bad/missing token, `404` unknown route,
`409` Anki state problem (no collection open, card gone), `500` unexpected failure.

## Configuration

Edit the add-on's options in Anki (*Tools → Add-ons → Anki TUI Bridge → Config*):

| Key | Default | Meaning |
|-----|---------|---------|
| `host` | `127.0.0.1` | Interface to bind. Anything but loopback is **refused** unless a `token` is set (or `allow_insecure_remote` is on). |
| `port` | `8766` | Listening port. |
| `token` | `null` | Optional shared secret; when set, requests must send it in the `X-Anki-Tui-Token` header. |
| `allow_insecure_remote` | `false` | Set `true` to serve a non-loopback `host` without a token. Only on a network you trust. |

Changes need an Anki restart.

## Security

This API reads your cards and changes your scheduling, so it is deliberately narrow and
bounded:

- **Loopback by default.** `127.0.0.1:8766` only. Configure another `host` and the add-on
  refuses to start (with a message in the console *and* an Anki dialog) unless a `token` is
  set or `allow_insecure_remote` explicitly accepts the risk.
- **No destructive endpoints.** You can read decks, cards, the queue and the media path, and
  change scheduling the way Anki's own reviewer does (answer, undo/redo, bury, suspend).
  There is no way to delete notes/decks, import, or sync — unlike AnkiConnect, which exposes
  those and ships without an API key.
- **No CORS layer**, on purpose: a page cannot read our responses and cannot make a JSON
  `POST` without a preflight we never approve. Set a `token` and web pages are shut out
  entirely, as are other local programs.
- **Bounded resources:** requests larger than 256 KB are refused with `413` before the body
  is read, at most 8 connections are served at once (further ones get `503`), and a stalled
  connection is dropped after 15 s so it cannot hold a worker slot.
- **No plaintext secrets in config** are needed unless you enable a token; the token travels
  as an HTTP header, so keep the bind on loopback (or behind something that encrypts) if you
  ever move it off this machine.

## Implementation notes

- `col.sched.get_queued_cards(fetch_limit=…, intraday_learning_only=False)` is the same
  backend call Anki's own reviewer uses; the scheduler computes its queue for the
  **currently selected deck**, so the add-on selects the requested deck and leaves it
  selected, the way clicking a deck in Anki does. It deliberately does not restore the
  previous deck: every switch is its own undoable operation, so one after a review would
  sit above it (the first undo would no longer reach the review) and switching back after an
  undo would clear Anki's redo stack (redo would never work).
- Answers rewind the card timer (`card.timer_started = now − elapsed_ms`) and then call
  `sched.answerCard`, so Anki's own code computes and logs the duration.
- Undo/redo call `col.undo()` / `col.redo()` (Anki's own `UndoEmpty` becomes
  `{"operation": null}`), then fire `gui_hooks.state_did_undo` and `mw.reset()` so Anki's
  windows follow. Bury/suspend use `sched.bury_cards`/`bury_notes` and
  `sched.suspend_cards`/`suspend_notes`, and are undoable from Anki afterwards.
- Anki's collection and GUI are not thread-safe, so every Anki call is marshalled onto
  Anki's main thread (`main_thread.py`); the HTTP server itself stays on stdlib threads.
- `bridge.py` (routing, validation, auth) and `engine.py` (Anki side) are separate so the
  HTTP layer is tested without Anki installed.

## Development

```bash
uv run pytest          # unit tests (no Anki required)
uv run ruff check .
uv run ruff format .
uv run python scripts/install.py --addons-dir PATH   # install into a test profile
```

## License

MIT
