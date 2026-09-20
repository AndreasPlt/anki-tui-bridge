"""Anki add-on: expose the scheduler's *real* review queue over localhost HTTP.

Anki holds an exclusive lock on the collection, so external tools cannot open it
while Anki runs, and AnkiConnect exposes searches and the GUI bridge but not the
scheduler's queue. The scheduler itself knows the answer (it applies daily
limits, learning steps, sibling burying, gather order and interleaving).

This add-on answers that single question through a tiny read-only HTTP endpoint so
local clients - anki-tui being the reference one - can follow Anki's own ordering
without hijacking Anki's window.

The add-on is split in two so the interesting half is testable without Anki:

* :mod:`anki_queue_bridge.bridge` - stdlib HTTP layer, no Anki imports.
* :mod:`anki_queue_bridge.queue` - the Anki-side provider.
"""

from __future__ import annotations

import threading

from . import bridge

try:  # pragma: no cover - only available inside Anki
    from aqt import gui_hooks
except ImportError:  # imported outside Anki (tests, packaging)
    gui_hooks = None

_server: bridge.BridgeServer | None = None
_lock = threading.Lock()


def start_server() -> bridge.BridgeServer | None:
    """Start the bridge once the profile (and thus the collection) is open."""
    global _server

    with _lock:
        if _server is not None:
            return _server

        config = bridge.BridgeConfig.from_mapping(_addon_config())
        server = bridge.BridgeServer(config, _provider())
        try:
            server.start()
        except OSError as error:
            print(f"anki-queue-bridge: could not listen on {config.host}:{config.port}: {error}")
            return None

        _server = server
        print(f"anki-queue-bridge: listening on {server.endpoint}")
        return server


def _addon_config() -> dict:
    from aqt import mw

    return mw.addonManager.getConfig(__name__) or {}


def _provider() -> bridge.QueueProvider:
    from . import queue

    return queue.AnkiQueue()


if gui_hooks is not None:  # pragma: no cover - only inside Anki
    gui_hooks.profile_did_open.append(start_server)
