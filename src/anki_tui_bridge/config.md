**Anki TUI Bridge** serves Anki's review queue, card content and answer handling
over a localhost HTTP API, so local clients can follow Anki's own card order and
record answer times while Anki runs.

- `host`: interface to bind. Keep `127.0.0.1`. Anything else makes the API
  reachable from your network — that is refused unless a `token` is set or
  `allow_insecure_remote` is enabled.
- `port`: port to listen on (default `8766`).
- `token`: optional shared secret. When set, every request must send it in the
  `X-Anki-Tui-Token` header. This API can read your cards and change your
  scheduling, so set a token if other users or untrusted programs can reach this
  machine.
- `allow_insecure_remote`: set to `true` to serve a non-loopback `host` without a
  token. Only do this on a network you trust.

Changes require restarting Anki.
