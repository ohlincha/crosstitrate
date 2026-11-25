"""Simulator chemistry (charge balance, demo presets) and the evaluation of titration curves."""

import csv
import math
import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crosstitrate import datafile, evaluate  # noqa: E402
from crosstitrate.instrument import Titrino  # noqa: E402
from crosstitrate.simulator import (PRESETS, Chemistry, Clock, SimulatedTitrino, Species,  # noqa: E402
                                    chemistry_from_preset, preset_parts)
from crosstitrate.titration import Burette, Controller, Method, Sample  # noqa: E402
from crosstitrate.transport import SimTransport  # noqa: E402

V0, CT = 50.0, 0.1   # sample volume (mL), titrant concentration (mol/L)


def curve(preset, step=0.1, v_end=8.0, noise=0.0, seed=1, parts=None):
    """Simulated equilibrium curve (V, E) for a preset titrated with 0.1 M titrant."""
    rng = random.Random(seed)
    chem = chemistry_from_preset(preset, V0, parts=parts)
    tc = CT if PRESETS[preset]["titrant"] == "NaOH" else -CT
    v, e, n = [], [], 0
    while n * step <= v_end + 1e-9:
        v.append(n * step)
        e.append(chem.emf() + (rng.gauss(0, noise) if noise else 0.0))
        chem.add(step, tc)
        n += 1
    return v, e


class ChemistryTest(unittest.TestCase):
    def ph_after(self, chem, v, conc):
        chem.add(v, conc)
        return chem.ph()

    def test_old_default_unchanged(self):
        """Chemistry() is still 0.01 M strong + 0.005 M weak acid (pKa 4.76), same pH as the old formula."""
        chem = Chemistry()
        for v in (0.0, 2.0, 4.0, 5.0, 7.0, 8.0):
            c = Chemistry()
            c.add(v, 0.1)
            vt = 50 + v
            ca, cw, cb, ka = 0.01 * 50 / vt, 0.005 * 50 / vt, 0.1 * v / vt, 10 ** -4.76
            h = 10 ** -c.ph()
            self.assertAlmostEqual(h + cb - 1e-14 / h - ca - cw * ka / (ka + h), 0, delta=1e-9)
        self.assertAlmostEqual(chem.strong_acid, 0.01)
        self.assertAlmostEqual(chem.weak_acid, 0.005)

    def test_strong_acid_and_base(self):
        self.assertAlmostEqual(Chemistry(strong_acid=0.01, weak_acid=0).ph(), 2.0, places=3)
        self.assertAlmostEqual(Chemistry(strong_acid=0, weak_acid=0, strong_base=0.01).ph(), 12.0, places=3)
        c = Chemistry(strong_acid=0.01, weak_acid=0)
        self.assertAlmostEqual(self.ph_after(c, 5.0, 0.1), 7.0, places=2)   # equivalence

    def test_weak_acid(self):
        c = chemistry_from_preset("Acetic acid", V0)
        # 0.01 M acetic acid: pH = 3.39 (exact quadratic)
        ka = 10 ** -4.76
        h = (-ka + math.sqrt(ka * ka + 4 * ka * 0.01)) / 2
        self.assertAlmostEqual(c.ph(), -math.log10(h), places=2)
        self.assertAlmostEqual(self.ph_after(c, 2.5, 0.1), 4.76, places=1)   # half-neutralised: pH = pKa

    def test_polyprotic_and_bases(self):
        p = chemistry_from_preset("Phosphoric acid", V0)        # 5 mM: EP1 at 2.5 mL, EP2 at 5 mL
        self.assertAlmostEqual(self.ph_after(p, 3.75, 0.1), 7.20, delta=0.05)   # between EP1 and EP2: pKa2
        co = chemistry_from_preset("Sodium carbonate (titrate with HCl)", V0)
        self.assertAlmostEqual(self.ph_after(co, 2.5, -0.1), (6.35 + 10.33) / 2, delta=0.1)   # HCO3- at EP1
        nh = chemistry_from_preset("Ammonia (titrate with HCl)", V0)
        self.assertAlmostEqual(self.ph_after(nh, 2.5, -0.1), 9.25, delta=0.02)
        na = chemistry_from_preset("NaOH (titrate with HCl)", V0)
        self.assertAlmostEqual(na.ph(), 12.0, places=2)

    def test_species_charges(self):
        self.assertEqual(Species("NH3", 0.01, [9.25], charge=1, protons=0).counter_ion_charge(), 0)
        self.assertEqual(Species("Na2CO3", 0.01, [6.35, 10.33], protons=0).counter_ion_charge(), 2)
        self.assertEqual(Species("HAc", 0.01, [4.76]).counter_ion_charge(), 0)

    def test_presets_have_literature_pka(self):
        pk = {n: [k for p in PRESETS[n]["parts"] for k in p.get("pkas", [])] for n in PRESETS}
        self.assertEqual(pk["Phosphoric acid"], [2.15, 7.20, 12.35])
        self.assertEqual(pk["Sodium carbonate (titrate with HCl)"], [6.35, 10.33])
        self.assertEqual(pk["Ammonia (titrate with HCl)"], [9.25])
        self.assertEqual(pk["Acetic acid"], [4.76])
        self.assertEqual({PRESETS[n]["titrant"] for n in PRESETS}, {"NaOH", "HCl"})


class EvaluateTest(unittest.TestCase):
    def check(self, preset, truth, rel=0.01, **kw):
        v, e = curve(preset, **kw)
        ev = evaluate.evaluate(v, e, CT, V0, "NaOH")
        found = [p.v_eq_ml for p in ev.points]
        self.assertGreaterEqual(len(found), len(truth), f"{preset}: {found}")
        for t, f in zip(truth, found):
            self.assertAlmostEqual(f, t, delta=rel * t, msg=f"{preset}: found {found}, expected {truth}")
        return ev

    def test_hcl(self):
        ev = self.check("HCl", [5.0], rel=0.01)
        self.assertEqual(len(ev.points), 1)
        p = ev.points[0]
        self.assertAlmostEqual(p.c_step_mol_l, 0.01, delta=1e-4)
        self.assertAlmostEqual(p.c_cum_mol_l, 0.01, delta=1e-4)
        self.assertAlmostEqual(p.v_inflection_ml, 5.0, delta=0.05)
        # E at the equivalence point: pH 7
        self.assertAlmostEqual(p.e_eq_mv, 400 - 59.16 * 7, delta=40)

    def test_hcl_noisy_coarse_and_other_concentration(self):
        self.check("HCl", [5.0], noise=0.1, step=0.2)
        parts = preset_parts("HCl")
        parts[0]["conc_mm"] = 6.0      # 0.006 M: V_eq = 0.006 * 50 / 0.1 = 3 mL
        ev = self.check("HCl", [3.0], parts=parts)
        self.assertAlmostEqual(ev.points[0].c_step_mol_l, 0.006, delta=6e-5)

    def test_acetic_acid(self):
        ev = self.check("Acetic acid", [5.0])
        self.assertEqual(len(ev.points), 1)

    def test_phosphoric_acid_first_two(self):
        ev = self.check("Phosphoric acid", [2.5, 5.0])
        self.assertEqual(len(ev.points), 2)   # pKa3 = 12.35 gives no step
        for p in ev.points:
            self.assertAlmostEqual(p.c_step_mol_l, 0.005, delta=5e-5)

    def test_carbonate_with_hcl(self):
        ev = self.check("Sodium carbonate (titrate with HCl)", [2.5, 5.0])
        self.assertEqual(len(ev.points), 2)
        self.assertAlmostEqual(ev.points[1].c_cum_mol_l, 0.010, delta=1e-4)   # total alkalinity, 2 H+ per CO3

    def test_bases_with_hcl(self):
        self.check("NaOH (titrate with HCl)", [5.0])
        self.check("Ammonia (titrate with HCl)", [5.0])

    def test_hcl_plus_acetic(self):
        # The HCl step is less sharp in the presence of acetic acid: 2 % tolerance
        self.check("HCl + acetic acid", [2.5, 5.0], rel=0.02)

    def test_noise_does_not_create_points(self):
        for preset, n in (("HCl", 1), ("Phosphoric acid", 2), ("Sodium carbonate (titrate with HCl)", 2)):
            for seed in range(5):
                v, e = curve(preset, noise=0.1, seed=seed)
                self.assertEqual(len(evaluate.find_equivalence_points(v, e)), n, (preset, seed))

    def test_uneven_steps_and_duplicates(self):
        v, e = curve("HCl", step=0.05)
        keep = [i for i in range(len(v)) if v[i] < 4.0 and i % 4 == 0 or 4.0 <= v[i] <= 6.0
                or v[i] > 6.0 and i % 4 == 0]
        vv = [v[i] for i in keep] + [v[keep[3]]]
        ee = [e[i] for i in keep] + [e[keep[3]]]
        ps = evaluate.find_equivalence_points(vv, ee)
        self.assertEqual(len(ps), 1)
        self.assertAlmostEqual(ps[0]["v_eq_ml"], 5.0, delta=0.02)

    def test_flat_and_short_curves(self):
        self.assertEqual(evaluate.find_equivalence_points([0, 1, 2], [1, 2, 3]), [])
        ev = evaluate.evaluate([0, 1, 2, 3, 4, 5], [100, 99, 98, 97, 96, 95], CT, V0)
        self.assertEqual(ev.points, [])
        self.assertTrue(ev.notes)

    def test_unknown_titrant(self):
        v, e = curve("HCl")
        ev = evaluate.evaluate(v, e, None, V0)
        self.assertEqual(len(ev.points), 1)
        self.assertIsNone(ev.points[0].c_step_mol_l)
        self.assertTrue(any("unknown" in n for n in ev.notes))

    def test_titrant_from_burettes(self):
        c, d = evaluate.titrant_from_burettes([{"name": "NaOH", "concentrations": {"H": -100}}])
        self.assertAlmostEqual(c, 0.1)
        self.assertIn("base", d)
        c, d = evaluate.titrant_from_burettes([{"name": "HCl", "concentrations": {"H": 50}}])
        self.assertAlmostEqual(c, 0.05)
        self.assertIn("acid", d)
        self.assertEqual(evaluate.titrant_from_burettes([])[0], None)

    def test_roundtrip_dict(self):
        v, e = curve("Phosphoric acid")
        ev = evaluate.evaluate(v, e, CT, V0, "NaOH")
        back = evaluate.Evaluation.from_dict(ev.to_dict())
        self.assertEqual(len(back.points), 2)
        self.assertAlmostEqual(back.points[1].v_eq_ml, ev.points[1].v_eq_ml, places=4)
        self.assertIn("EP2", back.summary())


class EndToEndTest(unittest.TestCase):
    """A full simulated titration writes the results into the data file and a _results.csv."""

    def test_titration_with_results(self):
        clock = Clock(3000)
        chem = chemistry_from_preset("Phosphoric acid", V0)
        sim = SimulatedTitrino(clock=clock, seed=2, exv=20, chemistry=chem, titrant_conc=0.1)
        ctl = Controller(clock)
        ins = Titrino(SimTransport(sim, "S"), clock)
        ins.identify()
        ctl.instruments["T1"] = ins
        b = Burette("NaOH", "T1", concentrations={"H": -100, "B": 0, "C": 0, "D": 0}, rate=60)
        m = Method(control="constant", eq_time_min=1.5, volume_step_ml=0.2, stop_volume_ml=7.0)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.csv")
            ctl.run_titration(Sample("H3PO4", V0, 25), m, [b], [0], path, "T1")
            got = None
            deadline = time.time() + 120
            while time.time() < deadline:
                ev = ctl.events.get(timeout=120)
                if ev[0] == "error":
                    self.fail(ev[1])
                if ev[0] == "evaluated":
                    got = ev[1]
                if ev[0] == "idle":
                    break
            self.assertIsNotNone(got)
            self.assertEqual(len(got.points), 2, got.summary())
            for p, t in zip(got.points, (2.5, 5.0)):
                self.assertAlmostEqual(p.v_eq_ml, t, delta=0.01 * t)
            meta, rows = datafile.read(path)
            saved = evaluate.Evaluation.from_dict(meta["results"])
            self.assertAlmostEqual(saved.points[0].v_eq_ml, got.points[0].v_eq_ml, places=4)
            again = evaluate.evaluate_datafile(meta, rows)
            self.assertAlmostEqual(again.points[1].v_eq_ml, got.points[1].v_eq_ml, places=6)
            with open(evaluate.results_path(path), newline="", encoding="utf-8") as fh:
                res = list(csv.DictReader(fh))
            self.assertEqual([r["ep"] for r in res], ["1", "2"])
            self.assertAlmostEqual(float(res[0]["c_step_mol_l"]), 0.005, delta=5e-5)
            self.assertEqual(res[0]["sample"], "H3PO4")


class BjerrumTest(unittest.TestCase):
    """Formation function Z̄ vs pH and pKa at half-integer Z̄ (simulated electrode: E0 400 mV, 59.16 mV/pH)."""

    def pkas(self, preset, conc_mm=None, v_end=8.0, c_used=None):
        parts = preset_parts(preset)
        if conc_mm:
            for p in parts:
                p["conc_mm"] = conc_mm
        v, e = curve(preset, parts=parts, v_end=v_end, noise=0.05)
        weak = [p for p in parts if p["kind"] == "weak"][0]
        strong = sum(p["conc_mm"] for p in parts if p["kind"] == "strong acid") - \
            sum(p["conc_mm"] for p in parts if p["kind"] == "strong base")
        tc = -CT if PRESETS[preset]["titrant"] == "NaOH" else CT
        prm = evaluate.BjerrumParams(c_used or weak["conc_mm"], len(weak["pkas"]), weak["protons"], tc, V0, strong)
        pts = evaluate.bjerrum(v, e, prm)
        return [k for _, k in evaluate.pka_from_bjerrum(pts, len(weak["pkas"]))], pts

    def test_acetic_acid(self):
        pk, pts = self.pkas("Acetic acid")
        self.assertAlmostEqual(pk[0], 4.76, delta=0.05)
        self.assertAlmostEqual(pts[0][3], 1.0, delta=0.05)      # start: fully protonated
        self.assertAlmostEqual(pts[-1][3], 0.0, delta=0.05)     # excess NaOH: fully deprotonated

    def test_acetic_acid_with_hcl(self):
        pk, _ = self.pkas("HCl + acetic acid")
        self.assertAlmostEqual(pk[0], 4.76, delta=0.05)

    def test_phosphoric_acid(self):
        pk, _ = self.pkas("Phosphoric acid")          # 5 mM: Z̄ = 2.5 not reached (free H+ dominates)
        self.assertIsNone(pk[0])
        self.assertAlmostEqual(pk[1], 7.20, delta=0.05)
        self.assertIsNone(pk[2])                       # 12.35: beyond the pH range of the titration
        pk, _ = self.pkas("Phosphoric acid", conc_mm=50, v_end=80)
        self.assertAlmostEqual(pk[0], 2.15, delta=0.05)
        self.assertAlmostEqual(pk[1], 7.20, delta=0.05)

    def test_carbonate(self):
        pk, _ = self.pkas("Sodium carbonate (titrate with HCl)")
        self.assertAlmostEqual(pk[0], 6.35, delta=0.05)
        self.assertAlmostEqual(pk[1], 10.33, delta=0.05)

    def test_ammonia(self):
        pk, _ = self.pkas("Ammonia (titrate with HCl)")
        self.assertAlmostEqual(pk[0], 9.25, delta=0.05)

    def test_wrong_concentration_shifts_pka(self):
        pk, _ = self.pkas("Acetic acid", c_used=10.5)  # 5 % too high
        self.assertGreater(pk[0], 4.77)
        self.assertLess(pk[0], 4.85)

    def test_csv(self):
        pk, pts = self.pkas("Acetic acid")
        prm = evaluate.BjerrumParams(10, 1, 1, -CT, V0)
        with tempfile.TemporaryDirectory() as d:
            path = evaluate.bjerrum_path(os.path.join(d, "t.csv"))
            self.assertTrue(path.endswith("t_bjerrum.csv"))
            evaluate.write_bjerrum(path, pts, list(enumerate(pk, 1)), prm)
            meta, rows = datafile.read(path)
            self.assertAlmostEqual(float(meta["pKa1"]), 4.76, delta=0.05)
            self.assertEqual(meta["bjerrum"]["n_protons"], 1)
            self.assertAlmostEqual(float(rows[0]["zbar"]), 1.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
