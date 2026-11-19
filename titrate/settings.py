"""Persistent settings (JSON) in the platform's config directory."""

import json
import os
import sys


def config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "titrate")


def data_dir():
    d = os.path.join(os.path.expanduser("~"), "Documents")
    if not os.path.isdir(d):
        d = os.path.expanduser("~")
    return os.path.join(d, "Titrations")


def path():
    return os.path.join(config_dir(), "settings.json")


def load():
    try:
        with open(path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save(data):
    os.makedirs(config_dir(), exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path())
