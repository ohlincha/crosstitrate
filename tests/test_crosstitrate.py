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



class TestLinuxPermissions(unittest.TestCase):
    """Serial-port access: dialout, or the udev rule's uaccess for the logged-in user (users A and B alike)."""
    PORTS = [dict(device="/dev/ttyUSB0"), dict(device="/dev/ttyS0")]

    def check(self, **kw):
        from crosstitrate.diagnostics import linux_permission_checks
        return linux_permission_checks(self.PORTS, **kw)

    def test_dialout(self):
        f = self.check(groups={"dialout"}, root=False, can_open=lambda d: False)
        self.assertIn("OK", f[0].title)

    def test_uaccess_without_dialout(self):   # the .deb's rule: no group needed at the computer
        f = self.check(groups={"users"}, root=False, can_open=lambda d: d == "/dev/ttyUSB0")
        self.assertIn("logged-in user", f[0].title)

    def test_no_access(self):
        f = self.check(groups={"users"}, root=False, can_open=lambda d: False)
        self.assertIn("inaccessible", f[0].title)
        self.assertIn("dialout", f[0].detail)

    def test_no_ports(self):
        from crosstitrate.diagnostics import linux_permission_checks
        self.assertEqual(linux_permission_checks([], groups=set(), root=False), [])

    def test_rule_file(self):
        rule = open(os.path.join(os.path.dirname(__file__), "..", "packaging", "linux", "70-crosstitrate.rules")).read()
        self.assertIn('TAG+="uaccess"', rule)
        self.assertIn("ttyUSB", rule)


class TestManual(unittest.TestCase):
    """Help > Manual: the generated HTML is in the repo (packaged by build_deb.sh and PyInstaller)."""

    def test_found_in_checkout(self):
        from crosstitrate import manual
        p = manual.find()
        self.assertIsNotNone(p)
        self.assertTrue(p.endswith(os.path.join("docs", "MANUAL.html")))

    def test_missing_fails_gracefully(self):
        from unittest import mock
        from crosstitrate import manual
        with mock.patch.object(manual, "candidates", return_value=["/nonexistent/MANUAL.html"]):
            ok, msg = manual.open_manual()
        self.assertFalse(ok)
        self.assertIn("not found", msg)


if __name__ == "__main__":
    unittest.main()


def test_identify_wakes_an_instrument_that_was_already_on():
    """A Titrino that was on before the port was opened may miss the first request: identify() wakes it and
    asks again (user, 2026-11-25: it only connected when switched on after the program)."""
    from crosstitrate import transport
    from crosstitrate.instrument import Titrino, NoResponse
    from crosstitrate.simulator import SimulatedTitrino

    class Sleepy(transport.SimTransport):
        def __init__(self, sim, missed):
            super().__init__(sim, "sleepy")
            self.missed, self.wakes = missed, 0

        def wake(self):
            self.wakes += 1

        def request(self, line, timeout=1.5):
            if self.missed > 0:
                self.missed -= 1
                return ""
            return super().request(line, timeout)

    t = Sleepy(SimulatedTitrino(model="718"), missed=1)
    info = Titrino(t, label="T").identify()
    assert info["model"] and t.wakes == 2
    t = Sleepy(SimulatedTitrino(model="718"), missed=5)
    try:
        Titrino(t, label="T").identify()
        assert False, "should give up"
    except NoResponse:
        assert t.wakes == 3
