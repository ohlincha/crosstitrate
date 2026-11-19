"""
Byte transports to an instrument, with a shared traffic log.

* SerialTransport: RS232 ports, USB-RS232 adapters (FTDI, Prolific, CH340, ...),
  instruments with a native USB virtual COM port, and network serial servers
  via pyserial URLs ("socket://host:port", "rfc2217://host:port").
* SimTransport: talks to the built-in simulated Titrino.
"""

import collections
import threading
import time

from . import protocol


class TransportError(Exception):
    pass


class CommLog:
    """Thread-safe ring buffer of traffic, shown in the diagnostics console."""

    def __init__(self, maxlen=5000):
        self.entries = collections.deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.listeners = []

    def add(self, port, direction, data):
        if isinstance(data, bytes):
            data = data.decode("ascii", "replace")
        entry = (time.time(), port, direction, data)
        with self.lock:
            self.entries.append(entry)
        for fn in list(self.listeners):
            try:
                fn(entry)
            except Exception:  # noqa: BLE001
                pass

    def text(self):
        with self.lock:
            return "\n".join(format_entry(e) for e in self.entries)


def format_entry(entry):
    t, port, direction, data = entry
    ts = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
    shown = data.replace("\r", "\\r").replace("\n", "\\n")
    return f"{ts} {port:>12} {direction} {shown}"


LOG = CommLog()


class SerialSettings:
    def __init__(self, baud=9600, bytesize=8, parity="N", stopbits=1, handshake="none",
                 assert_lines=True):
        self.baud = int(baud)
        self.bytesize = int(bytesize)
        self.parity = parity          # "N", "E", "O"
        self.stopbits = int(stopbits)
        self.handshake = handshake    # "none", "rtscts", "xonxoff"
        self.assert_lines = assert_lines  # raise RTS and DTR (needed for Titrino HWs handshake)

    def to_dict(self):
        return dict(baud=self.baud, bytesize=self.bytesize, parity=self.parity,
                    stopbits=self.stopbits, handshake=self.handshake, assert_lines=self.assert_lines)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in (d or {}).items()
                      if k in ("baud", "bytesize", "parity", "stopbits", "handshake", "assert_lines")})

    def describe(self):
        return f"{self.baud} {self.bytesize}{self.parity}{self.stopbits}, handshake {self.handshake}"


class Transport:
    """Base class: line-oriented request/response with the Titrino."""

    name = "?"

    def __init__(self):
        self.lock = threading.RLock()

    # to implement: _write(bytes), _read(n, timeout) -> bytes, close(), modem_lines()
    def send(self, line):
        """Send one command line (terminator added)."""
        with self.lock:
            LOG.add(self.name, "→", line)
            self._write((line + protocol.TERM).encode("ascii"))

    def read_block(self, timeout=1.5):
        """Read until the Titrino's block terminator (CR CR LF) or a plain CR LF line
        after a short quiet period. Returns the raw text ('' on timeout)."""
        with self.lock:
            buf = b""
            deadline = time.monotonic() + timeout
            quiet_since = None
            while time.monotonic() < deadline:
                chunk = self._read(256, 0.05)
                if chunk:
                    buf += chunk
                    quiet_since = time.monotonic()
                    if buf.endswith(protocol.BLOCK_END):
                        break
                elif buf.endswith(b"\n") and quiet_since and time.monotonic() - quiet_since > 0.15:
                    break  # some models/settings end with a single CR LF
            text = buf.decode("ascii", "replace")
            if text:
                LOG.add(self.name, "←", text)
            return text

    def flush_input(self):
        with self.lock:
            junk = self._read(4096, 0.02)
            while junk:
                LOG.add(self.name, "← (discarded)", junk)
                junk = self._read(4096, 0.02)

    def request(self, line, timeout=1.5):
        """Send a command and return the reply block (for $Q / $D)."""
        with self.lock:
            self.flush_input()
            self.send(line)
            return self.read_block(timeout)

    def modem_lines(self):
        return {}

    def close(self):
        pass


class SerialTransport(Transport):
    def __init__(self, port, settings=None):
        super().__init__()
        try:
            import serial  # pyserial
        except ImportError as e:  # pragma: no cover
            raise TransportError("pyserial is not installed (pip install pyserial)") from e
        self.serial_mod = serial
        self.settings = settings or SerialSettings()
        self.name = port.replace("/dev/", "")
        s = self.settings
        kwargs = dict(baudrate=s.baud, bytesize=s.bytesize, parity=s.parity, stopbits=s.stopbits,
                      timeout=0.05, write_timeout=2,
                      rtscts=s.handshake == "rtscts", xonxoff=s.handshake == "xonxoff")
        try:
            if "://" in port:
                self.ser = serial.serial_for_url(port, **kwargs)
            else:
                self.ser = serial.Serial(port, **kwargs)
        except serial.SerialException as e:
            raise TransportError(friendly_serial_error(port, e)) from e
        if s.assert_lines and "://" not in port:
            try:
                self.ser.dtr = True
                if s.handshake != "rtscts":
                    self.ser.rts = True
            except Exception:  # noqa: BLE001 - not supported by every driver
                pass
        LOG.add(self.name, "··", f"opened {port} ({s.describe()})")

    def _write(self, data):
        try:
            self.ser.write(data)
            self.ser.flush()
        except self.serial_mod.SerialTimeoutException as e:
            raise TransportError("write timed out - hardware handshake is blocking (CTS off). "
                                 "Check the cable or set handshake to 'none'.") from e
        except (OSError, self.serial_mod.SerialException) as e:
            raise TransportError(f"serial port error: {e} (adapter unplugged?)") from e

    def _read(self, n, timeout):
        try:
            self.ser.timeout = timeout
            return self.ser.read(n)
        except (OSError, self.serial_mod.SerialException) as e:
            raise TransportError(f"serial port error: {e} (adapter unplugged?)") from e

    def modem_lines(self):
        try:
            return {"CTS": self.ser.cts, "DSR": self.ser.dsr, "CD": self.ser.cd, "RI": self.ser.ri}
        except Exception:  # noqa: BLE001
            return {}

    def close(self):
        try:
            self.ser.close()
            LOG.add(self.name, "··", "closed")
        except Exception:  # noqa: BLE001
            pass


def friendly_serial_error(port, e):
    msg = str(e)
    low = msg.lower()
    if "permission" in low or "access is denied" in low or "errno 13" in low:
        import sys
        if sys.platform.startswith("linux"):
            return (f"Permission denied for {port}. Add yourself to the 'dialout' group:\n"
                    "    sudo usermod -aG dialout $USER\nthen log out and in again.")
        if "access is denied" in low:
            return f"{port} is in use by another program (close it, e.g. the old WinTitrate) or access is denied."
        return f"Permission denied for {port}: {msg}"
    if "busy" in low or "resource temporarily" in low or "errno 16" in low:
        return f"{port} is in use by another program."
    if "no such file" in low or "could not open port" in low or "filenotfound" in low:
        return f"{port} does not exist (adapter unplugged? use Refresh)."
    return f"Cannot open {port}: {msg}"


class SimTransport(Transport):
    """Connects to a SimulatedTitrino object in-process."""

    def __init__(self, sim, name="SIM"):
        super().__init__()
        self.sim = sim
        self.name = name
        self.outbuf = b""
        LOG.add(self.name, "··", f"connected to simulator ({sim.model})")

    def _write(self, data):
        self.outbuf += self.sim.receive(data)

    def _read(self, n, timeout):
        if not self.outbuf:
            time.sleep(min(timeout, 0.01))
            self.outbuf += self.sim.poll_output()
        out, self.outbuf = self.outbuf[:n], self.outbuf[n:]
        return out

    def modem_lines(self):
        return {"CTS": True, "DSR": True, "CD": True, "RI": False}


def list_ports():
    """Available serial ports as list of dicts with useful details."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    out = []
    for p in sorted(lp.comports(), key=lambda p: p.device):
        vid = f"{p.vid:04x}" if p.vid is not None else ""
        pid = f"{p.pid:04x}" if p.pid is not None else ""
        out.append(dict(device=p.device, description=p.description or "", manufacturer=p.manufacturer or "",
                        vid=vid, pid=pid, serial=p.serial_number or "", hwid=p.hwid or "",
                        chip=adapter_chip(vid, pid, p.manufacturer or "", p.description or "")))
    return out


_CHIPS = {
    "0403": "FTDI", "067b": "Prolific", "1a86": "WCH CH340/CH341", "10c4": "Silicon Labs CP210x",
    "0557": "ATEN", "2478": "Tripp-Lite / Keyspan", "06cd": "Keyspan", "04e2": "Exar",
}


def adapter_chip(vid, pid, manufacturer, description):
    if vid in _CHIPS:
        return _CHIPS[vid]
    text = (manufacturer + " " + description).lower()
    for key in ("ftdi", "prolific", "ch340", "cp210", "metrohm"):
        if key in text:
            return key.upper()
    return ""
