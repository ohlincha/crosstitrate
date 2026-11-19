"""
Titration logic and the instrument worker thread.

All instrument traffic runs on one worker thread (Controller), which executes
jobs one at a time: a whole titration, a single measurement, live monitoring,
a manual addition, filling ... The GUI talks to it through methods that post
jobs and receives progress through an event queue.
"""

import copy
import dataclasses
import json
import math
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field

from . import datafile, __version__
from .instrument import InstrumentError, NoResponse
from .transport import TransportError

COMPONENTS = ("H", "B", "C", "D")


@dataclass
class Burette:
    name: str = "Burette 1"
    device: str = ""                    # key of the connected instrument that drives it
    select: str = ""                    # e.g. "internal D0" on multi-burette models
    concentrations: dict = field(default_factory=lambda: {c: 0.0 for c in COMPONENTS})  # mM
    rate: float = 10.0                  # mL/min
    ratio: float = 1.0                  # volume factor when dispensing together with others

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})


@dataclass
class Sample:
    name: str = ""
    v0_ml: float = 50.0
    temperature_c: float = 25.0
    concentrations: dict = field(default_factory=lambda: {c: 0.0 for c in COMPONENTS})  # mM
    comment: str = ""


@dataclass
class Method:
    control: str = "constant"           # "constant" (fixed equilibration time) or "drift"
    eq_time_min: float = 2.0            # constant: equilibration time per point
    meas_interval_min: float = 0.5      # drift: time between readings
    drift_points: int = 3               # drift: compare reading with the one N readings earlier
    max_drift_mv: float = 0.2           # drift: accept point when change < this
    max_readings: int = 20              # drift: accept after this many readings anyway
    volume_step_ml: float = 0.1
    adaptive: bool = False
    halve_if_de_gt: float = 10.0        # mV
    double_if_de_lt: float = 2.0        # mV
    min_step_ml: float = 0.01
    max_step_ml: float = 1.0
    stop_volume_ml: float = 10.0
    stop_emf_min: float = None
    stop_emf_max: float = None
    quantity: str = "U"
    live_interval_s: float = 2.0        # live display during equilibration
    fill_at_end: bool = True

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})

    def validate(self):
        errs = []
        if self.control not in ("constant", "drift"):
            errs.append("unknown control mode")
        if self.volume_step_ml <= 0:
            errs.append("volume step must be > 0")
        if self.stop_volume_ml <= 0:
            errs.append("stop volume must be > 0")
        if self.control == "constant" and self.eq_time_min < 0:
            errs.append("equilibration time must be >= 0")
        if self.control == "drift":
            if self.meas_interval_min <= 0:
                errs.append("measurement interval must be > 0")
            if self.drift_points < 1:
                errs.append("drift points must be >= 1")
            if self.max_readings < self.drift_points + 1:
                errs.append("max readings must exceed drift points")
        if self.adaptive and not (0 < self.min_step_ml <= self.max_step_ml):
            errs.append("min/max step must satisfy 0 < min <= max")
        if (self.stop_emf_min is not None and self.stop_emf_max is not None
                and self.stop_emf_min >= self.stop_emf_max):
            errs.append("stop EMF min must be below max")
        return errs


@dataclass
class Reading:
    t: float
    emf: float
    v_total: float
    v_segment: float
    kind: str
    segment: int
    temperature: float = None
    drift: float = None


class Stopped(Exception):
    pass


class Interrupted(Exception):
    """A waiting period was cut short (e.g. by a manual addition)."""


# Method fields that may be changed while a titration is running
LIVE_FIELDS = ("control", "eq_time_min", "meas_interval_min", "drift_points", "max_drift_mv", "max_readings",
               "volume_step_ml", "adaptive", "halve_if_de_gt", "double_if_de_lt", "min_step_ml", "max_step_ml",
               "stop_volume_ml", "stop_emf_min", "stop_emf_max", "fill_at_end")


def next_step(step, de, m):
    """Adaptive step size: halve if the EMF changed a lot, double if little."""
    if not m.adaptive or de is None:
        return step
    if abs(de) >= m.halve_if_de_gt:
        new = step / 2
        return step if new < m.min_step_ml else new
    if abs(de) <= m.double_if_de_lt:
        new = step * 2
        return step if new > m.max_step_ml else new
    return step


class Controller:
    """Owns the instruments and the worker thread."""

    def __init__(self, clock):
        self.clock = clock
        self.instruments = {}           # key -> Titrino
        self.events = queue.Queue()
        self.jobs = queue.Queue()
        self.stop_event = threading.Event()     # stop current job
        self.pause_event = threading.Event()
        self.interrupt_event = threading.Event()  # cut the current wait short (manual addition)
        self.busy = False
        self.job_name = ""
        self.requests = queue.Queue()   # burette changes etc. for the running titration
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    # ------------------------------------------------------------ plumbing
    def emit(self, kind, *args):
        self.events.put((kind,) + args)

    def submit(self, name, fn, *args):
        if self.busy:
            self.emit("error", f"Busy with '{self.job_name}'. Stop it first.")
            return False
        self.busy = True
        self.job_name = name
        self.stop_event.clear()
        self.pause_event.clear()
        self.interrupt_event.clear()
        while not self.requests.empty():   # drop requests meant for an earlier job
            self.requests.get_nowait()
        self.jobs.put((name, fn, args))
        return True

    def stop(self):
        self.stop_event.set()
        self.pause_event.clear()

    def _worker(self):
        while True:
            name, fn, args = self.jobs.get()
            self.emit("busy", name)
            try:
                fn(*args)
            except Stopped:
                self.emit("status", f"{name}: stopped.")
            except (InstrumentError, TransportError) as e:
                self.emit("error", str(e))
                self._emergency_stop()
            except Exception as e:  # noqa: BLE001
                self.emit("error", f"Unexpected error in {name}: {e}\n{traceback.format_exc()}")
                self._emergency_stop()
            finally:
                self.busy = False
                self.job_name = ""
                self.emit("idle")

    def _emergency_stop(self):
        for ins in self.instruments.values():
            try:
                ins.stop()
            except Exception:  # noqa: BLE001
                pass

    def check_stop(self):
        if self.stop_event.is_set():
            raise Stopped()

    def wait(self, seconds, label, on_tick=None):
        """Wait `seconds` (simulation-aware), honouring pause and stop. Emits countdown."""
        end = self.clock.now() + seconds
        last_tick = -math.inf
        while True:
            self.check_stop()
            if self.interrupt_event.is_set():
                raise Interrupted()
            if self.pause_event.is_set():
                t_pause = self.clock.now()
                self.emit("countdown", None, "paused")
                while self.pause_event.is_set():
                    self.check_stop()
                    if self.interrupt_event.is_set():
                        raise Interrupted()
                    time.sleep(0.1)
                end += self.clock.now() - t_pause
            remaining = end - self.clock.now()
            if remaining <= 0:
                self.emit("countdown", 0, label)
                return
            self.emit("countdown", remaining, label)
            if on_tick and self.clock.now() - last_tick >= on_tick[0]:
                last_tick = self.clock.now()
                on_tick[1]()
            self.clock.sleep(min(remaining, 0.25), self.stop_event)

    # ------------------------------------------------------------ manual jobs
    def measure_once(self, key, quantity="U", settle_s=5.0):
        def job():
            ins = self.instruments[key]
            ins.start_measuring(quantity)
            self.wait(settle_s, "settling")
            v = ins.read_measurement()
            temp = ins.read_temperature()
            ins.stop_measuring()
            self.emit("measured", key, v, temp)
        return self.submit("measure", job)

    def monitor(self, key, quantity="U", interval_s=1.0, path=None, meta=None, v_total=0.0):
        """Follow the EMF continuously. The display updates about once a second; if `path`
        is given, a reading (with temperature) is written every `interval_s` seconds.
        Notes sent with request_note() are written as event rows."""
        interval_s = max(0.5, float(interval_s))

        def job():
            ins = self.instruments[key]
            f = None
            if path:
                info = {"type": "EMF monitor", "software": f"Titrate {__version__}",
                        "created": datafile.iso(time.time()),
                        "instrument": f"{ins.profile.title} ({key})", "interval_s": interval_s}
                info.update(meta or {})
                f = datafile.DataFile(path, info)
            ins.start_measuring(quantity)
            t0 = self.clock.now()
            wall0 = time.time()
            next_rec = t0
            n = 0

            def row(kind, emf=None, temp=None, note=""):
                elapsed = self.clock.now() - t0
                f.write(dict(timestamp=datafile.iso(wall0 + elapsed), elapsed_s=round(elapsed, 1),
                             segment=0, kind=kind, v_total_ml=round(v_total, 4), emf_mv=emf,
                             temperature_c=temp, note=note))
            if f:
                self.emit("monitor_started", path)
            try:
                while True:
                    self.check_stop()
                    while True:
                        try:
                            req = self.requests.get_nowait()
                        except queue.Empty:
                            break
                        if req[0] == "note" and f:
                            row("event", note=req[1])
                            self.emit("note", self.clock.now() - t0, req[1])
                    try:
                        v = ins.read_measurement(retries=1)
                    except InstrumentError as e:
                        if "NV" not in str(e) and "valid" not in str(e):
                            raise
                        v = None
                    if v is not None:
                        self.emit("live", key, v)
                        now = self.clock.now()
                        if f and now >= next_rec:
                            temp = ins.read_temperature()
                            row("monitor", v, temp)
                            n += 1
                            self.emit("monitor_reading", wall0 + (now - t0), now - t0, v, temp, v_total)
                            next_rec += interval_s
                            if next_rec < now:
                                next_rec = now + interval_s
                    wait = min(1.0, interval_s)
                    if f:
                        wait = max(0.05, min(wait, next_rec - self.clock.now()))
                    self.clock.sleep(wait, self.stop_event)
            finally:
                try:
                    ins.stop_measuring()
                finally:
                    if f:
                        f.close()
                        self.emit("monitor_finished", path, n)
        return self.submit("monitor", job)

    def dispense(self, key, select, volume, rate):
        def job():
            ins = self.instruments[key]
            ins.burette_select = select or None
            done = ins.dispense(volume, rate, stop_event=self.stop_event)
            self.emit("dispensed", key, done)
        return self.submit("dispense", job)

    def fill(self, key, select=""):
        def job():
            ins = self.instruments[key]
            ins.burette_select = select or None
            ins.fill(stop_event=self.stop_event)
            self.emit("status", f"{key}: burette filled.")
        return self.submit("fill", job)

    def run_titration(self, sample, method, burettes, active, path, meas_key, extra_meta=None,
                      start_at=None):
        """Start a titration. `active` = list of burette indices dispensing together."""
        job = TitrationJob(self, copy.deepcopy(sample), copy.deepcopy(method), copy.deepcopy(burettes),
                           list(active), path, meas_key, extra_meta or {}, start_at)
        self.current = job
        return self.submit("titration", job.run)

    def request_burette_change(self, burettes, active, new_method_fields=None):
        self.requests.put(("burette", copy.deepcopy(burettes), list(active), dict(new_method_fields or {})))

    def request_method_change(self, fields):
        """Change settings of the running titration (applied before the next addition)."""
        self.requests.put(("method", dict(fields)))

    def request_note(self, text):
        """Write a note into the data file of the running titration or EMF monitor."""
        self.requests.put(("note", str(text)))

    def request_addition(self, burette, volume):
        """Manual addition during a titration: done at once, recorded, equilibration restarts."""
        self.requests.put(("add", copy.deepcopy(burette), float(volume)))
        self.interrupt_event.set()


class TitrationJob:
    def __init__(self, ctl, sample, method, burettes, active, path, meas_key, extra_meta, start_at):
        self.ctl = ctl
        self.sample = sample
        self.m = method
        self.burettes = burettes
        self.active = active
        self.path = path
        self.meas_key = meas_key
        self.extra_meta = extra_meta
        self.start_at = start_at
        self.readings = []
        self.accepted = []
        self.v_total = 0.0
        self.v_segment = 0.0
        self.v_per_burette = [0.0] * len(burettes)
        self.segment = 1
        self.step = method.volume_step_ml
        self.t0 = None
        self.file = None

    # ------------------------------------------------------------ helpers
    def emit(self, *a):
        self.ctl.emit(*a)

    @property
    def meas(self):
        return self.ctl.instruments[self.meas_key]

    def active_names(self):
        return "+".join(self.burettes[i].name for i in self.active)

    def record(self, emf, kind, temperature=None, drift=None, note=""):
        now = time.time()
        r = Reading(now, emf, self.v_total, self.v_segment, kind, self.segment, temperature, drift)
        if emf is not None:
            self.readings.append(r)
            if kind == "eq":
                self.accepted.append(r)
        self.file.write(dict(timestamp=datafile.iso(now), elapsed_s=round(now - self.t0, 1),
                             segment=self.segment, kind=kind, burettes=self.active_names(),
                             v_total_ml=round(self.v_total, 4), v_segment_ml=round(self.v_segment, 4),
                             emf_mv=emf, temperature_c=temperature, drift_mv=drift, note=note))
        if emf is not None:
            self.emit("reading", r)
        return r

    def live(self):
        try:
            v = self.meas.read_measurement(retries=1)
            self.emit("live", self.meas_key, v)
        except InstrumentError:
            pass

    def meta(self):
        return {
            "software": f"titrate {__version__}",
            "start_time": datafile.iso(time.time()),
            "sample": dataclasses.asdict(self.sample),
            "method": self.m.to_dict(),
            "burettes": [b.to_dict() for b in self.burettes],
            "active_burettes": [self.burettes[i].to_dict() for i in self.active],
            "measuring_instrument": self.meas_key,
            "instruments": {k: dict(model=ins.profile.title, prog=ins.prog, devname=ins.devname,
                                    exv_ml=ins.exv) for k, ins in self.ctl.instruments.items()},
            **self.extra_meta,
        }

    # ------------------------------------------------------------ steps
    def measure_point(self):
        """Equilibrate and take the accepted reading for the current volume."""
        m = self.m
        ins = self.meas
        ins.start_measuring(m.quantity)
        temp = None
        if m.control == "constant":
            self.ctl.wait(m.eq_time_min * 60, "equilibrating", on_tick=(m.live_interval_s, self.live))
            emf = ins.read_measurement()
            temp = ins.read_temperature()
            r = self.record(emf, "eq", temp)
        else:
            history = []
            r = None
            for i in range(m.max_readings):
                self.ctl.wait(m.meas_interval_min * 60, f"reading {i + 1}/{m.max_readings}",
                              on_tick=(m.live_interval_s, self.live))
                emf = ins.read_measurement()
                temp = ins.read_temperature()
                history.append(emf)
                ref = history[-1 - m.drift_points] if len(history) > m.drift_points else history[0]
                drift = abs(emf - ref)
                stable = len(history) > m.drift_points and drift < m.max_drift_mv
                last = i == m.max_readings - 1
                self.emit("drift", drift, len(history))
                r = self.record(emf, "eq" if (stable or last) else "drift", temp, round(drift, 3),
                                note="" if stable or not last else "max readings reached, drift not satisfied")
                if stable or last:
                    break
        return r

    def check_limits(self, emf):
        m = self.m
        if self.ctl.stop_event.is_set():
            return "stopped by user"
        if m.stop_emf_min is not None and emf < m.stop_emf_min:
            return f"EMF {emf:.1f} mV below stop limit {m.stop_emf_min} mV"
        if m.stop_emf_max is not None and emf > m.stop_emf_max:
            return f"EMF {emf:.1f} mV above stop limit {m.stop_emf_max} mV"
        if self.v_segment >= m.stop_volume_ml - 1e-9:
            return f"stop volume {m.stop_volume_ml} mL reached"
        return None

    def add_volume(self, step):
        for i in self.active:
            b = self.burettes[i]
            ins = self.ctl.instruments[b.device]
            ins.burette_select = b.select or None
            v = step * (b.ratio if len(self.active) > 1 else 1.0)
            self.emit("status", f"Adding {v:.4f} mL from {b.name}…")
            done = ins.dispense(v, b.rate, stop_event=self.ctl.stop_event)
            self.ctl.check_stop()
            self.v_total += done
            self.v_segment += done
            self.v_per_burette[i] += done
        self.emit("volume", self.v_total, self.v_segment)

    def handle_requests(self):
        while True:
            try:
                req = self.ctl.requests.get_nowait()
            except queue.Empty:
                return
            if req[0] == "note":
                self.record(None, "event", note=req[1])
                self.emit("note", None, req[1])
            elif req[0] == "method":
                fields = {k: v for k, v in req[1].items() if k in LIVE_FIELDS}
                for k, v in fields.items():
                    setattr(self.m, k, v)
                if "volume_step_ml" in fields:
                    self.step = fields["volume_step_ml"]
                    self.emit("step", self.step)
                self.record(None, "event", note="settings changed: " + json.dumps(fields, ensure_ascii=False))
                self.emit("status", "Settings applied: " + ", ".join(f"{k}={v}" for k, v in fields.items()))
            elif req[0] == "add":
                _, b, volume = req
                ins = self.ctl.instruments[b.device]
                ins.burette_select = b.select or None
                self.emit("status", f"Manual addition: {volume:.4f} mL from {b.name}…")
                done = ins.dispense(volume, b.rate, stop_event=self.ctl.stop_event)
                self.ctl.check_stop()
                self.v_total += done
                self.v_segment += done
                idx = next((i for i, x in enumerate(self.burettes) if x.name == b.name), None)
                if idx is not None:
                    self.v_per_burette[idx] += done
                self.record(None, "event", note=f"manual addition: {done:.4f} mL from {b.name}")
                self.emit("volume", self.v_total, self.v_segment)
                self.emit("status", f"Added {done:.4f} mL from {b.name}; equilibrating again.")
            elif req[0] == "burette":
                _, burettes, active, fields = req
                self.burettes, self.active = burettes, active
                if len(self.v_per_burette) < len(burettes):
                    self.v_per_burette += [0.0] * (len(burettes) - len(self.v_per_burette))
                for k, v in fields.items():
                    setattr(self.m, k, v)
                self.segment += 1
                self.v_segment = 0.0
                self.step = self.m.volume_step_ml
                info = {"active_burettes": [burettes[i].to_dict() for i in active],
                        "method_changes": fields}
                self.record(None, "event", note="burette change: " + json.dumps(info, ensure_ascii=False))
                self.emit("status", f"Burette changed to {self.active_names()} (segment {self.segment}).")
                self.emit("segment", self.segment)

    # ------------------------------------------------------------ main
    def run(self):
        ctl = self.ctl
        if self.start_at:
            while time.time() < self.start_at:
                ctl.check_stop()
                ctl.emit("countdown", self.start_at - time.time(), "delayed start")
                time.sleep(0.25)
        self.t0 = time.time()
        self.file = datafile.DataFile(self.path, self.meta())
        self.emit("started", self.path)
        reason = "finished"
        try:
            prev = None
            while True:
                self.handle_requests()
                self.ctl.interrupt_event.clear()
                self.emit("status", f"Measuring at {self.v_segment:.4f} mL (segment {self.segment})…")
                try:
                    r = self.measure_point()
                except Interrupted:
                    self.ctl.interrupt_event.clear()
                    continue   # handle the request (e.g. manual addition), then measure again
                de = None if prev is None or prev.segment != r.segment else r.emf - prev.emf
                prev = r
                why = self.check_limits(r.emf)
                if why:
                    reason = why
                    break
                self.handle_requests()
                if self.segment != r.segment:
                    prev = None
                    continue  # new burette: measure the start point of the new segment first
                if de is not None:
                    self.step = next_step(self.step, de, self.m)
                step = min(self.step, self.m.stop_volume_ml - self.v_segment)
                self.emit("step", self.step)
                self.add_volume(step)
        except Stopped:
            reason = "stopped by user"
        finally:
            try:
                self.meas.stop_measuring()
            except Exception:  # noqa: BLE001
                pass
            self.file.comment("end", {"time": datafile.iso(time.time()), "reason": reason,
                                      "v_total_ml": round(self.v_total, 4)})
            self.file.close()
        if self.m.fill_at_end and reason != "stopped by user":
            for i in sorted({i for i in self.active}):
                b = self.burettes[i]
                ins = ctl.instruments[b.device]
                ins.burette_select = b.select or None
                self.emit("status", f"Filling {b.name}…")
                try:
                    ins.fill(stop_event=ctl.stop_event)
                except (InstrumentError, NoResponse) as e:
                    self.emit("error", f"Filling {b.name} failed: {e}")
        self.emit("finished", reason, self.path)
