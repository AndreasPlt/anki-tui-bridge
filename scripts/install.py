"""Install the add-on into Anki.

    uv run python scripts/install.py            # install (or update)
    uv run python scripts/install.py --uninstall
    uv run python scripts/install.py --clean    # remove older installs of this add-on

Installing builds `dist/anki_tui_bridge.ankiaddon` (see `build.py`) and opens it with Anki,
which asks to confirm. Replacing an earlier install keeps your config (`meta.json`) and
`user_files/`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build import ADDON_NAME, build

#: Earlier releases of this add-on, which bind the same port and must go.
STALE_ADDON_NAMES = ("anki_queue_bridge", "anki-queue-bridge", "anki-tui-bridge")


def addons_dir() -> Path:
    """Anki's `addons21` folder for this platform."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Anki2"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home())) / "Anki2"
    else:
        data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        base = Path(data_home) / "Anki2"
    return base / "addons21"


def open_with_anki(path: Path) -> None:
    """Hand the file to Anki through the OS file association."""
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=True)
    elif sys.platform.startswith("win"):
        os.startfile(path)
    else:
        subprocess.run(["xdg-open", path], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--addons-dir",
        type=Path,
        default=None,
        help="override addons21 (for --uninstall and --clean)",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove the add-on")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove older installs of this add-on (they bind the same port)",
    )
    args = parser.parse_args()

    root = args.addons_dir or addons_dir()
    if not root.is_dir():
        print(f"add-ons folder not found: {root}", file=sys.stderr)
        print("Is Anki installed? Use --addons-dir to point at it manually.", file=sys.stderr)
        return 1

    stale = [
        root / name for name in STALE_ADDON_NAMES if name != ADDON_NAME and (root / name).is_dir()
    ]
    if stale:
        print("older versions of this add-on are installed and occupy the same port:")
        for path in stale:
            print(f"  {path}")
        if args.clean:
            for path in stale:
                shutil.rmtree(path)
                print(f"removed {path}")
        else:
            print("remove them with --clean before starting Anki, then install this version.")

    target = root / ADDON_NAME
    if args.uninstall:
        if target.exists():
            shutil.rmtree(target)
            print(f"removed {target}")
        else:
            print(f"not installed: {target}")
        return 0

    package = build()
    print(f"built {package}")
    open_with_anki(package)
    print("Confirm the install in Anki, then restart Anki to start the bridge.")
    print("Verify with: curl http://127.0.0.1:8766/health")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
