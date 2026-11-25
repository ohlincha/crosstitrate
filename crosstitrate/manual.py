"""Find and open the user manual (docs/MANUAL.html, shipped with every installer)."""

import os
import sys
import webbrowser

NAME = "MANUAL.html"
ONLINE = "https://github.com/ohlincha/crosstitrate/blob/main/docs/MANUAL.md"


def candidates():
    """Places the manual may be: next to the package (source checkout, .deb: /usr/lib/crosstitrate/docs),
    inside a PyInstaller bundle, or in the .deb's doc folder."""
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = [os.path.join(pkg_parent, "docs", NAME)]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        out.append(os.path.join(bundle, "docs", NAME))
    out.append(os.path.join("/usr/share/doc/crosstitrate", NAME))
    return out


def find():
    """Path of the manual, or None if it is not installed."""
    for p in candidates():
        if os.path.isfile(p):
            return p
    return None


def open_manual():
    """Open the manual in the system's browser. Returns (ok, message)."""
    path = find()
    if not path:
        return False, ("The manual was not found on this computer.\n\n"
                       f"It is also online:\n{ONLINE}")
    try:
        from pathlib import Path
        if webbrowser.open(Path(path).as_uri()):
            return True, path
    except Exception:  # noqa: BLE001
        pass
    return False, f"Could not open a web browser. Open this file by hand:\n{path}"
