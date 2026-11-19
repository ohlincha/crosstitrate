"""
Connection diagnostics: step-by-step checks with plain-language advice.
"""

import os
import sys
import time

from . import protocol, transport
from .transport import SerialSettings, SerialTransport, TransportError

OK, INFO, WARN, FAIL = "ok", "info", "warning", "fail"


class Finding:
    def __init__(self, level, title, detail=""):
        self.level, self.title, self.detail = level, title, detail

    def __repr__(self):
        return f"[{self.level}] {self.title}"


TITRINO_SETUP = """\
Settings on the Titrino (press <configuration>, go to ">RS232 settings"):
  baud rate   9600
  data bit    8
  stop bit    1
  parity      none
  handshake   none   (or HWs if your cable has all handshake wires, e.g. Metrohm 6.2125.060)
  RS control  ON     <- if OFF, the Titrino ignores every command
Under ">peripheral units", "send to" = IBM is recommended for computer connection.
The instrument must be idle (not running a method, no question on the display).
Connect the electrode to measuring input 1."""

CABLE_HELP = """\
Cable: the Titrino's RS232 socket (marked A or "RS232") is a 25-pin connector.
Use the Metrohm computer cable 6.2125.060 (+ adapter 6.2125.010 for 9-pin PC ports),
or any null-modem cable (TxD/RxD crossed, RTS/CTS and DTR/DSR crossed).
Most PCs need a USB-RS232 adapter; FTDI-based adapters are the most reliable."""


def environment_checks():
    out = []
    try:
        import serial  # noqa: F401
        out.append(Finding(OK, f"pyserial {serial.__version__} available"))
    except ImportError:
        out.append(Finding(FAIL, "pyserial is missing", "Install it with: pip install pyserial"))
        return out
    if sys.platform.startswith("linux"):
        try:
            import grp
            groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
            if "dialout" not in groups and os.geteuid() != 0:
                out.append(Finding(WARN, "You are not in the 'dialout' group",
                                   "Serial ports may be inaccessible. Fix with:\n"
                                   "    sudo usermod -aG dialout $USER\nthen log out and in again."))
            else:
                out.append(Finding(OK, "Serial port permissions (dialout group) OK"))
        except Exception:  # noqa: BLE001
            pass
    ports = transport.list_ports()
    if not ports:
        out.append(Finding(WARN, "No serial ports found",
                           "Plug in the USB-RS232 adapter. If it still doesn't appear, install its driver "
                           "(FTDI: ftdichip.com/drivers; Prolific: prolific.com.tw)."))
    for p in ports:
        desc = f"{p['device']}: {p['description']}"
        if p["chip"]:
            desc += f"  [{p['chip']} {p['vid']}:{p['pid']}]"
        level, detail = INFO, ""
        if p["chip"] == "Prolific" and sys.platform == "win32":
            level = WARN
            detail = ("Prolific adapters (and clones) often fail with recent Windows drivers "
                      "(\"PL2303HXA PHASED OUT\", code 10). If it doesn't work, use an FTDI-based adapter.")
        if "bluetooth" in p["description"].lower():
            detail = "Bluetooth virtual port - not your Titrino adapter."
        out.append(Finding(level, desc, detail))
    return out


def _try_status(port, settings, timeout=1.2):
    """Open port, send $D, return (reply_text, modem_lines) or raise TransportError."""
    t = SerialTransport(port, settings)
    try:
        time.sleep(0.1)
        lines = t.modem_lines()
        reply = t.request(protocol.DETAIL, timeout)
        return reply, lines, t
    except Exception:
        t.close()
        raise


SCAN_BAUDS = [9600, 4800, 2400, 1200, 19200, 600, 300, 38400, 57600, 115200]
SCAN_FRAMES = [(8, "N", 1), (7, "E", 1), (7, "N", 1), (7, "O", 1), (8, "E", 1), (8, "N", 2)]


def diagnose_port(port, settings=None, progress=None, scan=True):
    """Run the connection checks on one port. Returns (findings, working_settings or None, ident)."""
    settings = settings or SerialSettings()
    say = progress or (lambda msg: None)
    findings = []

    say(f"Opening {port} ({settings.describe()})…")
    try:
        reply, lines, t = _try_status(port, settings)
    except TransportError as e:
        findings.append(Finding(FAIL, f"Cannot open {port}", str(e)))
        return findings, None, None
    findings.append(Finding(OK, f"Opened {port}"))

    if lines:
        state = ", ".join(f"{k} {'on' if v else 'off'}" for k, v in lines.items() if k != "RI")
        if lines.get("CTS") or lines.get("DSR"):
            findings.append(Finding(OK, f"Handshake lines: {state}",
                                    "Something is connected and powered (the Titrino raises DTR/RTS)."))
        else:
            findings.append(Finding(INFO, f"Handshake lines: {state}",
                                    "No handshake signals from the other side. Normal for 3-wire cables; "
                                    "otherwise the Titrino may be off, unplugged, or connected with a "
                                    "straight (non-null-modem) cable."))

    ident = None
    if reply.strip() and protocol.Status(reply).global_:
        st = protocol.Status(reply)
        findings.append(Finding(OK, "Titrino answers", f"Status: {st.describe()}"))
        ident = identify(t, findings)
        t.close()
        return findings, settings, ident

    t.close()
    if reply.strip():
        if protocol.DETAIL in reply and reply.strip().startswith("$D"):
            findings.append(Finding(WARN, "We receive our own command back (echo/loopback)",
                                    "The cable or adapter loops TxD to RxD (or a loopback plug is "
                                    "fitted). The adapter itself works, but nothing is answering."))
        else:
            findings.append(Finding(WARN, "Received data, but it is garbled",
                                    f"Got {reply!r}. The baud rate / data bits / parity probably differ "
                                    "between the computer and the Titrino."))
    else:
        findings.append(Finding(WARN, f"No answer at {settings.describe()}"))

    if scan:
        say("Scanning other serial settings (this takes up to a minute)…")
        for baud in SCAN_BAUDS:
            for bits, parity, stop in SCAN_FRAMES:
                trial = SerialSettings(baud, bits, parity, stop, "none", settings.assert_lines)
                if (trial.baud, trial.bytesize, trial.parity, trial.stopbits) == (
                        settings.baud, settings.bytesize, settings.parity, settings.stopbits):
                    continue
                say(f"Trying {trial.describe()}…")
                try:
                    reply, _, t = _try_status(port, trial, timeout=0.8)
                except TransportError:
                    continue
                ok = reply.strip() and protocol.Status(reply).global_
                if ok:
                    findings.append(Finding(OK, f"Titrino answers at {trial.describe()}",
                                            "Use these settings in the program, or change the Titrino "
                                            "to 9600 8N1."))
                    ident = identify(t, findings)
                    t.close()
                    return findings, trial, ident
                t.close()
        findings.append(Finding(FAIL, "The Titrino did not answer with any common setting"))

    findings.append(Finding(INFO, "Things to check", "\n".join([
        "1. Is the Titrino switched on and showing its normal display (no error, not running)?",
        "2. Configuration > RS232 settings > RS control must be ON.",
        "3. If the Titrino display shows E42 (or the program stalls on writes): set handshake to "
        "'none' on the Titrino, or use a cable with all handshake wires.",
        "4. The cable must be a null-modem / Metrohm 6.2125.060 cable, plugged into the Titrino's RS232 "
        "socket (A), not the remote socket.",
        "5. Is this the right port? Unplug the adapter, press Refresh, and see which port disappears.",
        "6. Test the adapter: bridge pins 2 and 3 on its plug; the diagnostics should then report "
        "an echo.",
    ])))
    return findings, None, None


def identify(t, findings):
    from .instrument import Titrino, NoResponse
    ins = Titrino(t)
    try:
        info = ins.identify()
    except NoResponse as e:
        findings.append(Finding(WARN, "Status OK but identification failed", str(e)))
        return None
    level = OK if info["prog"] else WARN
    findings.append(Finding(level, f"Instrument: {info['model']}",
                            f"Program version {info['prog'] or '?'}; device name '{info['devname']}'; "
                            f"exchange unit {str(info['exv']) + ' mL' if info['exv'] else 'unknown'}.\n"
                            + (info["notes"] or "")))
    l1, l2 = ins.display()
    if l1 or l2:
        findings.append(Finding(INFO, "Titrino display", f"{l1}\n{l2}"))
    try:
        rs = {k: ins.query(f"&Config.RSSet.{k}") for k in ("Baud", "DataBit", "StopBit", "Parity", "Handsh")}
        findings.append(Finding(INFO, "RS232 settings reported by the Titrino",
                                ", ".join(f"{k} {v}" for k, v in rs.items())))
    except NoResponse:
        pass
    st = ins.status()
    if st.busy:
        findings.append(Finding(WARN, "The Titrino is busy", f"{st.describe()}. Stop it before titrating."))
    return info


def scan_ports(settings=None, progress=None):
    """Quickly check every port for an answering Titrino. Returns list of (port, ok, text)."""
    settings = settings or SerialSettings()
    results = []
    for p in transport.list_ports():
        if progress:
            progress(f"Checking {p['device']}…")
        try:
            reply, _, t = _try_status(p["device"], settings, timeout=0.8)
            t.close()
            ok = bool(reply.strip()) and protocol.Status(reply).global_ is not None
            results.append((p["device"], ok, protocol.Status(reply).describe() if ok else
                            ("garbled reply" if reply.strip() else "no answer")))
        except TransportError as e:
            results.append((p["device"], False, str(e).splitlines()[0]))
    return results
