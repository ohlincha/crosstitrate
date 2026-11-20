import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crosstitrate import datafile, models, protocol  # noqa: E402
from crosstitrate.instrument import InstrumentError, Titrino  # noqa: E402
from crosstitrate.simulator import Clock, SimulatedTitrino  # noqa: E402
from crosstitrate.titration import Burette, Controller, Method, Sample, next_step  # noqa: E402
from crosstitrate.transport import SimTransport  # noqa: E402


class ProtocolTest(unittest.TestCase):
    def test_format_number(self):
        self.assertEqual(protocol.format_number(0.1), "0.1")
        self.assertEqual(protocol.format_number(1.23456), "1.2346")
        self.assertEqual(protocol.format_number(12345.678), "12345.7")
        self.assertEqual(protocol.format_number(10), "10")
        self.assertEqual(protocol.format_number(-0.00001), "0")

    def test_set_cmd(self):
        self.assertEqual(protocol.set_cmd("&Mode.Select", "MEAS"), '&Mode.Select "MEAS"')
        self.assertEqual(protocol.set_cmd("&Assembly.Bur.ModeDis.V", 0.25), '&Assembly.Bur.ModeDis.V "0.25"')
        with self.assertRaises(protocol.ProtocolError):
            protocol.set_cmd("&X", 'bad"value')

    def test_parse_value_and_float(self):
        self.assertEqual(protocol.parse_float('"-345.6"\r\r\n'), (-345.6, None))
        self.assertEqual(protocol.parse_float('"NV"\r\r\n'), (None, "NV"))
        self.assertEqual(protocol.parse_float('"not valid"\r\r\n'), (None, "NV"))
        self.assertEqual(protocol.parse_float('"OV"\r\r\n'), (None, "OV"))
        self.assertEqual(protocol.parse_value('"718.0010"\r\r\n'), "718.0010")

    def test_status(self):
        s = protocol.Status("$G.Assembly.Bur.ModeDis;E27\r\r\n")
        self.assertTrue(s.busy)
        self.assertEqual(s.errors, ["E27"])
        s = protocol.Status("$R.Mode.MEAS.Inac")
        self.assertTrue(s.ready)
        self.assertEqual(s.errors, [])
        self.assertIn("ready", s.describe())

    def test_profiles(self):
        self.assertEqual(models.profile_for_prog("718.0010").model, "718")
        self.assertTrue(models.profile_for_prog("751.0020").multi_burette)
        self.assertIs(models.profile_for_prog("999.1"), models.GENERIC)


class SimulatorDriverTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(500)
        self.sim = SimulatedTitrino(clock=self.clock, seed=1, exv=5)
        self.ti = Titrino(SimTransport(self.sim), self.clock)

    def test_identify(self):
        info = self.ti.identify()
        self.assertEqual(info["prog"], "718.0012")
        self.assertEqual(info["exv"], 5.0)

    def test_measure(self):
        self.ti.identify()
        self.ti.start_measuring("U")
        time.sleep(0.01)
        v = self.ti.read_measurement()
        self.assertAlmostEqual(v, self.sim.chem.emf(), delta=2)
        self.ti.stop_measuring()

    def test_dispense_with_refill(self):
        self.ti.identify()
        done = self.ti.dispense(7.0, rate=20)
        self.assertAlmostEqual(done, 7.0, places=3)
        self.assertAlmostEqual(self.sim.counter, 7.0, places=3)
        self.assertLessEqual(self.ti.piston_used, 5.0)

    def test_errors(self):
        with self.assertRaises(InstrumentError) as cm:
            self.ti.send("&No.Such.Object $Q")
        self.assertIn("E28", cm.exception.codes)
        with self.assertRaises(InstrumentError):
            self.ti.set("&Mode.Select", "NONSENSE")

    def test_abbreviated_commands(self):
        # Abbreviated object names, which the Titrino command language allows
        self.ti.send('&M.S"MEAS"', '&M.MEA"U"')
        self.assertEqual(self.sim.values["Mode.MEASQuantity"], "U")
        self.ti.send('&A.B.M.S"volume"', '&A.B.R.F.S"digital"', '&A.B.R.F.D"10"', '&A.B.M.V"0.5"',
                     '&A.B.M $G')
        self.ti.wait_ready(30)
        self.assertAlmostEqual(self.sim.counter, 0.5, places=4)

    def test_rs_control_off(self):
        sim = SimulatedTitrino(clock=self.clock, rs_control=False)
        ti = Titrino(SimTransport(sim), self.clock)
        self.assertFalse(ti.ping())


class StepTest(unittest.TestCase):
    def test_adaptive(self):
        m = Method(adaptive=True, halve_if_de_gt=10, double_if_de_lt=2, min_step_ml=0.05, max_step_ml=1)
        self.assertEqual(next_step(0.4, 20, m), 0.2)
        self.assertEqual(next_step(0.4, 1, m), 0.8)
        self.assertEqual(next_step(0.8, 1, m), 0.8)     # would exceed max
        self.assertEqual(next_step(0.06, 30, m), 0.06)  # would go below min
        self.assertEqual(next_step(0.4, 5, m), 0.4)
        self.assertEqual(next_step(0.4, 50, Method(adaptive=False)), 0.4)


class TitrationTest(unittest.TestCase):
    def test_full_titration(self):
        clock = Clock(1000)
        sim = SimulatedTitrino(clock=clock, seed=4, exv=10)
        ctl = Controller(clock)
        ins = Titrino(SimTransport(sim, "S"), clock)
        ins.identify()
        ctl.instruments["T1"] = ins
        b = Burette("NaOH", "T1", concentrations={"H": -100, "B": 0, "C": 0, "D": 0}, rate=30)
        m = Method(control="constant", eq_time_min=0.1, volume_step_ml=1.0, stop_volume_ml=4.0)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.csv")
            self.assertTrue(ctl.run_titration(Sample("x", 50, 25, {"H": 10, "B": 0, "C": 0, "D": 0}), m, [b],
                                              [0], path, "T1"))
            finished = None
            deadline = time.time() + 60
            while time.time() < deadline:
                ev = ctl.events.get(timeout=60)
                if ev[0] == "finished":
                    finished = ev
                if ev[0] == "error":
                    self.fail(ev[1])
                if ev[0] == "idle":
                    break
            self.assertIsNotNone(finished)
            self.assertIn("stop volume", finished[1])
            meta, rows = datafile.read(path)
            eq = [r for r in rows if r["kind"] == "eq"]
            self.assertEqual([float(r["v_total_ml"]) for r in eq], [0.0, 1.0, 2.0, 3.0, 4.0])
            self.assertEqual(meta["sample"]["v0_ml"], 50)


class LiveChangesTest(unittest.TestCase):
    def test_step_change_and_manual_addition(self):
        clock = Clock(1000)
        sim = SimulatedTitrino(clock=clock, seed=5, exv=20)
        ctl = Controller(clock)
        ins = Titrino(SimTransport(sim, "S"), clock)
        ins.identify()
        ctl.instruments["T1"] = ins
        b = Burette("NaOH", "T1", concentrations={"H": -100, "B": 0, "C": 0, "D": 0}, rate=30)
        m = Method(control="constant", eq_time_min=0.2, volume_step_ml=1.0, stop_volume_ml=5.0)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.csv")
            ctl.run_titration(Sample("x", 50, 25, {"H": 10, "B": 0, "C": 0, "D": 0}), m, [b], [0], path, "T1")
            n_eq, sent_step, sent_add = 0, False, False
            while True:
                ev = ctl.events.get(timeout=60)
                if ev[0] == "error":
                    self.fail(ev[1])
                if ev[0] == "reading" and ev[1].kind == "eq":
                    n_eq += 1
                if ev[0] == "countdown" and n_eq == 1 and not sent_step:
                    # during the equilibration of the 2nd point (at 1 mL), like a user would
                    ctl.request_method_change({"volume_step_ml": 0.5})
                    sent_step = True
                if ev[0] == "countdown" and n_eq == 3 and not sent_add:
                    ctl.request_addition(b, 0.25)   # during the equilibration of the 4th point
                    sent_add = True
                if ev[0] == "idle":
                    break
            meta, rows = datafile.read(path)
            eq = [float(r["v_total_ml"]) for r in rows if r["kind"] == "eq"]
            notes = [r["note"] for r in rows if r["kind"] == "event"]
            # 0, 1 (step 1), then step 0.5: 1.5; the next point (2.0) is interrupted by the manual
            # +0.25 mL, so it is re-measured at 2.25 and 2.0 is never accepted.
            self.assertEqual(eq[:4], [0.0, 1.0, 1.5, 2.25])
            self.assertNotIn(2.0, eq)
            self.assertTrue(any(n.startswith("settings changed") for n in notes))
            self.assertTrue(any(n.startswith("manual addition: 0.2500 mL") for n in notes))
            self.assertAlmostEqual(sim.counter, eq[-1], places=3)


class MonitorTest(unittest.TestCase):
    def test_monitor_writes_file_with_notes(self):
        clock = Clock(100)
        sim = SimulatedTitrino(clock=clock, seed=3)
        ctl = Controller(clock)
        ins = Titrino(SimTransport(sim, "S"), clock)
        ins.identify()
        ctl.instruments["T1"] = ins
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.csv")
            self.assertTrue(ctl.monitor("T1", interval_s=10, path=path))
            n, noted = 0, False
            while True:
                ev = ctl.events.get(timeout=60)
                if ev[0] == "error":
                    self.fail(ev[1])
                if ev[0] == "monitor_reading":
                    n += 1
                    if n == 3 and not noted:
                        ctl.request_note("ice forming")
                        noted = True
                    if n == 6:
                        ctl.stop()
                if ev[0] == "idle":
                    break
            meta, rows = datafile.read(path)
            self.assertEqual(meta["type"], "EMF monitor")
            mon = [r for r in rows if r["kind"] == "monitor"]
            self.assertGreaterEqual(len(mon), 6)
            elapsed = [float(r["elapsed_s"]) for r in mon]
            gaps = [b - a for a, b in zip(elapsed, elapsed[1:])]
            self.assertTrue(all(8 <= g <= 13 for g in gaps), gaps)
            self.assertTrue(all(r["emf_mv"] for r in mon))
            self.assertIn("ice forming", [r["note"] for r in rows if r["kind"] == "event"])


if __name__ == "__main__":
    unittest.main()
