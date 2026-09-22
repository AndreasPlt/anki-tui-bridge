"""Anki add-on: expose Anki's own review machinery over localhost HTTP.

Anki holds an exclusive lock on the collection, so external tools cannot open it
while Anki runs. AnkiConnect covers a lot of ground but not the scheduler's
queue and not the answer time, so this add-on serves both — plus the rendered
card and its audio — to clients such as anki-tui.

The add-on is split so the interesting half is testable without Anki:

* :mod:`anki_tui_bridge.bridge` - stdlib HTTP layer, no Anki imports.
* :mod:`anki_tui_bridge.engine` - the Anki-side provider.
* :mod:`anki_tui_bridge.main_thread` - marshals Anki calls onto Anki's thread.
"""

from __future__ import annotations

import sys
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
        problem = config.security_problem()
        if problem is not None:
            message = (
                f"{bridge.NAME}: refusing to listen on {config.host}:{config.port} — {problem}."
            )
            print(message, file=sys.stderr)
            _warn_in_anki(message)
            return None

        server = bridge.BridgeServer(config, _provider())
        try:
            server.start()
        except OSError as error:
            print(f"{bridge.NAME}: could not listen on {config.host}:{config.port}: {error}")
            return None

        _server = server
        print(f"{bridge.NAME}: listening on {server.endpoint}")
        return server


def _warn_in_anki(message: str) -> None:
    """Make a refused start visible in Anki's UI, not just its console."""
    try:  # pragma: no cover - only available inside Anki
        from aqt.utils import showWarning

        showWarning(message, title=bridge.NAME)
    except Exception:  # noqa: BLE001 - never fail startup because of a warning
        pass


def _addon_config() -> dict:
    from aqt import mw

    return mw.addonManager.getConfig(__name__) or {}


def _provider() -> bridge.Provider:
    from .engine import AnkiEngine
    from .main_thread import MainThreadRunner

    return AnkiEngine(MainThreadRunner.for_anki())


if gui_hooks is not None:  # pragma: no cover - only inside Anki
    gui_hooks.profile_did_open.append(start_server)
