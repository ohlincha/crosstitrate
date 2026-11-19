"""
Data files.

Primary format: CSV with a commented metadata header ("# key: value") followed
by one row per measurement. Rows are flushed immediately, so data survives a
crash or power cut.

Legacy format: the text format written by the old WinTitrate (VB6) program,
for existing analysis programs:

    "Datum",#2002-04-24#
    "Start:konc.",V0,H0,B0,C0,D0,0,0
    "Byrkonc",H,B,C,D,0,0,0
    "10 23 45",Vt,Temp,EMF,0,0
    ...
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


# --------------------------------------------------------------------------
# Legacy WinTitrate format
# --------------------------------------------------------------------------

def vb_number(x):
    """Format a number the way VB6's Write # statement does (e.g. 0.5 -> .5)."""
    x = float(x)
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    s = repr(x)
    if "e" in s or "E" in s:
        s = f"{x:.15g}"
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s


def vb_write(fields):
    out = []
    for f in fields:
        if isinstance(f, str):
            out.append(f'"{f}"')
        elif isinstance(f, dt.date):
            out.append(f"#{f.isoformat()}#")
        else:
            out.append(vb_number(f))
    return ",".join(out)


def export_legacy(path, meta, rows, components=("H", "B", "C", "D")):
    """Write the WinTitrate text format from a titration's metadata and rows."""
    sample = meta.get("sample", {})
    start = meta.get("start_time")
    date = dt.datetime.fromisoformat(start).date() if isinstance(start, str) else dt.date.today()
    conc0 = sample.get("concentrations", {})
    lines = [vb_write(["Datum", date]),
             vb_write(["Start:konc.", float(sample.get("v0_ml", 0))]
                      + [float(conc0.get(c, 0) or 0) for c in components] + [0.0, 0.0])]

    def burette_lines(burettes):
        out = []
        for i, b in enumerate(burettes):
            conc = b.get("concentrations", {})
            tag = "Byrkonc" if i == 0 else "Byrkonc2"
            out.append(vb_write([tag] + [float(conc.get(c, 0) or 0) for c in components] + [0.0, 0.0, 0.0]))
        return out

    lines += burette_lines(meta.get("active_burettes", []))
    temp_default = float(sample.get("temperature_c", 25.0) or 25.0)
    for r in rows:
        kind = r.get("kind")
        if kind == "event" and r.get("note", "").startswith("burette change"):
            try:
                info = json.loads(r["note"].split(":", 1)[1])
                lines += burette_lines(info.get("active_burettes", []))
            except (ValueError, IndexError, KeyError):
                pass
            continue
        if kind not in ("eq", "drift", "manual") or r.get("emf_mv", "") == "":
            continue
        t = dt.datetime.fromisoformat(r["timestamp"])
        temp = float(r["temperature_c"]) if r.get("temperature_c") not in ("", None) else temp_default
        lines.append(vb_write([t.strftime("%H %M %S"), float(r["v_segment_ml"]), temp,
                               float(r["emf_mv"]), 0.0, 0.0]))
    with open(path, "w", newline="", encoding="ascii", errors="replace") as fh:
        fh.write("\r\n".join(lines) + "\r\n")
