**Anki Queue Bridge** exposes Anki's own review queue on a localhost HTTP
endpoint, so local clients can follow Anki's card ordering while Anki runs.

- `host`: interface to bind. Keep `127.0.0.1` unless you deliberately want other
  machines to reach it.
- `port`: port to listen on (default `8766`).
- `token`: optional shared secret. When set, every request must send it in the
  `X-Anki-Queue-Token` header. Set this if other local users/processes should not
  be able to read your queue.

Changing these requires restarting Anki. The endpoint is read-only: it never
answers cards or modifies scheduling.
