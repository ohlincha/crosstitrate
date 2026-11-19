"""
Data files.

CSV with a commented metadata header ("# key: value") followed by one row
per measurement. Rows are flushed immediately, so data survives a crash or
power cut.
"""

import csv
import datetime as dt
import io
import json
import os

COLUMNS = ["timestamp", "elapsed_s", "segment", "kind", "burettes", "v_total_ml", "v_segment_ml",
           "emf_mv", "temperature_c", "drift_mv", "note"]
# kind: "eq" = accepted (equilibrium) point, "drift" = intermediate drift-check reading,
#       "manual" = measured manually, "event" = burette change / comment


def iso(t):
    return dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


class DataFile:
    def __init__(self, path, meta, append=False):
        self.path = path
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        self.fh = open(path, "a" if append else "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.fh)
        if not (append and exists):
            for key, value in meta.items():
                if not isinstance(value, str):
                    value = json.dumps(value, ensure_ascii=False)
                for line in str(value).splitlines() or [""]:
                    self.fh.write(f"# {key}: {line}\n")
            self.w.writerow(COLUMNS)
        self.fh.flush()

    def write(self, row):
        self.w.writerow([_fmt(row.get(c, "")) for c in COLUMNS])
        self.fh.flush()
        try:
            os.fsync(self.fh.fileno())
        except OSError:
            pass

    def comment(self, key, value):
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        self.fh.write(f"# {key}: {value}\n")
        self.fh.flush()

    def close(self):
        try:
            self.fh.close()
        except Exception:  # noqa: BLE001
            pass


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1e6 else repr(v)
    return v


def read(path):
    """Read a data file. Returns (meta dict, list of row dicts)."""
    meta, lines = {}, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("# "):
                key, _, value = line[2:].rstrip("\n").partition(": ")
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    pass
                meta[key] = value
            else:
                lines.append(line)
    rows = list(csv.DictReader(io.StringIO("".join(lines))))
    return meta, rows
