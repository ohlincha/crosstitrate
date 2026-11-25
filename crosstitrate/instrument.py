"""
High-level driver for one Titrino-family instrument.
"""

import time

from . import models, protocol
from .protocol import Paths as P
from .simulator import REAL_CLOCK


class InstrumentError(Exception):
    def __init__(self, message, codes=()):
        super().__init__(message)
        self.codes = list(codes)


class NoResponse(InstrumentError):
    pass


# Error codes that mean "the command did not work"
FATAL = {"E20", "E21", "E22", "E24", "E28", "E29", "E30", "E31", "E32", "E34", "E36", "E37", "E38",
         "E39", "E42", "E43", "E45", "E167", "E171", "E270", "E282"}


class Titrino:
    def __init__(self, transport, clock=None, label=""):
        self.t = transport
        self.clock = clock or REAL_CLOCK
        self.label = label or transport.name
        self.profile = models.GENERIC
        self.prog = ""
        self.devname = ""
        self.exv = None                # exchange unit volume (mL)
        self.piston_used = 0.0         # mL dispensed since last fill (tracked by us)
        self.measuring = False
        self.burette_select = None     # e.g. "internal D0" on multi-burette models

    # ------------------------------------------------------------- low level
    def query(self, path, timeout=1.5):
        reply = self.t.request(protocol.query_cmd(path), timeout)
        if not reply.strip():
            raise NoResponse(f"{self.label}: no reply to {path} $Q")
        return protocol.parse_value(reply)

    def query_float(self, path, timeout=1.5):
        reply = self.t.request(protocol.query_cmd(path), timeout)
        if not reply.strip():
            raise NoResponse(f"{self.label}: no reply to {path} $Q")
        return protocol.parse_float(reply)

    def status(self, timeout=1.5):
        reply = self.t.request(protocol.DETAIL, timeout)
        if not reply.strip():
            raise NoResponse(f"{self.label}: no reply to status request ($D)")
        return protocol.Status(reply)

    def send(self, *lines, check=True):
        """Send command line(s); then check the status for error codes."""
        for line in lines:
            self.t.send(line)
            time.sleep(0.05)   # the Titrino needs a moment between commands
        if check:
            st = self.status()
            bad = [e for e in st.errors if e in FATAL]
            if bad:
                raise InstrumentError(
                    f"{self.label}: " + "; ".join(f"{e}: {protocol.explain_error(e)}" for e in bad)
                    + f"  (command: {lines[-1]})", bad)
            return st
        return None

    def set(self, path, value, check=True):
        return self.send(protocol.set_cmd(path, value), check=check)

    # ------------------------------------------------------------- info
    def ping(self):
        """True if the instrument answers a status request."""
        try:
            self.status(timeout=1.0)
            return True
        except NoResponse:
            return False

    def identify(self, attempts=3):
        # A Titrino that was already on when the port was opened may miss the first request (see
        # SerialTransport.wake): wake it and ask again before giving up.
        for i in range(attempts):
            self.t.wake()
            try:
                st = self.status()
                break
            except NoResponse:
                if i == attempts - 1:
                    raise
        try:
            self.prog = self.query(P.prog)
        except NoResponse:
            self.prog = ""
        self.profile = models.profile_for_prog(self.prog)
        try:
            self.devname = self.query(P.devname)
        except NoResponse:
            self.devname = ""
        self.exv = None
        try:
            v, flag = self.query_float(P.exv)
            if flag is None and v > 0:
                self.exv = v
        except (NoResponse, protocol.ProtocolError):
            pass
        return dict(prog=self.prog, model=self.profile.title, devname=self.devname, exv=self.exv,
                    status=st.describe(), verified=self.profile.verified, notes=self.profile.notes)

    def display(self):
        try:
            return self.query(P.display1), self.query(P.display2)
        except NoResponse:
            return "", ""

    def wait_ready(self, timeout, poll=0.3, stop_event=None):
        """Poll $D until the instrument is no longer busy. Returns final Status."""
        end = time.monotonic() + timeout
        while True:
            st = self.status()
            bad = [e for e in st.errors if e in FATAL]
            if bad:
                raise InstrumentError(f"{self.label}: " + "; ".join(
                    f"{e}: {protocol.explain_error(e)}" for e in bad), bad)
            if not st.busy:
                return st
            if time.monotonic() > end:
                raise InstrumentError(f"{self.label}: still busy after {timeout:.0f} s ({st.describe()})")
            if stop_event is not None and stop_event.wait(poll):
                return st
            elif stop_event is None:
                time.sleep(poll)

    # ------------------------------------------------------------- measuring
    def start_measuring(self, quantity="U"):
        if quantity not in self.profile.quantities and self.profile is not models.GENERIC:
            raise InstrumentError(f"{self.profile.title} cannot measure {quantity}")
        self.send(protocol.set_cmd(P.mode_select, self.profile.meas_mode),
                  protocol.set_cmd(P.meas_quantity, quantity))
        self.send(protocol.trigger_cmd(P.mode, protocol.GO))
        self.measuring = True

    def read_measurement(self, retries=10, retry_delay=0.5):
        """Current measured value (mV for U). Retries while the Titrino reports NV."""
        for _ in range(retries):
            value, flag = self.query_float(P.meas_value)
            if flag is None:
                return value
            if flag == "OV":
                raise InstrumentError(f"{self.label}: measured value overrange (electrode connected?)",
                                      ["E120"])
            time.sleep(retry_delay)
        raise InstrumentError(f"{self.label}: no valid measured value (instrument keeps reporting 'NV')")

    def read_temperature(self):
        try:
            value, flag = self.query_float(P.meas_temp)
            return value if flag is None else None
        except (NoResponse, protocol.ProtocolError):
            return None

    def stop_measuring(self):
        self.send(protocol.trigger_cmd(P.mode, protocol.STOP), check=False)
        self.measuring = False
        # A real 785 DMP takes longer than a fixed short pause to leave measuring mode; a dosing
        # command sent too early fails with E31 ("not possible while active"). Wait for the status.
        time.sleep(0.2)
        try:
            self.wait_ready(timeout=10)
        except InstrumentError:
            pass

    # ------------------------------------------------------------- burette
    def _select_burette(self):
        if self.burette_select and self.profile.multi_burette:
            self.set(P.bur_select, self.burette_select)

    def read_counter(self):
        try:
            v, flag = self.query_float(P.counter_v)
            return v if flag is None else None
        except (NoResponse, protocol.ProtocolError):
            return None

    def dispense(self, volume, rate="max.", stop_event=None, autofill=False):
        """Dispense `volume` mL at `rate` mL/min (or 'max.'). Blocks until done.
        Refills the burette first (or in between) when the piston would run out.
        Returns the dispensed volume (from the instrument's counter when available)."""
        total, remaining = 0.0, float(volume)
        while remaining > 1e-6:
            if stop_event is not None and stop_event.is_set():
                break
            chunk = remaining
            if self.exv:
                room = self.exv * 0.98 - self.piston_used
                if room < min(remaining, self.exv * 0.98) - 1e-9:
                    self.fill(stop_event=stop_event)
                    room = self.exv * 0.98
                chunk = min(remaining, room)
            done = self._dispense_once(chunk, rate, stop_event, autofill)
            total += done
            remaining -= chunk
        return round(total, 4)

    def _dispense_once(self, volume, rate, stop_event, autofill):
        if volume <= 0:
            return 0.0
        if self.measuring:
            self.stop_measuring()
        self._select_burette()
        before = self.read_counter()
        rate_value = rate if isinstance(rate, str) else protocol.format_number(rate, 2)
        self.send(protocol.set_cmd(P.bur_dis_select, "volume"),
                  protocol.set_cmd(P.bur_rate_select, "digital"),
                  protocol.set_cmd(P.bur_rate, rate_value),
                  protocol.set_cmd(P.bur_dis_autofill, "ON" if autofill else "OFF"),
                  protocol.set_cmd(P.bur_dis_v, protocol.format_number(volume, 4)))
        self.send(protocol.trigger_cmd(P.bur_dis, protocol.GO))
        try:
            r = float(rate)
        except (TypeError, ValueError):
            r = 10.0
        expected = volume / max(r, 0.01) * 60.0
        self.wait_ready(timeout=expected * 3 + 30, stop_event=stop_event)
        if stop_event is not None and stop_event.is_set():
            self.stop()   # user pressed Stop while dispensing
            time.sleep(0.3)
        after = self._settled_counter(before, volume, expected, stop_event)
        stopped = stop_event is not None and stop_event.is_set()
        if before is not None and after is not None and after >= before:
            actual = round(after - before, 4)
            if not stopped and abs(actual - volume) > max(0.002, 0.02 * volume):
                raise InstrumentError(f"{self.label}: requested {volume:.4f} mL but the counter shows "
                                      f"{actual:.4f} mL dispensed")
        else:
            actual = volume
        self.piston_used = 0.0 if autofill else self.piston_used + actual
        return actual

    def _settled_counter(self, before, volume, expected, stop_event):
        """Read the volume counter until it reaches the requested volume (or stops moving / times out).

        Seen on a real 785 DMP: right after a dose the status can already say 'stopped' while the
        counter still lags behind, so a single read can be short."""
        after = self.read_counter()
        if before is None or after is None:
            return after
        tol = max(0.002, 0.02 * volume)
        deadline = time.monotonic() + expected * 2 + 10
        while after - before < volume - tol and time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(0.5)
            nxt = self.read_counter()
            if nxt is None:
                break
            after = nxt
        return after

    def fill(self, rate="max.", stop_event=None):
        if self.measuring:
            self.stop_measuring()
        self._select_burette()
        rate_value = rate if isinstance(rate, str) else protocol.format_number(rate, 2)
        self.send(protocol.set_cmd(P.bur_fill_rate_select, "digital"),
                  protocol.set_cmd(P.bur_fill_rate, rate_value))
        self.send(protocol.trigger_cmd(P.bur_fill, protocol.GO))
        self.wait_ready(timeout=180, stop_event=stop_event)
        self.piston_used = 0.0

    def stop(self):
        """Emergency stop of whatever the instrument is doing."""
        for line in (protocol.trigger_cmd(P.bur_dis, protocol.STOP), protocol.trigger_cmd(P.mode, protocol.STOP)):
            try:
                self.t.send(line)
            except Exception:  # noqa: BLE001
                pass
        self.measuring = False

    def close(self):
        self.t.close()
