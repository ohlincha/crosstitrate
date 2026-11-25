"""
Evaluation of a titration curve (no GUI).

Equivalence points are the maxima of |dE/dV|:

1. Accepted points (kind "eq") are sorted by volume; repeated volumes are averaged.
2. The first derivative is taken between neighbouring points, d_i = ΔE/ΔV at the
   interval midpoint. It is signed so that the titration direction is positive
   (then noise in flat regions does not add up to false peaks).
3. Peaks are searched in a lightly smoothed copy (1-2-1 weights). A peak must be a
   local maximum inside the curve (not at its ends), reach at least `min_rel_height`
   of the highest peak, and stand out from the valleys on both sides by a factor
   `min_prominence`.
4. The peak position is refined on the unsmoothed derivative by a parabola through
   the highest midpoint and its two neighbours (works for uneven steps). This is V_eq.
5. The inflection point is also given as a cross-check: the zero crossing of the second
   derivative of the cubic through the two points on each side of the steepest interval.
   Both estimates agree closely for a symmetric jump; a difference larger than the step
   size points to an asymmetric (weak acid/base) or noisy jump.

From V_eq, the titrant concentration c_t (mol/L) and the sample volume V0 (mL),
assuming 1:1 stoichiometry per equivalence point (one proton per step):

    c from ΔV   = c_t * (V_eq,k - V_eq,k-1) / V0    (amount titrated between EP k-1 and EP k)
    c from V_eq = c_t * V_eq,k / V0                  (everything titrated up to EP k)
"""

import csv
import json
import os
from dataclasses import asdict, dataclass, field

STOICHIOMETRY = "1:1 per equivalence point (one proton per step)"


@dataclass
class EquivalencePoint:
    n: int                         # 1, 2, ...
    v_eq_ml: float                 # maximum of |dE/dV| (parabola)
    e_eq_mv: float                 # EMF (or pH) interpolated at v_eq_ml
    dedv_max: float                # |dE/dV| at the peak (mV/mL)
    v_inflection_ml: float = None  # zero of the second derivative (local cubic)
    dv_ml: float = None            # v_eq - previous v_eq (or v_eq for the first)
    c_step_mol_l: float = None     # c_t * dv / V0
    c_cum_mol_l: float = None      # c_t * v_eq / V0


@dataclass
class Evaluation:
    points: list = field(default_factory=list)
    titrant: str = ""              # e.g. "NaOH 0.1 mol/L (Burette 1)"
    titrant_conc_mol_l: float = None
    v0_ml: float = None
    n_points: int = 0              # accepted curve points used
    stoichiometry: str = STOICHIOMETRY
    notes: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        for p in d["points"]:
            for k, v in p.items():
                if isinstance(v, float):
                    p[k] = float(f"{v:.6g}")
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        pts = [EquivalencePoint(**p) for p in d.pop("points", [])]
        known = set(cls.__dataclass_fields__)
        return cls(points=pts, **{k: v for k, v in d.items() if k in known})

    def summary(self):
        """One line per equivalence point, for messages and the clipboard."""
        if not self.points:
            return "No equivalence point found."
        lines = []
        for p in self.points:
            s = f"EP{p.n}: V_eq = {p.v_eq_ml:.4f} mL, E = {p.e_eq_mv:.1f} mV"
            if p.c_step_mol_l is not None:
                s += f", c = {p.c_step_mol_l:.5f} mol/L (from ΔV), {p.c_cum_mol_l:.5f} mol/L (from V_eq)"
            lines.append(s)
        return "\n".join(lines)


def prepare(volumes, values):
    """Sort by volume and average repeated volumes. Returns (v, e) lists."""
    pts = sorted((float(v), float(e)) for v, e in zip(volumes, values) if v is not None and e is not None)
    v_out, e_out = [], []
    i = 0
    while i < len(pts):
        j = i
        while j + 1 < len(pts) and abs(pts[j + 1][0] - pts[i][0]) < 1e-9:
            j += 1
        group = pts[i:j + 1]
        v_out.append(group[0][0])
        e_out.append(sum(e for _, e in group) / len(group))
        i = j + 1
    return v_out, e_out


def _parabola_vertex(x, y):
    """Vertex x of the parabola through three points (x sorted); None if not a maximum."""
    (x0, x1, x2), (y0, y1, y2) = x, y
    d01, d12 = (y1 - y0) / (x1 - x0), (y2 - y1) / (x2 - x1)
    a = (d12 - d01) / (x2 - x0)
    if a >= 0:
        return None
    b = d01 - a * (x0 + x1)
    return -b / (2 * a)


def _cubic_inflection(x, y):
    """Inflection point (second derivative = 0) of the cubic through four points."""
    x0, x1, x2, x3 = x
    f01 = (y[1] - y[0]) / (x1 - x0)
    f12 = (y[2] - y[1]) / (x2 - x1)
    f23 = (y[3] - y[2]) / (x3 - x2)
    f012 = (f12 - f01) / (x2 - x0)
    f123 = (f23 - f12) / (x3 - x1)
    c3 = (f123 - f012) / (x3 - x0)
    if c3 == 0:
        return None
    return ((x0 + x1 + x2) - f012 / c3) / 3


def _interp(x, xs, ys):
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            t = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[0] if x < xs[0] else ys[-1]


def find_equivalence_points(volumes, values, min_rel_height=0.05, min_prominence=2.0, smooth=True):
    """Equivalence points of a curve E(V). Returns a list of dicts with v_eq_ml, e_eq_mv,
    dedv_max, v_inflection_ml (sorted by volume)."""
    v, e = prepare(volumes, values)
    if len(v) < 5:
        return []
    direction = 1.0 if e[-1] >= e[0] else -1.0
    m = [(v[i] + v[i + 1]) / 2 for i in range(len(v) - 1)]
    d = [direction * (e[i + 1] - e[i]) / (v[i + 1] - v[i]) for i in range(len(v) - 1)]
    n = len(d)
    if smooth and n >= 5:
        s = [d[0]] + [(d[i - 1] + 2 * d[i] + d[i + 1]) / 4 for i in range(1, n - 1)] + [d[-1]]
    else:
        s = list(d)
    cand = [k for k in range(1, n - 1) if s[k] > 0 and s[k] >= s[k - 1] and s[k] > s[k + 1]]
    if not cand:
        return []
    top = max(s[k] for k in cand)
    found = []
    for k in cand:
        if s[k] < min_rel_height * top:
            continue
        # valleys on both sides: lowest value until a higher point (or the end of the curve)
        left = right = s[k]
        i = k - 1
        while i >= 0 and s[i] <= s[k]:
            left = min(left, s[i])
            i -= 1
        i = k + 1
        while i < n and s[i] <= s[k]:
            right = min(right, s[i])
            i += 1
        valley = max(left, right, 0.0)
        if valley > 0 and s[k] < min_prominence * valley:
            continue
        # refine on the raw derivative
        j = max((i for i in (k - 1, k, k + 1) if 0 < i < n - 1), key=lambda i: d[i])
        x = _parabola_vertex(m[j - 1:j + 2], d[j - 1:j + 2])
        if x is None or not m[j - 1] <= x <= m[j + 1]:
            x = m[j]
        infl = _cubic_inflection(v[j - 1:j + 3], e[j - 1:j + 3]) if j + 2 < len(v) else None
        if infl is not None and not v[j - 1] <= infl <= v[j + 2]:
            infl = None
        found.append(dict(v_eq_ml=x, e_eq_mv=_interp(x, v, e), dedv_max=abs(d[j]), v_inflection_ml=infl))
    # two candidates in the same peak (flat top) -> keep the higher
    found.sort(key=lambda p: p["v_eq_ml"])
    out = []
    for p in found:
        if out and abs(p["v_eq_ml"] - out[-1]["v_eq_ml"]) < 1e-9:
            if p["dedv_max"] > out[-1]["dedv_max"]:
                out[-1] = p
            continue
        out.append(p)
    return out


def evaluate(volumes, values, titrant_conc_mol_l=None, v0_ml=None, titrant="", **kw):
    """Equivalence points plus sample concentrations (1:1 per equivalence point)."""
    ev = Evaluation(titrant=titrant, titrant_conc_mol_l=titrant_conc_mol_l, v0_ml=v0_ml,
                    n_points=len(prepare(volumes, values)[0]))
    prev = 0.0
    for i, p in enumerate(find_equivalence_points(volumes, values, **kw), 1):
        ep = EquivalencePoint(n=i, dv_ml=p["v_eq_ml"] - prev, **p)
        if titrant_conc_mol_l and v0_ml:
            ep.c_step_mol_l = titrant_conc_mol_l * ep.dv_ml / v0_ml
            ep.c_cum_mol_l = titrant_conc_mol_l * ep.v_eq_ml / v0_ml
        prev = p["v_eq_ml"]
        ev.points.append(ep)
    if ev.n_points < 5:
        ev.notes.append("Too few points to evaluate.")
    elif not ev.points:
        ev.notes.append("No equivalence point found (no clear maximum of dE/dV inside the curve).")
    if not titrant_conc_mol_l:
        ev.notes.append("Titrant concentration unknown (component H of the burette is 0): "
                        "no sample concentration.")
    return ev


def titrant_from_burettes(burettes):
    """(concentration mol/L as a magnitude, description) of the titrant from the active burette dicts.
    Component H in mM: negative = base, positive = acid. Several burettes: ratio-weighted mixture."""
    burettes = [b for b in burettes or [] if isinstance(b, dict)]
    if not burettes:
        return None, ""
    total = sum(float(b.get("ratio") or 1.0) for b in burettes) if len(burettes) > 1 else 1.0
    h = sum(float((b.get("concentrations") or {}).get("H", 0) or 0) * (float(b.get("ratio") or 1.0)
                                                                         if len(burettes) > 1 else 1.0)
            for b in burettes) / total
    names = " + ".join(str(b.get("name", "")) for b in burettes)
    if not h:
        return None, names
    kind = "base" if h < 0 else "acid"
    return abs(h) / 1000, f"{kind} {abs(h) / 1000:.4g} mol/L ({names})"


def evaluate_datafile(meta, rows):
    """Evaluate the rows of a data file (datafile.read) using its metadata."""
    v, e = [], []
    segments = set()
    for r in rows:
        if r.get("kind") == "eq" and r.get("emf_mv") not in (None, ""):
            v.append(float(r["v_total_ml"]))
            e.append(float(r["emf_mv"]))
            segments.add(r.get("segment"))
    sample = meta.get("sample") if isinstance(meta.get("sample"), dict) else {}
    conc, desc = titrant_from_burettes(meta.get("active_burettes"))
    ev = evaluate(v, e, conc, sample.get("v0_ml"), desc)
    if len(segments) > 1:
        ev.notes.append("The burette was changed during the titration; the concentration uses the "
                        "titrant of the first segment.")
    return ev


RESULT_COLUMNS = ["data_file", "sample", "ep", "v_eq_ml", "e_eq_mv", "dedv_max_mv_per_ml", "v_inflection_ml",
                  "dv_ml", "titrant", "c_titrant_mol_l", "v0_ml", "c_step_mol_l", "c_cum_mol_l", "stoichiometry"]


def results_path(data_path):
    root, _ = os.path.splitext(data_path)
    return root + "_results.csv"


def write_results(path, ev, data_file="", sample=""):
    """Write the results CSV: one row per equivalence point (no comment lines, so results
    of repeated titrations can be pasted below each other)."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(RESULT_COLUMNS)
        for p in ev.points:
            w.writerow([os.path.basename(data_file), sample, p.n, _f(p.v_eq_ml), _f(p.e_eq_mv), _f(p.dedv_max),
                        _f(p.v_inflection_ml), _f(p.dv_ml), ev.titrant, _f(ev.titrant_conc_mol_l), _f(ev.v0_ml),
                        _f(p.c_step_mol_l), _f(p.c_cum_mol_l), ev.stoichiometry])


def results_json(ev):
    return json.dumps(ev.to_dict(), ensure_ascii=False)


def _f(x):
    return "" if x is None else f"{x:.6g}"


# ---------------------------------------------------------------- Bjerrum formation function
KW = 1e-14


@dataclass
class BjerrumParams:
    """What the formation function needs besides the curve.

    c_analyte_mm:  total concentration of the acid/base in the sample (mM), from the sample
                   setup or from an equivalence point.
    n_protons:     number of dissociable protons of the fully protonated form (H3PO4: 3, NH4+: 1).
    bound_as_added: protons bound on the form put into the sample (H3PO4: 3, HAc: 1, Na2CO3: 0, NH3: 0).
    strong_acid_mm: strong acid in the sample (mM); negative for a strong base (e.g. NaOH).
    titrant_mol_l: titrant concentration, + for a strong acid (HCl), - for a strong base (NaOH).
    e0_mv, slope_mv: electrode calibration, E = e0 - slope * pH, slope at 25 °C (scaled to temp_c).
    """
    c_analyte_mm: float
    n_protons: int
    bound_as_added: int
    titrant_mol_l: float
    v0_ml: float
    strong_acid_mm: float = 0.0
    e0_mv: float = 400.0
    slope_mv: float = 59.16
    temp_c: float = 25.0

    def ph(self, emf):
        return (self.e0_mv - emf) / (self.slope_mv * (273.15 + self.temp_c) / 298.15)


def bjerrum(volumes, values, prm):
    """Formation function Z̄ (mean number of protons bound per molecule) at each point.

    Proton balance: all protons put in (bound on the analyte as added, strong acid, acid titrant)
    minus strong base and base titrant are either bound to the analyte or free (h - Kw/h):

        Z̄ = (C·n0 + C_acid - C_base - [H+] + [OH-]) / C

    with every concentration diluted to the current volume V0 + V. Returns a list of
    (v_ml, emf_mv, pH, Z̄), sorted by volume."""
    v, e = prepare(volumes, values)
    out = []
    if not prm.c_analyte_mm or prm.c_analyte_mm <= 0 or not prm.v0_ml:
        return out
    for vi, ei in zip(v, e):
        vt = prm.v0_ml + vi
        c = prm.c_analyte_mm / 1000 * prm.v0_ml / vt
        protons = (prm.strong_acid_mm / 1000 * prm.v0_ml + prm.titrant_mol_l * vi) / vt
        ph = prm.ph(ei)
        h = 10 ** -ph
        z = (c * prm.bound_as_added + protons - h + KW / h) / c
        out.append((vi, ei, ph, z))
    return out


def pka_from_bjerrum(points, n_protons):
    """pKa estimates where Z̄ crosses half-integers: pKa_i at Z̄ = N - i + 0.5 (i = 1..N, so pKa1 at
    the highest Z̄). Linear interpolation between neighbouring points in pH order; several crossings
    (noise) are averaged. Returns a list of (i, pKa or None if that Z̄ is not reached).
    Valid when the pKa values are well apart (> ~2 units); otherwise the steps overlap."""
    pts = sorted((ph, z) for _, _, ph, z in points)
    out = []
    for i in range(1, n_protons + 1):
        target = n_protons - i + 0.5
        xs = []
        for (p0, z0), (p1, z1) in zip(pts, pts[1:]):
            if (z0 - target) * (z1 - target) < 0 or z0 == target:
                xs.append(p0 if z1 == z0 else p0 + (target - z0) * (p1 - p0) / (z1 - z0))
        out.append((i, sum(xs) / len(xs) if xs else None))
    return out


def write_bjerrum(path, points, pkas, prm):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(f"# bjerrum: {json.dumps(asdict(prm))}\n")
        for i, pk in pkas:
            fh.write(f"# pKa{i}: {'' if pk is None else f'{pk:.3f}'}\n")
        w = csv.writer(fh)
        w.writerow(["v_total_ml", "emf_mv", "ph", "zbar"])
        for v, e, ph, z in points:
            w.writerow([_f(v), _f(e), f"{ph:.4f}", f"{z:.4f}"])


def bjerrum_path(data_path):
    root, _ = os.path.splitext(data_path)
    return root + "_bjerrum.csv"


def titrant_signed(burettes):
    """Titrant concentration in mol/L with sign: + acid, - base (component H of the active burettes)."""
    c, desc = titrant_from_burettes(burettes)
    if c is None:
        return None
    return -c if desc.startswith("base") else c
