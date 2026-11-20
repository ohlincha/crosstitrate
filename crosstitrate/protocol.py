"""
Metrohm Titrino remote-control language (RS232).

The Titrino family (702, 716, 718, 719, 720, 736, 751, 784, 785, 794-799 ...)
shares one command language, described in chapter 3 of each instrument's
"Instructions for Use":

* Commands are sent as ASCII lines terminated by CR LF.
* Objects are addressed in a tree from the root "&", levels separated by ".",
  e.g. ``&Mode.Select``. Names may be abbreviated, but we always send full
  names to be unambiguous across models.
* A value is assigned by appending it in double quotes: ``&Mode.Select "MEAS"``.
* Triggers start with "$": $G go, $S stop, $H hold, $C continue, $Q query,
  $D detailed status, $U quit (abort output).
* The Titrino terminates a data block with CR CR LF; a queried leaf value is
  returned in quotes, e.g. ``"-345.6"``. NV = not valid, OV = overrange.
* $D returns a status message like ``$R.Mode.MEAS.Inac`` optionally followed by
  ``;E26`` error codes.
"""

import re

TERM = "\r\n"          # terminator for commands we send
BLOCK_END = b"\r\r\n"  # terminator of a data block sent by the Titrino

# Triggers
GO, STOP, HOLD, CONTINUE, QUERY, DETAIL, QUIT = "$G", "$S", "$H", "$C", "$Q", "$D", "$U"


class ProtocolError(Exception):
    pass


def format_number(x, max_decimals=4):
    """Format a number the way the Titrino accepts it: max 6 digits, a sign and
    a decimal point, no exponent, leading zero for |x| < 1, max 4 decimals."""
    x = float(x)
    for dec in range(max_decimals, -1, -1):
        s = f"{x:.{dec}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".") or "0"
        digits = sum(c.isdigit() for c in s)
        if digits <= 6:
            if s in ("-0", ""):
                s = "0"
            return s
    raise ProtocolError(f"number {x} cannot be represented with 6 digits")


def set_cmd(path, value):
    """Command assigning a value to an object, e.g. &Mode.Select "MEAS"."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = format_number(value)
    value = str(value)
    if '"' in value or len(value) > 24:
        raise ProtocolError(f"invalid value {value!r}")
    return f'{path} "{value}"'


def trigger_cmd(path, trig):
    return f"{path} {trig}" if path else trig


def query_cmd(path):
    return f"{path} {QUERY}"


_QUOTED = re.compile(r'"([^"]*)"')


def parse_value(reply):
    """Extract the value from the reply to a leaf $Q, e.g. '"-345.6"\\r\\r\\n'.

    Returns the string inside the last pair of quotes (or the stripped reply
    if there are no quotes)."""
    text = reply.decode("ascii", "replace") if isinstance(reply, bytes) else reply
    found = _QUOTED.findall(text)
    if found:
        return found[-1].strip()
    return text.strip()


_NUM = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|[-+]?\.\d+")


def parse_float(reply):
    """Parse a numeric reply. Returns (value, flag) where flag is None, 'NV'
    (not valid) or 'OV' (overrange); value is None unless flag is None."""
    s = parse_value(reply)
    up = s.upper().replace(" ", "")
    if up in ("NV", "NOTVALID", "INVALID") or "NOTVALID" in up:
        return None, "NV"
    if up.startswith("OV") or "OVERRANGE" in up:
        return None, "OV"
    m = _NUM.search(s)
    if not m:
        raise ProtocolError(f"no number in reply {s!r}")
    return float(m.group(0)), None


class Status:
    """Parsed $D status message, e.g. '$G.Assembly.Bur.ModeDis;E27'."""

    GLOBAL_NAMES = {"G": "busy", "H": "on hold", "C": "continuing", "R": "ready", "S": "stopped"}

    def __init__(self, raw):
        self.raw = raw.strip()
        text = self.raw.strip('"').strip()
        parts = [p.strip() for p in text.split(";") if p.strip()]
        head = parts[0] if parts else ""
        self.errors = [p for p in parts[1:] if re.fullmatch(r"E\d+", p)]
        m = re.match(r"\$([GHCRS])\.?(.*)", head)
        if m:
            self.global_ = m.group(1)
            self.detail = m.group(2)
        else:
            # Some replies carry only an error, or are garbled.
            errs = re.findall(r"E\d+", head)
            self.errors = errs + self.errors
            self.global_ = None
            self.detail = head

    @property
    def ready(self):
        return self.global_ in ("R", "S")

    @property
    def busy(self):
        return self.global_ in ("G", "C")

    def describe(self):
        g = self.GLOBAL_NAMES.get(self.global_, "unknown")
        s = f"{g}" + (f" ({self.detail})" if self.detail else "")
        for e in self.errors:
            s += f"; {e}: {ERRORS.get(e, 'unknown error')}"
        return s

    def __repr__(self):
        return f"Status({self.raw!r})"


# Error codes common to the Titrino family (descriptions condensed from the
# 718/751 manuals). Not every model uses every code.
ERRORS = {
    "E8": "memory card read/write error",
    "E9": "wrong memory card",
    "E10": "memory card has lost data",
    "E18": "memory card battery low",
    "E20": "check exchange unit (burette not mounted properly)",
    "E21": "check electrode: short circuit",
    "E22": "check electrode: break",
    "E23": "division by zero",
    "E24": "check drive unit",
    "E26": "manual stop",
    "E27": "stop volume reached",
    "E28": "wrong object (command path not recognised by this model)",
    "E29": "wrong value or no value allowed",
    "E30": "wrong trigger / action not possible now",
    "E31": "command not possible while the instrument is active",
    "E32": "command not possible during titration",
    "E33": "value was corrected automatically",
    "E34": "instrument busy (sample data being edited, or filling)",
    "E36": "RS232 receive error: parity (check parity/data bit settings)",
    "E37": "RS232 receive error: framing (check baud rate / data bits / stop bits)",
    "E38": "RS232 receive error: overrun",
    "E39": "RS232 input buffer full (command too long)",
    "E40": "RS232 transmission error",
    "E41": "RS232 transmission error",
    "E42": "RS232 send error: CTS off for >1 s (handshake/cable problem - the computer "
           "must assert RTS/DTR, or set handshake to 'none' on the Titrino)",
    "E43": "RS232 send blocked by XOFF for >3-6 s",
    "E45": "incomplete command received (LF missing)",
    "E120": "measured value out of range (electrode not connected?)",
    "E121": "measuring point list overflow",
    "E131": "no endpoint set",
    "E135": "check temperature sensor",
    "E167": "dispensing rate too high for the mounted exchange unit",
    "E171": "dispensing rate too low for the mounted exchange unit",
    "E184": "measured value below lower limit",
    "E185": "measured value above upper limit",
    "E270": "dosing element overload: piston blocked",
    "E282": "dosing element overload: stopcock blocked",
}


def explain_error(code):
    return ERRORS.get(code, "unknown error code (see the instrument manual, chapter 'Error messages')")


# ---------------------------------------------------------------------------
# Command paths (full names). Collected here so model profiles can override.
# ---------------------------------------------------------------------------

class Paths:
    mode = "&Mode"
    mode_select = "&Mode.Select"
    meas_quantity = "&Mode.MEASQuantity"
    meas_value = "&Info.ActualInfo.Titrator.Meas"
    meas_temp = "&Info.ActualInfo.Titrator.T"
    meas_drift = "&Info.ActualInfo.Titrator.dMeasdt"
    prog = "&Config.Aux.Prog"
    devname = "&Config.Aux.DevName"
    display1 = "&Info.ActualInfo.Display.L1"
    display2 = "&Info.ActualInfo.Display.L2"
    exv = "&Info.Assembly.ExV"
    counter_v = "&Info.ActualInfo.Assembly.Counter.V"
    counter_clear = "&Info.ActualInfo.Assembly.Counter.Clear"
    bur = "&Assembly.Bur"
    bur_select = "&Assembly.Bur.Select"           # only multi-burette models (751, 785, 799)
    bur_fill = "&Assembly.Bur.Fill"
    bur_dis = "&Assembly.Bur.ModeDis"
    bur_dis_select = "&Assembly.Bur.ModeDis.Select"
    bur_dis_v = "&Assembly.Bur.ModeDis.V"
    bur_dis_autofill = "&Assembly.Bur.ModeDis.AutoFill"
    bur_rate_select = "&Assembly.Bur.Rates.Forward.Select"
    bur_rate = "&Assembly.Bur.Rates.Forward.Digital"
    bur_fill_rate_select = "&Assembly.Bur.Rates.Reverse.Select"
    bur_fill_rate = "&Assembly.Bur.Rates.Reverse.Digital"
    keyboard_lock = "&Setup.Lock.Keyboard"
