"""
A simulated Metrohm Titrino for testing and demonstration without hardware.

It understands the same remote-control language as the real instruments
(including abbreviated object names such as ``&A.B.M.S``), answers $Q and $D,
reports error codes for invalid commands, and simulates a glass electrode in a
titration vessel: any mixture of strong and weak (polyprotic) acids and bases,
titrated with a strong base or strong acid, with the pH from the charge
balance, a first-order electrode response (drift) and measurement noise.
"""

import math
import random
import re
import threading
import time

from . import protocol


class Clock:
    """Time source with an adjustable speed factor (simulation can run faster)."""

    def __init__(self, speed=1.0):
        self.speed = speed
        self._t0 = time.monotonic()
        self._sim0 = 0.0

    def now(self):
        return self._sim0 + (time.monotonic() - self._t0) * self.speed

    def set_speed(self, speed):
        self._sim0 = self.now()
        self._t0 = time.monotonic()
        self.speed = speed

    def sleep(self, seconds, stop_event=None):
        """Sleep `seconds` of simulated time; returns early if stop_event is set."""
        end = self.now() + seconds
        while True:
            remaining = end - self.now()
            if remaining <= 0:
                return True
            wall = min(remaining / self.speed, 0.1)
            if stop_event is not None:
                if stop_event.wait(wall):
                    return False
            else:
                time.sleep(wall)


REAL_CLOCK = Clock(1.0)


# Object tree: name -> children (ordered, for abbreviation resolution).
def _tree(multi_burette):
    bur = {}
    if multi_burette:
        bur["Select"] = None
    bur.update({
        "Rates": {"Forward": {"Select": None, "Digital": None}, "Reverse": {"Select": None, "Digital": None}},
        "Fill": None,
        "ModeDis": {"Select": None, "V": None, "Time": None, "VStop": None, "AutoFill": None},
    })
    if multi_burette:
        bur["Empty"] = None
        bur["Prep"] = None
    return {
        "Mode": {"QuickMeas": None, "Select": None, "DETQuantity": None, "METQuantity": None,
                 "SETQuantity": None, "MEASQuantity": None, "STATQuantity": None, "DOCQuantity": None,
                 "Name": None, "Parameter": {}},
        "Def": {},
        "Config": {"PeriphUnit": {"CharSet": None, "Balance": None, "Plot": None},
                   "Aux": {"Language": None, "Set": {"Date": None, "Time": None}, "RunNo": None,
                           "AutoStart": None, "StartDelay": None, "DevName": None, "Prog": None},
                   "RSSet": {"Baud": None, "DataBit": None, "StopBit": None, "Parity": None, "Handsh": None},
                   "ComVar": {}},
        "SmplData": {},
        "Info": {"Report": {}, "CalibrationData": {}, "Checksums": {}, "DetermData": {}, "TitrResults": {},
                 "StatisticsVal": {}, "SiloCalc": {},
                 "ActualInfo": {"Inputs": {}, "Outputs": {},
                                "Assembly": {"CyclNo": None, "Counter": {"V": None, "Clear": None}, "Meas": None},
                                "Titrator": {"CyclNo": None, "V": None, "Meas": None, "dVdt": None,
                                             "dMeasdt": None, "dMeasdV": None, "ERC": None, "T": None},
                                "MeasPt": {}, "EP": {}, "Display": {"L1": None, "L2": None}},
                 "Assembly": {"CycleTime": None, "ExV": None}},
        "Assembly": {"Bur": bur, "Meas": {"Status": None, "MeasInput": None, "Ipol": None, "Upol": None},
                     "Outputs": {}, "Stirrer": None},
        "Setup": {"Keycode": None, "Tree": {"Short": None, "ChangedOnly": None}, "Trace": None,
                  "Lock": {"Keyboard": None, "Config": None, "Parameter": None, "SmplData": None,
                           "UserMeth": {}, "Display": None},
                  "Mode": {}, "SendMeas": {}, "AutoInfo": {}, "Graphics": {}},
    }


KW = 1e-14   # ion product of water at 25 °C


class Species:
    """A weak acid/base system in the sample, e.g. acetic acid, phosphate, carbonate, ammonia.

    conc:    total concentration in the sample (mol/L).
    pkas:    pKa values of the fully protonated form, in increasing order (one per proton).
    charge:  charge of the fully protonated form (0 for H3PO4, H2CO3, HAc; +1 for NH4+).
    protons: number of acidic protons on the form that was put into the sample
             (HAc: 1 of 1; Na2CO3: 0 of 2; NH3: 0 of 1). The counter-ions (Na+, Cl-)
             needed to make that form neutral are included automatically.
    """

    def __init__(self, name, conc, pkas, charge=0, protons=None):
        self.name = name
        self.conc = float(conc)
        self.pkas = sorted(float(p) for p in pkas)
        self.charge = charge
        self.protons = len(self.pkas) if protons is None else protons

    def mean_charge(self, h):
        """Mean charge per molecule at [H+] = h (fractions of each protonation state)."""
        n = len(self.pkas)
        terms, prod = [], 1.0
        for i in range(n + 1):           # i protons lost
            terms.append(h ** (n - i) * prod)
            if i < n:
                prod *= 10 ** -self.pkas[i]
        total = sum(terms)
        return sum(t * (self.charge - i) for i, t in enumerate(terms)) / total

    def counter_ion_charge(self):
        """Charge of the counter-ions per molecule (+ = cations such as Na+)."""
        return -(self.charge - (len(self.pkas) - self.protons))

    def to_dict(self):
        return dict(name=self.name, conc=self.conc, pkas=self.pkas, charge=self.charge, protons=self.protons)


class Chemistry:
    """Sample in the titration vessel, titrated with a strong base or strong acid.

    Concentrations in mol/L, volumes in mL. The pH follows from the charge balance
    (activities ignored), solved by bisection on log[H+]; the electrode EMF is
    E = e0 - slope * pH with the Nernst slope at the sample temperature.

    The old signature still works: Chemistry(strong_acid=0.01, weak_acid=0.005, pka=4.76)
    is HCl + acetic-type acid. Pass `species` (list of Species) for any other sample.
    """

    def __init__(self, v0=50.0, strong_acid=0.01, weak_acid=0.005, pka=4.76, e0=400.0, slope=59.16,
                 temp=25.0, strong_base=0.0, species=None):
        self.v0 = v0
        self.strong_acid = strong_acid
        self.strong_base = strong_base
        if species is None:
            species = [Species("weak acid", weak_acid, [pka])] if weak_acid else []
        self.species = list(species)
        self.weak_acid = sum(s.conc for s in self.species)
        self.e0 = e0
        self.slope = slope * (273.15 + temp) / 298.15
        self.base_added_mol = 0.0   # mmol actually
        self.acid_added_mol = 0.0
        self.v_added = 0.0

    def add(self, v_ml, conc_base):
        """Add v_ml of titrant; conc_base > 0 for base, < 0 for acid (mol/L)."""
        self.v_added += v_ml
        if conc_base >= 0:
            self.base_added_mol += v_ml * conc_base
        else:
            self.acid_added_mol += v_ml * -conc_base

    def charge_balance(self, h):
        """Net positive charge (mol/L) at [H+] = h; zero at the real pH, increasing with h."""
        v = self.v0 + self.v_added
        dil = self.v0 / v
        q = h - KW / h
        q += (self.strong_base * self.v0 + self.base_added_mol) / v      # Na+
        q -= (self.strong_acid * self.v0 + self.acid_added_mol) / v      # Cl-
        for s in self.species:
            c = s.conc * dil
            q += c * (s.mean_charge(h) + s.counter_ion_charge())
        return q

    def ph(self):
        lo, hi = -16.0, 1.5  # log10 [H+]
        for _ in range(80):
            mid = (lo + hi) / 2
            if self.charge_balance(10 ** mid) > 0:
                hi = mid
            else:
                lo = mid
        return -(lo + hi) / 2

    def emf(self):
        return self.e0 - self.slope * self.ph()

    def describe(self):
        parts = []
        if self.strong_acid:
            parts.append(f"strong acid {self.strong_acid * 1000:g} mM")
        if self.strong_base:
            parts.append(f"strong base {self.strong_base * 1000:g} mM")
        for s in self.species:
            parts.append(f"{s.name} {s.conc * 1000:g} mM (pKa {', '.join(f'{p:g}' for p in s.pkas)})")
        return ", ".join(parts) or "water"


# Demo samples for the simulator. Literature pKa values at 25 °C. Concentrations in mM.
# kind: "strong acid", "strong base" or "weak" (Species with pkas/charge/protons).
# titrant: what the sample is titrated with ("NaOH" = strong base, "HCl" = strong acid).
PRESETS = {
    "HCl": dict(titrant="NaOH", parts=[
        dict(name="HCl", kind="strong acid", conc_mm=10.0)]),
    "Acetic acid": dict(titrant="NaOH", parts=[
        dict(name="acetic acid", kind="weak", conc_mm=10.0, pkas=[4.76], charge=0, protons=1)]),
    "HCl + acetic acid": dict(titrant="NaOH", parts=[
        dict(name="HCl", kind="strong acid", conc_mm=5.0),
        dict(name="acetic acid", kind="weak", conc_mm=5.0, pkas=[4.76], charge=0, protons=1)]),
    "Phosphoric acid": dict(titrant="NaOH", parts=[
        dict(name="phosphoric acid", kind="weak", conc_mm=5.0, pkas=[2.15, 7.20, 12.35], charge=0, protons=3)]),
    "NaOH (titrate with HCl)": dict(titrant="HCl", parts=[
        dict(name="NaOH", kind="strong base", conc_mm=10.0)]),
    "Sodium carbonate (titrate with HCl)": dict(titrant="HCl", parts=[
        dict(name="sodium carbonate", kind="weak", conc_mm=5.0, pkas=[6.35, 10.33], charge=0, protons=0)]),
    "Ammonia (titrate with HCl)": dict(titrant="HCl", parts=[
        dict(name="ammonia", kind="weak", conc_mm=10.0, pkas=[9.25], charge=1, protons=0)]),
}
DEFAULT_PRESET = "HCl"


def preset_parts(name):
    """A copy of the parts of a preset (to edit concentrations or pKa values)."""
    import copy
    return copy.deepcopy(PRESETS[name]["parts"])


def chemistry_from_parts(parts, v0=50.0, temp=25.0, e0=400.0, slope=59.16):
    """Build a Chemistry from preset-style parts (concentrations in mM)."""
    strong_acid = sum(p["conc_mm"] for p in parts if p["kind"] == "strong acid") / 1000
    strong_base = sum(p["conc_mm"] for p in parts if p["kind"] == "strong base") / 1000
    species = [Species(p["name"], p["conc_mm"] / 1000, p["pkas"], p.get("charge", 0), p.get("protons"))
               for p in parts if p["kind"] == "weak"]
    return Chemistry(v0=v0, strong_acid=strong_acid, strong_base=strong_base, species=species,
                     temp=temp, e0=e0, slope=slope)


def chemistry_from_preset(name, v0=50.0, temp=25.0, parts=None):
    return chemistry_from_parts(parts if parts is not None else PRESETS[name]["parts"], v0, temp)


class SimulatedTitrino:
    """Simulated instrument. Feed it bytes with receive(); it returns reply bytes."""

    def __init__(self, model="718", clock=None, chemistry=None, titrant_conc=0.1, exv=20.0,
                 multi_burette=None, rs_control=True, noise=0.05, tau=15.0, seed=None):
        self.model = model
        self.clock = clock or REAL_CLOCK
        self.chem = chemistry or Chemistry()
        self.titrant_conc = titrant_conc      # mol/L in the burette (negative for acid)
        self.rs_control = rs_control
        self.noise = noise
        self.tau = tau                        # electrode response time constant (s)
        self.rng = random.Random(seed)
        if multi_burette is None:
            multi_burette = model in ("751", "785", "798", "799")
        self.tree = _tree(multi_burette)
        self.lock = threading.Lock()
        self.inbuf = b""
        self.out = b""
        self.values = {
            "Mode.Select": "SET", "Mode.MEASQuantity": "pH", "Mode.SETQuantity": "pH",
            "Mode.STATQuantity": "pH", "Mode.DOCQuantity": "pH", "Mode.Name": "********",
            "Config.Aux.Language": "english", "Config.Aux.DevName": "SIM" + model,
            "Config.Aux.Prog": f"{model}.0012", "Config.Aux.RunNo": "0",
            "Config.RSSet.Baud": "9600", "Config.RSSet.DataBit": "8", "Config.RSSet.StopBit": "1",
            "Config.RSSet.Parity": "none", "Config.RSSet.Handsh": "HWs",
            "Info.Assembly.ExV": f"{exv:.0f}", "Info.Assembly.CycleTime": "0.1",
            "Assembly.Bur.ModeDis.Select": "volume", "Assembly.Bur.ModeDis.V": "0.1",
            "Assembly.Bur.ModeDis.Time": "1", "Assembly.Bur.ModeDis.VStop": "OFF",
            "Assembly.Bur.ModeDis.AutoFill": "OFF",
            "Assembly.Bur.Rates.Forward.Select": "digital", "Assembly.Bur.Rates.Forward.Digital": "max.",
            "Assembly.Bur.Rates.Reverse.Select": "digital", "Assembly.Bur.Rates.Reverse.Digital": "max.",
            "Assembly.Bur.Select": "internal D0",
            "Assembly.Meas.Status": "OFF", "Assembly.Meas.MeasInput": "1",
            "Setup.Lock.Keyboard": "OFF",
        }
        self.exv = exv
        self.piston = 0.0          # volume dispensed since last fill (mL)
        self.counter = 0.0         # volume counter
        self.state = "idle"        # idle | meas | dispense | fill
        self.state_end = 0.0
        self.meas_start = 0.0
        self.pending_v = 0.0
        self.last_error = None
        self.electrode = self.chem.emf()
        self.t_last = self.clock.now()
        self.temp = 25.0

    # ---------------------------------------------------------------- I/O
    def receive(self, data):
        with self.lock:
            if not self.rs_control:
                return b""   # "RS control: OFF" - commands are ignored
            self.inbuf += data
            out = b""
            while b"\n" in self.inbuf:
                line, self.inbuf = self.inbuf.split(b"\n", 1)
                text = line.decode("ascii", "replace").strip("\r ")
                if text:
                    for cmd in text.split(";"):
                        out += self._command(cmd.strip())
            return out

    def poll_output(self):
        return b""

    # ---------------------------------------------------------------- model
    def _update(self):
        now = self.clock.now()
        dt = max(0.0, now - self.t_last)
        self.t_last = now
        if self.state == "dispense":
            rate = self._rate_ml_s()
            if now >= self.state_end:
                remaining = self.pending_v
                self.state = "idle"
            else:
                remaining = min(self.pending_v, rate * dt)
            if remaining > 0:
                self.chem.add(remaining, self.titrant_conc)
                self.piston += remaining
                self.counter += remaining
                self.pending_v -= remaining
            if self.state == "idle" and self.values["Assembly.Bur.ModeDis.AutoFill"].upper() == "ON":
                self._start_fill()
        elif self.state == "fill" and now >= self.state_end:
            self.state = "idle"
            self.piston = 0.0
        # electrode relaxes towards equilibrium potential
        target = self.chem.emf()
        if dt > 0:
            self.electrode = target + (self.electrode - target) * math.exp(-dt / self.tau)

    def _rate_ml_s(self):
        r = self.values["Assembly.Bur.Rates.Forward.Digital"]
        try:
            return float(r) / 60.0
        except ValueError:
            return self.exv / 20.0   # "max." ~ full exchange unit in 20 s

    def _start_fill(self):
        self.state = "fill"
        self.state_end = self.clock.now() + 2.0 + 18.0 * self.piston / self.exv

    def emf_reading(self):
        return self.electrode + self.rng.gauss(0, self.noise)

    # ---------------------------------------------------------------- commands
    def _resolve(self, path):
        """Resolve an absolute, possibly abbreviated path. Returns canonical
        dotted name (without '&') or None (-> E28)."""
        if not path.startswith("&"):
            return None
        segs = [s for s in path[1:].split(".") if s]
        node, out = self.tree, []
        for seg in segs:
            if not isinstance(node, dict):
                return None
            match = next((k for k in node if k.lower().startswith(seg.lower())), None)
            if match is None:
                return None
            out.append(match)
            node = node[match]
        return ".".join(out)

    def _is_leaf(self, canon):
        node = self.tree
        for seg in canon.split("."):
            node = node[seg]
        return node is None

    def _command(self, cmd):
        self._update()
        m = re.fullmatch(r'(&[\w.]*)?\s*(?:"([^"]*)")?\s*(\$[A-Z](?:\.[A-Z](?:"\d+")?)?)?', cmd)
        if not m or not (m.group(1) or m.group(3)):
            self.last_error = "E28"
            return b""
        path, value, trig = m.group(1), m.group(2), m.group(3)
        if trig == "$D":
            return self._status().encode() + protocol.BLOCK_END
        if trig == "$U":
            return b""
        canon = self._resolve(path) if path else None
        if path and canon is None:
            self.last_error = "E28"
            return b""
        self.last_error = None
        if value is not None:
            err = self._set(canon, value)
            if err:
                self.last_error = err
                return b""
        if trig == "$Q":
            return self._query(canon)
        if trig in ("$G", "$S", "$H", "$C"):
            err = self._trigger(canon, trig)
            if err:
                self.last_error = err
        elif trig:
            self.last_error = "E30"
        return b""

    def _set(self, canon, value):
        if not self._is_leaf(canon):
            return "E29"
        ro = ("Config.Aux.Prog", "Info.Assembly.ExV", "Info.ActualInfo")
        if canon.startswith(ro):
            return "E29"
        if self.state != "idle" and canon.startswith(("Mode.Select", "Assembly.Bur")):
            return "E31"
        v = value.strip()
        if canon == "Mode.Select" and v.upper() not in ("SET", "STAT", "DOS", "DOC", "MEAS", "CAL", "TIP",
                                                       "DET", "MET", "KFT"):
            return "E29"
        if canon.endswith("Quantity") and v not in ("pH", "U", "Ipol", "Upol", "T"):
            return "E29"
        if canon in ("Assembly.Bur.ModeDis.V", "Assembly.Bur.Rates.Forward.Digital",
                     "Assembly.Bur.Rates.Reverse.Digital"):
            if v.lower() not in ("max.", "max"):
                try:
                    x = float(v)
                except ValueError:
                    return "E29"
                if not re.fullmatch(r"-?\d+(\.\d{1,4})?", v) or sum(c.isdigit() for c in v) > 6:
                    return "E29"
                if canon.endswith("Digital") and not (0.01 <= x <= 150):
                    return "E29"
                if canon.endswith("ModeDis.V") and not (0.0001 <= x <= 9999):
                    return "E29"
        self.values[canon] = v
        return None

    def _query(self, canon):
        if canon is None or not self._is_leaf(canon):
            return b"" if canon else b""
        v = self._live_value(canon)
        return f'"{v}"'.encode() + protocol.BLOCK_END

    def _live_value(self, canon):
        if canon == "Info.ActualInfo.Titrator.Meas":
            measuring = self.state == "meas" and self.clock.now() - self.meas_start > 1.0
            if not measuring:
                return "NV"
            q = self.values["Mode.MEASQuantity"]
            if q == "U":
                return f"{self.emf_reading():.1f}"
            if q == "pH":
                return f"{(self.chem.e0 - self.emf_reading()) / self.chem.slope:.3f}"
            return "NV"
        if canon == "Info.ActualInfo.Titrator.T":
            return f"{self.temp:.1f}" if self.state == "meas" else "NV"
        if canon == "Info.ActualInfo.Titrator.dMeasdt":
            return "NV"
        if canon == "Info.ActualInfo.Assembly.Counter.V":
            return f"{self.counter:.4f}"
        if canon == "Info.ActualInfo.Display.L1":
            return {"idle": f"{self.values['Mode.Select']} {self.values['Mode.MEASQuantity']}",
                    "meas": "measuring", "dispense": "dispensing", "fill": "filling"}[self.state]
        if canon == "Info.ActualInfo.Display.L2":
            return f"{self.emf_reading():.1f} mV" if self.state == "meas" else f"{self.counter:.3f} ml"
        return self.values.get(canon, "")

    def _trigger(self, canon, trig):
        now = self.clock.now()
        if canon == "Mode":
            if trig == "$G":
                if self.state not in ("idle", "meas"):
                    return "E31"
                if self.values["Mode.Select"].upper() == "MEAS":
                    self.state = "meas"
                    self.meas_start = now
                    return None
                return "E30"   # other modes are not simulated
            if trig == "$S":
                if self.state == "meas":
                    self.state = "idle"
                elif self.state == "dispense":
                    self.state = "idle"
                    self.pending_v = 0.0
                return None
            return None
        if canon == "Assembly.Bur.ModeDis":
            if trig == "$G":
                if self.state != "idle":
                    return "E31"
                v = float(self.values["Assembly.Bur.ModeDis.V"])
                if self.piston + v > self.exv + 1e-9:
                    return "E30"   # not enough volume left in the piston
                self.state = "dispense"
                self.pending_v = v
                self.state_end = now + v / self._rate_ml_s()
                return None
            if trig == "$S" and self.state == "dispense":
                self.state = "idle"
                self.pending_v = 0.0
            return None
        if canon == "Assembly.Bur.Fill":
            if trig == "$G":
                if self.state not in ("idle",):
                    return "E31"
                self._start_fill()
            return None
        if canon == "Info.ActualInfo.Assembly.Counter.Clear" and trig == "$G":
            self.counter = 0.0
            return None
        if canon == "Mode.QuickMeas":
            return None
        return "E30"

    def _status(self):
        st = {
            "idle": f"$R.Mode.{self.values['Mode.Select'].upper()}.Inac",
            "meas": "$G.Mode.MEAS.Meas",
            "dispense": "$G.Assembly.Bur.ModeDis",
            "fill": "$G.Assembly.Bur.Fill",
        }[self.state]
        if self.last_error:
            st += ";" + self.last_error
        return st
