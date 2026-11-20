"""Main window."""

import dataclasses
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import __version__, datafile, settings, transport
from ..instrument import InstrumentError, NoResponse, Titrino
from ..simulator import Chemistry, Clock, SimulatedTitrino
from ..titration import COMPONENTS, LIVE_FIELDS, Burette, Controller, Method, Sample
from . import dialogs
from .widgets import Form, FormError, Plot, autosize_columns

STATUS_COLORS = {"connected": "#1a7f37", "no answer": "#cf222e", "error": "#cf222e", "connecting…": "#9a6700"}


class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root)
        self.root = root
        self.clock = Clock(1.0)
        self.ctl = Controller(self.clock)
        self.cfg = settings.load()
        self.inst_cfg = dict(self.cfg.get("instruments", {}))     # name -> {port, settings, speed}
        self.inst_status = {}
        self.sims = {}
        self.sim_chem = Chemistry()
        self.components = list(self.cfg.get("components", COMPONENTS))
        self.burettes = [Burette.from_dict(b) for b in self.cfg.get("burettes", [])] or [
            Burette("Burette 1", "", concentrations={c: 0.0 for c in COMPONENTS})]
        self.active = [i for i in self.cfg.get("active", [0]) if i < len(self.burettes)] or [0]
        self.method = Method.from_dict(self.cfg.get("method", {}))
        self.sample = Sample(**{k: v for k, v in self.cfg.get("sample", {}).items()
                                if k in {f.name for f in dataclasses.fields(Sample)}})
        self.readings = []
        self.markers = []
        self.manual_volume = 0.0       # volume added manually outside a titration (for the table/plot)
        self.titrating = False
        self.monitoring = False
        self.x_mode = "volume"         # plot x axis: "volume" (titration) or "time" (EMF monitor)
        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()   # callables from worker threads, run on the Tk thread
        transport.LOG.listeners.append(self.log_queue.put)

        self.pack(fill="both", expand=True)
        self._build()
        self._load_forms()
        self.refresh_instruments()
        self.refresh_burettes()
        self._update_buttons()
        self.after(100, self.poll)
        self.after(300, self.reconnect_saved)

    # =============================================================== layout
    def _build(self):
        style = ttk.Style()
        style.configure("Big.TLabel", font=("TkDefaultFont", 30, "bold"))
        style.configure("Mid.TLabel", font=("TkDefaultFont", 12))
        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Notebook(pw, width=500)
        right = ttk.Frame(pw)
        pw.add(left, weight=0)
        pw.add(right, weight=1)
        front = ttk.Frame(left, padding=8)
        left.add(front, text="Titration")
        self._build_burettes(left)
        self._build_sample(left)
        self._build_method(left)
        self._build_run(right)
        self._build_front(front)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(8, 2)).pack(fill="x", side="bottom")

    def _build_front(self, tab):
        self.inst_tree = ttk.Treeview(tab, columns=("port", "model", "status"), height=3, selectmode="browse")
        self.inst_tree.heading("#0", text="Instrument")
        self.inst_tree.heading("port", text="Port")
        self.inst_tree.heading("model", text="Model")
        self.inst_tree.heading("status", text="Status")
        self.inst_tree.pack(fill="x")
        for st, col in STATUS_COLORS.items():
            self.inst_tree.tag_configure(st, foreground=col)
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=4)
        buttons = [("Connect…", self.add_instrument), ("Reconnect", self.reconnect_selected),
                   ("Remove", self.remove_instrument), ("Diagnostics…", self.diagnose_selected),
                   ("Instrument info", self.instrument_info),
                   ("Setup guide", lambda: dialogs.TextDialog(self.root, "Setup guide",
                                                              dialogs.setup_guide_text()).show())]
        for c in range(3):
            bar.columnconfigure(c, weight=1, uniform="ib")
        for i, (text, cmd) in enumerate(buttons):
            ttk.Button(bar, text=text, command=cmd).grid(row=i // 3, column=i % 3, sticky="ew", padx=2, pady=2)
        mf = ttk.Frame(tab)
        mf.pack(fill="x", pady=(4, 0))
        ttk.Label(mf, text="Measuring instrument:").pack(side="left")
        self.meas_var = tk.StringVar(value=self.cfg.get("meas_key", ""))
        self.meas_box = ttk.Combobox(mf, textvariable=self.meas_var, state="readonly", width=18)
        self.meas_box.pack(side="left", padx=4)

        # Main titration settings - the same values as on the Method tab
        qs = ttk.LabelFrame(tab, text="Titration settings", padding=6)
        qs.pack(fill="x", pady=(10, 0))
        q = Form(qs, label_width=24)
        q.frame.pack(fill="x")
        self.qform = q
        mv = self.mform.vars
        q.add("control", "Point acceptance", str, choices=["constant", "drift"], width=10, var=mv["control"],
              tip="constant: fixed equilibration time.\ndrift: until the EMF is stable (settings on the Method tab).")
        q.add("eq_time_min", "Equilibration time", float, "min", var=mv["eq_time_min"])
        q.add("volume_step_ml", "Volume step", float, "mL", var=mv["volume_step_ml"],
              tip="Can be changed during a titration: edit, then press 'Apply to running titration'.")
        q.add("adaptive", "Adjust step by ΔEMF", bool, var=mv["adaptive"],
              tip="Halve/double the step depending on the EMF change (limits on the Method tab).")
        q.section("Stop when")
        q.add("stop_volume_ml", "Volume reaches", float, "mL", var=mv["stop_volume_ml"])
        q.add("stop_emf_min", "EMF below (optional)", float, "mV", optional=True, var=mv["stop_emf_min"])
        q.add("stop_emf_max", "EMF above (optional)", float, "mV", optional=True, var=mv["stop_emf_max"])
        qb = ttk.Frame(qs)
        qb.pack(fill="x", pady=(6, 0))
        self.btn_apply = ttk.Button(qb, text="Apply to running titration", command=self.apply_live)
        self.btn_apply.pack(side="left")
        self.btn_pause2 = ttk.Button(qb, text="Pause", command=self.pause)
        self.btn_pause2.pack(side="left", padx=6)
        ttk.Label(qs, text="More settings (drift control, step limits) on the Method tab.",
                  foreground="#777").pack(anchor="w", pady=(4, 0))
        self.forms = [self.mform, self.qform]
        self._method_toggle()

        man = ttk.LabelFrame(tab, text="Manual control", padding=6)
        man.pack(fill="x", pady=(10, 0))
        r1 = ttk.Frame(man)
        r1.pack(fill="x")
        self.btn_measure = ttk.Button(r1, text="Measure EMF", command=self.measure_once)
        self.btn_measure.pack(side="left")
        self.btn_monitor = ttk.Button(r1, text="Monitor EMF", command=self.monitor)
        self.btn_monitor.pack(side="left", padx=4)
        self.btn_stop_manual = ttk.Button(r1, text="Stop", command=self.ctl.stop)
        self.btn_stop_manual.pack(side="left")
        ttk.Label(r1, text="save every").pack(side="left", padx=(10, 2))
        self.mon_interval = tk.StringVar(value=str(self.cfg.get("monitor_interval_s", 5)))
        ttk.Entry(r1, textvariable=self.mon_interval, width=5).pack(side="left")
        ttk.Label(r1, text="s").pack(side="left", padx=(2, 0))
        r2 = ttk.Frame(man)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="Add").pack(side="left")
        self.man_vol = tk.StringVar(value="0.1")
        ttk.Entry(r2, textvariable=self.man_vol, width=7).pack(side="left", padx=2)
        ttk.Label(r2, text="mL from").pack(side="left")
        self.man_bur = tk.StringVar()
        self.man_bur_box = ttk.Combobox(r2, textvariable=self.man_bur, state="readonly", width=12)
        self.man_bur_box.pack(side="left", padx=2)
        self.btn_add = ttk.Button(r2, text="Add", command=self.manual_add)
        self.btn_add.pack(side="left", padx=2)
        self.btn_fill = ttk.Button(r2, text="Fill", command=self.manual_fill)
        self.btn_fill.pack(side="left")
        r3 = ttk.Frame(man)
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="Note").pack(side="left")
        self.note_var = tk.StringVar()
        note_entry = ttk.Entry(r3, textvariable=self.note_var)
        note_entry.pack(side="left", fill="x", expand=True, padx=2)
        note_entry.bind("<Return>", lambda e: self.add_note())
        self.btn_note = ttk.Button(r3, text="Add note", command=self.add_note)
        self.btn_note.pack(side="left", padx=2)
        ttk.Label(man, text="Monitor EMF writes to the data file until you press Stop. Additions during "
                            "a titration and notes are recorded in the data file.", foreground="#777",
                  wraplength=430, justify="left").pack(anchor="w", pady=(4, 0))
        self.refresh_instruments()

    def _build_burettes(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Burettes")
        cols = ("active", "device", "rate") + tuple("c" + c for c in COMPONENTS) + ("ratio",)
        self.bur_tree = ttk.Treeview(tab, columns=cols, height=6, selectmode="browse")
        self.bur_tree.heading("#0", text="Name")
        self.bur_tree.column("#0", width=90)
        heads = {"active": "Use", "device": "Instrument", "rate": "Rate", "ratio": "Ratio"}
        for c in cols:
            self.bur_tree.heading(c, text=heads.get(c, c[1:]))
            self.bur_tree.column(c, anchor="center")
        xs = ttk.Scrollbar(tab, orient="horizontal", command=self.bur_tree.xview)
        self.bur_tree.config(xscrollcommand=xs.set)
        self.bur_tree.pack(fill="x")
        xs.pack(fill="x")
        self.bur_tree.bind("<Double-1>", lambda e: self.edit_burette())
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=4)
        for i, (text, cmd) in enumerate((("Add…", self.add_burette), ("Edit…", self.edit_burette),
                                         ("Remove", self.remove_burette), ("Use / don't use", self.toggle_active))):
            bar.columnconfigure(i, weight=1, uniform="bb")
            ttk.Button(bar, text=text, command=cmd).grid(row=0, column=i, sticky="ew", padx=2)
        ttk.Label(tab, wraplength=400, foreground="#555", justify="left", text=(
            "Rate in mL/min; concentrations H, B, C, D in mM (negative H for a base). Burettes marked 'Use' "
            "dispense at every step; with several, each adds step × ratio. During a titration, use "
            "'Change burette…' to switch.")).pack(anchor="w", pady=(8, 0))

    def _build_sample(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Sample")
        f = Form(tab, label_width=20)
        f.frame.pack(fill="x")
        self.sform = f
        f.add("name", "Sample name", str, width=24)
        f.add("v0_ml", "Start volume V₀", float, "mL")
        f.add("temperature_c", "Temperature", float, "°C")
        f.section("Start concentrations")
        for c in COMPONENTS:
            f.add("c_" + c, f"{c}₀", float, "mM")
        ttk.Label(tab, text="Comment:").pack(anchor="w", pady=(8, 0))
        self.comment = tk.Text(tab, height=5, width=40, wrap="word")
        self.comment.pack(fill="x")

    def _build_method(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Method")
        f = Form(tab, label_width=26)
        f.frame.pack(fill="x")
        self.mform = f
        f.add("control", "Point acceptance", str, choices=["constant", "drift"], width=12,
              tip="constant: wait a fixed equilibration time, then read.\n"
                  "drift: read at intervals until the EMF is stable.")
        f.section("Constant equilibration time")
        f.add("eq_time_min", "Equilibration time", float, "min")
        f.section("Drift-controlled")
        f.add("meas_interval_min", "Measurement interval", float, "min")
        f.add("drift_points", "Readings to calculate drift", int, "")
        f.add("max_drift_mv", "Max drift allowed", float, "mV",
              tip="Point accepted when |EMF − EMF N readings earlier| is below this.")
        f.add("max_readings", "Max readings per point", int, "")
        f.section("Additions")
        f.add("volume_step_ml", "Volume step", float, "mL")
        f.add("adaptive", "Adjust step by ΔEMF", bool)
        f.add("halve_if_de_gt", "  Halve step if ΔEMF >", float, "mV")
        f.add("double_if_de_lt", "  Double step if ΔEMF <", float, "mV")
        f.add("min_step_ml", "  Min step", float, "mL")
        f.add("max_step_ml", "  Max step", float, "mL")
        f.section("Stop when")
        f.add("stop_volume_ml", "Volume reaches", float, "mL")
        f.add("stop_emf_min", "EMF below (optional)", float, "mV", optional=True)
        f.add("stop_emf_max", "EMF above (optional)", float, "mV", optional=True)
        f.add("fill_at_end", "Fill burettes at the end", bool)
        f.vars["control"].trace_add("write", lambda *a: self._method_toggle())
        f.vars["adaptive"].trace_add("write", lambda *a: self._method_toggle())
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="Load method…", command=self.load_method).pack(side="left")
        ttk.Button(bar, text="Save method…", command=self.save_method).pack(side="left", padx=4)
        self.btn_apply_m = ttk.Button(bar, text="Apply to running titration", command=self.apply_live)
        self.btn_apply_m.pack(side="left", padx=4)

    def save_method(self):
        try:
            m = Method.from_dict({k: self.mform.get(k) for k in self.mform.vars})
        except FormError as e:
            messagebox.showerror("Save method", str(e))
            return
        p = filedialog.asksaveasfilename(title="Save method", defaultextension=".json",
                                         filetypes=[("Method files", "*.json")])
        if p:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"titrate_method": m.to_dict(), "sample": dataclasses.asdict(self.read_sample_safe()),
                           "burettes": [b.to_dict() for b in self.burettes], "active": self.active}, fh, indent=2)
            self.status(f"Method saved to {p}")

    def read_sample_safe(self):
        try:
            return self.read_forms()[0]
        except FormError:
            return self.sample

    def load_method(self):
        p = filedialog.askopenfilename(title="Load method", filetypes=[("Method files", "*.json")])
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            self.method = Method.from_dict(d.get("titrate_method", d))
            if "sample" in d and messagebox.askyesno("Load method", "Also load the sample and burette settings "
                                                                    "saved with this method?"):
                self.sample = Sample(**{k: v for k, v in d["sample"].items()
                                        if k in {f.name for f in dataclasses.fields(Sample)}})
                self.burettes = [Burette.from_dict(b) for b in d.get("burettes", [])] or self.burettes
                self.active = [i for i in d.get("active", [0]) if i < len(self.burettes)] or [0]
                self.refresh_burettes()
        except (OSError, ValueError, TypeError) as e:
            messagebox.showerror("Load method", f"Cannot load {p}: {e}")
            return
        self._load_forms()
        self.status(f"Loaded method {p}")

    def _method_toggle(self):
        drift = self.mform.vars["control"].get() == "drift"
        ad = self.mform.vars["adaptive"].get()
        state = {"eq_time_min": not drift}
        state.update({k: drift for k in ("meas_interval_min", "drift_points", "max_drift_mv", "max_readings")})
        state.update({k: ad for k in ("halve_if_de_gt", "double_if_de_lt", "min_step_ml", "max_step_ml")})
        for f in getattr(self, "forms", [self.mform]):
            for k, on in state.items():
                if k in f.vars:
                    f.enable(k, on)

    def _build_run(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        left = ttk.Frame(top)
        left.pack(side="left", fill="x", expand=True)
        self.emf_var = tk.StringVar(value="— mV")
        ttk.Label(left, textvariable=self.emf_var, style="Big.TLabel").pack(anchor="w")
        self.emf_note = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.emf_note, foreground="#666").pack(anchor="w")
        info = ttk.Frame(top)
        info.pack(side="right", anchor="n")
        self.info_vars = {}
        for i, (k, label) in enumerate((("vtot", "Total volume"), ("vseg", "Segment volume"),
                                        ("step", "Step"), ("drift", "Drift"), ("next", "Next"))):
            ttk.Label(info, text=label + ":").grid(row=i, column=0, sticky="e")
            v = tk.StringVar(value="—")
            ttk.Label(info, textvariable=v, width=22, style="Mid.TLabel").grid(row=i, column=1, sticky="w")
            self.info_vars[k] = v

        # Data file + run controls on one grid so the buttons line up in equal-width columns
        g = ttk.Frame(parent)
        g.pack(fill="x", pady=(8, 4))
        for c in range(6):
            g.columnconfigure(c, weight=1, uniform="btn")
        ttk.Label(g, text="Data file:").grid(row=0, column=0, sticky="w")
        self.file_var = tk.StringVar(value=self.default_file())
        self.file_entry = ttk.Entry(g, textvariable=self.file_var)
        self.file_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(0, 4))
        ttk.Button(g, text="Browse…", command=self.browse_file).grid(row=0, column=4, sticky="ew", padx=2)
        ttk.Button(g, text="Open data…", command=self.open_data).grid(row=0, column=5, sticky="ew", padx=2)
        self.btn_start = ttk.Button(g, text="▶ Start", command=self.start)
        self.btn_delayed = ttk.Button(g, text="Delayed start…", command=self.delayed_start)
        self.btn_pause = ttk.Button(g, text="Pause", command=self.pause)
        self.btn_stop = ttk.Button(g, text="■ Stop", command=self.stop)
        self.btn_change = ttk.Button(g, text="Change burette…", command=self.change_burette)
        self.btn_save_table = ttk.Button(g, text="Save table…", command=self.save_table)
        for i, btn in enumerate((self.btn_start, self.btn_delayed, self.btn_pause, self.btn_stop,
                                 self.btn_change, self.btn_save_table)):
            btn.grid(row=1, column=i, sticky="ew", padx=2, pady=(6, 0))

        pv = ttk.PanedWindow(parent, orient="vertical")
        pv.pack(fill="both", expand=True)
        self.plot = Plot(pv, xlabel="Volume added (mL)", ylabel="EMF (mV)", height=330)
        pv.add(self.plot, weight=3)
        nb = ttk.Notebook(pv)
        pv.add(nb, weight=2)
        dataf = ttk.Frame(nb)
        nb.add(dataf, text="Data")
        cols = ("time", "seg", "kind", "vtot", "vseg", "emf", "temp", "drift")
        self.data_tree = ttk.Treeview(dataf, columns=cols, show="headings", height=8)
        for c, label, w in (("time", "Time", 70), ("seg", "Seg", 35), ("kind", "Type", 55),
                            ("vtot", "V total (mL)", 85), ("vseg", "V seg (mL)", 85), ("emf", "EMF (mV)", 80),
                            ("temp", "T (°C)", 55), ("drift", "Drift (mV)", 70)):
            self.data_tree.heading(c, text=label)
            self.data_tree.column(c, width=w, anchor="e")
        self.data_tree.tag_configure("drift", foreground="#888")
        sb = ttk.Scrollbar(dataf, command=self.data_tree.yview)
        self.data_tree.config(yscrollcommand=sb.set)
        self.data_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.msg_text = self._text_tab(nb, "Messages")
        self.log_text = self._text_tab(nb, "Communication log", mono=True)
        self.msg_text.tag_config("error", foreground="#cf222e")

    def _text_tab(self, nb, title, mono=False):
        f = ttk.Frame(nb)
        nb.add(f, text=title)
        t = tk.Text(f, height=8, wrap="none" if mono else "word",
                    font=("TkFixedFont", 9) if mono else ("TkDefaultFont", 9))
        sb = ttk.Scrollbar(f, command=t.yview)
        t.config(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        return t

    # =============================================================== forms
    def _load_forms(self):
        s = self.sample
        f = self.sform
        f.set("name", s.name)
        f.set("v0_ml", s.v0_ml)
        f.set("temperature_c", s.temperature_c)
        for c in COMPONENTS:
            f.set("c_" + c, s.concentrations.get(c, 0.0))
        self.comment.delete("1.0", "end")
        self.comment.insert("1.0", s.comment)
        m = self.method
        for k in self.mform.vars:
            self.mform.set(k, getattr(m, k))
        self._method_toggle()

    def read_forms(self):
        f = self.sform
        sample = Sample(name=f.get("name"), v0_ml=f.get("v0_ml"), temperature_c=f.get("temperature_c"),
                        concentrations={c: f.get("c_" + c) for c in COMPONENTS},
                        comment=self.comment.get("1.0", "end").strip())
        if sample.v0_ml <= 0:
            raise FormError("Start volume V₀ must be > 0")
        m = Method.from_dict({k: self.mform.get(k) for k in self.mform.vars})
        errs = m.validate()
        if errs:
            raise FormError("Method: " + "; ".join(errs))
        return sample, m

    def save_settings(self):
        try:
            self.sample, self.method = self.read_forms()
        except FormError:
            pass
        self.cfg.update(instruments=self.inst_cfg, burettes=[b.to_dict() for b in self.burettes],
                        active=self.active, method=self.method.to_dict(),
                        sample=dataclasses.asdict(self.sample), meas_key=self.meas_var.get(),
                        components=self.components,
                        data_dir=os.path.dirname(self.file_var.get()))
        try:
            settings.save(self.cfg)
        except OSError as e:
            self.message(f"Could not save settings: {e}", error=True)

    # =============================================================== instruments
    def refresh_instruments(self):
        if not hasattr(self, "inst_tree"):
            return
        self.inst_tree.delete(*self.inst_tree.get_children())
        for name, c in self.inst_cfg.items():
            ins = self.ctl.instruments.get(name)
            st = self.inst_status.get(name, "disconnected")
            model = ins.profile.title if ins else ""
            port = "simulator" if c["port"] == dialogs.SIMULATOR else c["port"]
            self.inst_tree.insert("", "end", iid=name, text=name, values=(port, model, st), tags=(st,))
        autosize_columns(self.inst_tree)
        names = list(self.inst_cfg)
        self.meas_box["values"] = names
        if self.meas_var.get() not in names:
            self.meas_var.set(names[0] if names else "")
        self.refresh_burettes()

    def selected_instrument(self):
        sel = self.inst_tree.selection()
        return sel[0] if sel else (next(iter(self.inst_cfg)) if len(self.inst_cfg) == 1 else None)

    def add_instrument(self):
        r = dialogs.ConnectDialog(self.root, set(self.inst_cfg)).show()
        if not r:
            return
        name = r.pop("name")
        self.inst_cfg[name] = r
        if not self.meas_var.get():
            self.meas_var.set(name)
        for b in self.burettes:
            if not b.device:
                b.device = name
        self.connect(name)
        self.save_settings()

    def connect(self, name):
        c = self.inst_cfg[name]
        old = self.ctl.instruments.pop(name, None)
        if old:
            old.close()
        self.inst_status[name] = "connecting…"
        self.refresh_instruments()

        def job():
            try:
                if c["port"] == dialogs.SIMULATOR:
                    sim = SimulatedTitrino(model="718", clock=self.clock, chemistry=self.sim_chem)
                    self.sims[name] = sim
                    t = transport.SimTransport(sim, name)
                else:
                    t = transport.SerialTransport(c["port"], transport.SerialSettings.from_dict(c.get("settings")))
                ins = Titrino(t, self.clock, label=name)
                try:
                    info = ins.identify()
                except NoResponse:
                    t.close()
                    self.ui_queue.put(lambda: self._connected(name, None, "no answer"))
                    return
                self.ui_queue.put(lambda: self._connected(name, ins, "connected", info))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.ui_queue.put(lambda: self._connected(name, None, "error", msg))
        threading.Thread(target=job, daemon=True).start()

    def _connected(self, name, ins, status, info=None):
        if name not in self.inst_cfg:
            if ins:
                ins.close()
            return
        self.inst_status[name] = status
        if ins:
            self.ctl.instruments[name] = ins
            self.message(f"{name}: {info['model']} (program {info['prog']}, exchange unit "
                         f"{info['exv'] or '?'} mL) – {info['status']}")
            if not info["verified"]:
                self.message(f"{name}: note – this model has not yet been verified with this software "
                             f"on real hardware. {info['notes']}")
        elif status == "no answer":
            self.message(f"{name}: the port opened but the instrument does not answer. "
                         "Use Diagnostics… to find out why.", error=True)
        else:
            self.message(f"{name}: {info}", error=True)
        self._update_clock()
        self.refresh_instruments()
        self._update_buttons()

    def _update_clock(self):
        real = [n for n in self.ctl.instruments if self.inst_cfg[n]["port"] != dialogs.SIMULATOR]
        speeds = [self.inst_cfg[n].get("speed", 1) for n in self.ctl.instruments if n in self.sims]
        speed = 1.0 if real or not speeds else float(max(speeds))
        if speed != self.clock.speed:
            self.clock.set_speed(speed)
            self.message(f"Simulation speed ×{speed:g}")

    def reconnect_saved(self):
        for name in self.inst_cfg:
            self.connect(name)

    def reconnect_selected(self):
        name = self.selected_instrument()
        if name and not self.ctl.busy:
            self.connect(name)

    def remove_instrument(self):
        name = self.selected_instrument()
        if not name or self.ctl.busy:
            return
        ins = self.ctl.instruments.pop(name, None)
        if ins:
            ins.close()
        self.inst_cfg.pop(name, None)
        self.sims.pop(name, None)
        self.inst_status.pop(name, None)
        self.refresh_instruments()
        self.save_settings()

    def diagnose_selected(self):
        name = self.selected_instrument()
        c = self.inst_cfg.get(name) if name else None
        if c and c["port"] != dialogs.SIMULATOR:
            ins = self.ctl.instruments.pop(name, None)   # free the port for the test
            if ins:
                ins.close()
            self.inst_status[name] = "disconnected"
            self.refresh_instruments()
        r = dialogs.ConnectDialog(self.root, set(self.inst_cfg) - {name}, initial=dict(c or {}, name=name)
                                  if c else None).show()
        if r:
            newname = r.pop("name")
            if name and newname != name:
                self.inst_cfg.pop(name, None)
            self.inst_cfg[newname] = r
            self.connect(newname)
            self.save_settings()
        elif name and c:
            self.connect(name)

    def instrument_info(self):
        name = self.selected_instrument()
        ins = self.ctl.instruments.get(name) if name else None
        if not ins:
            messagebox.showinfo("Instrument info", "Select a connected instrument.")
            return
        if self.ctl.busy:
            messagebox.showinfo("Instrument info", "Busy - try again when idle.")
            return
        try:
            st = ins.status().describe()
            l1, l2 = ins.display()
        except (InstrumentError, transport.TransportError) as e:
            messagebox.showerror("Instrument info", str(e))
            return
        p = ins.profile
        text = (f"{name}\n\nModel: {p.title}\nProgram version: {ins.prog}\nDevice name: {ins.devname}\n"
                f"Exchange unit: {ins.exv or '?'} mL\nStatus: {st}\nDisplay:\n  {l1}\n  {l2}\n\n"
                f"Measures: {', '.join(p.quantities)}; multi-burette: {'yes' if p.multi_burette else 'no'}\n"
                f"Verified with real hardware: {'yes' if p.verified else 'not yet'}\n{p.notes}")
        dialogs.TextDialog(self.root, "Instrument info", text, width=70, height=18).show()

    # =============================================================== burettes
    def refresh_burettes(self):
        if not hasattr(self, "bur_tree"):
            return
        self.bur_tree.delete(*self.bur_tree.get_children())
        for i, b in enumerate(self.burettes):
            self.bur_tree.insert("", "end", iid=str(i), text=b.name, values=(
                "✔" if i in self.active else "", b.device, f"{b.rate:g}",
                *[f"{b.concentrations.get(c, 0):g}" for c in COMPONENTS], f"{b.ratio:g}"))
        autosize_columns(self.bur_tree)
        names = [b.name for b in self.burettes]
        self.man_bur_box["values"] = names
        if self.man_bur.get() not in names and names:
            self.man_bur.set(names[0])

    def _sel_burette(self):
        sel = self.bur_tree.selection()
        return int(sel[0]) if sel else (0 if len(self.burettes) == 1 else None)

    def add_burette(self):
        b = Burette(f"Burette {len(self.burettes) + 1}", next(iter(self.inst_cfg), ""))
        r = dialogs.BuretteDialog(self.root, b, list(self.inst_cfg), self.components).show()
        if r:
            self.burettes.append(r)
            self.refresh_burettes()
            self.save_settings()

    def edit_burette(self):
        i = self._sel_burette()
        if i is None:
            return
        r = dialogs.BuretteDialog(self.root, self.burettes[i], list(self.inst_cfg), self.components).show()
        if r:
            self.burettes[i] = r
            self.refresh_burettes()
            self.save_settings()

    def remove_burette(self):
        i = self._sel_burette()
        if i is None or len(self.burettes) <= 1 or self.titrating:
            return
        del self.burettes[i]
        self.active = [a if a < i else a - 1 for a in self.active if a != i] or [0]
        self.refresh_burettes()
        self.save_settings()

    def toggle_active(self):
        i = self._sel_burette()
        if i is None or self.titrating:
            return
        if i in self.active:
            if len(self.active) > 1:
                self.active.remove(i)
        else:
            self.active.append(i)
            self.active.sort()
        self.refresh_burettes()
        self.save_settings()

    # =============================================================== manual
    def _meas_instrument(self):
        name = self.meas_var.get()
        if name not in self.ctl.instruments:
            messagebox.showerror("Not connected", "Connect the measuring instrument first "
                                                  "(Instruments tab).")
            return None
        return name

    def measure_once(self):
        name = self._meas_instrument()
        if name:
            self.status("Measuring…")
            self.ctl.measure_once(name)

    def monitor(self):
        name = self._meas_instrument()
        if not name:
            return
        try:
            interval = float(self.mon_interval.get().replace(",", "."))
            if not 0.5 <= interval <= 86400:
                raise ValueError
        except ValueError:
            messagebox.showerror("Monitor EMF", "Enter a save interval between 0.5 and 86400 seconds.")
            return
        path = self.file_var.get().strip()
        if os.path.basename(path).startswith("titration_") and not os.path.exists(path):
            path = os.path.join(os.path.dirname(path), time.strftime("monitor_%Y%m%d_%H%M.csv"))
            self.file_var.set(path)
        if not path:
            messagebox.showerror("Monitor EMF", "Choose a data file.")
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        except OSError as e:
            messagebox.showerror("Monitor EMF", f"Cannot create folder: {e}")
            return
        if os.path.exists(path) and not messagebox.askyesno("Overwrite?", f"{path}\nexists. Overwrite it?"):
            return
        self.cfg["monitor_interval_s"] = interval
        self.save_settings()
        meta = {"sample": dataclasses.asdict(self.sample)}
        self._clear_data("time")
        if self.ctl.monitor(name, interval_s=interval, path=path, meta=meta, v_total=self.manual_volume):
            self.monitoring = True
            self.current_path = path
            self.status(f"Monitoring EMF, saving every {interval:g} s - press Stop to end.")
            self._update_buttons()

    def add_note(self):
        text = self.note_var.get().strip()
        if not text:
            return
        if not (self.titrating or self.monitoring):
            self.status("Notes are written to the data file while a titration or EMF monitor is running.")
            return
        self.ctl.request_note(text)
        self.note_var.set("")

    def _clear_data(self, x_mode):
        self.readings.clear()
        self.markers.clear()
        self.plot.clear()
        self.data_tree.delete(*self.data_tree.get_children())
        self.x_mode = x_mode
        self.plot.xlabel = "Time (min)" if x_mode == "time" else "Volume added (mL)"

    def _manual_burette(self):
        names = [b.name for b in self.burettes]
        if self.man_bur.get() not in names:
            return None
        b = self.burettes[names.index(self.man_bur.get())]
        if b.device not in self.ctl.instruments:
            messagebox.showerror("Not connected", f"The instrument for {b.name} ('{b.device}') is not connected.")
            return None
        return b

    def manual_add(self):
        b = self._manual_burette()
        if not b:
            return
        try:
            v = float(self.man_vol.get().replace(",", "."))
            if not 0 < v <= 100:
                raise ValueError
        except ValueError:
            messagebox.showerror("Add volume", "Enter a volume between 0 and 100 mL.")
            return
        if self.titrating:
            self.ctl.request_addition(b, v)
            self.status(f"Adding {v} mL from {b.name} now; it will be recorded and the equilibration restarts.")
            return
        self.status(f"Adding {v} mL from {b.name}…")
        self.ctl.dispense(b.device, b.select, v, b.rate)

    def manual_fill(self):
        b = self._manual_burette()
        if b:
            self.status(f"Filling {b.name}…")
            self.ctl.fill(b.device, b.select)

    # =============================================================== titration
    def default_file(self):
        d = self.cfg.get("data_dir") or settings.data_dir()
        return os.path.join(d, time.strftime("titration_%Y%m%d_%H%M.csv"))

    def browse_file(self):
        p = filedialog.asksaveasfilename(title="Data file", defaultextension=".csv",
                                         initialdir=os.path.dirname(self.file_var.get()),
                                         initialfile=os.path.basename(self.file_var.get()),
                                         filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if p:
            self.file_var.set(p)

    def _prepare(self):
        try:
            sample, method = self.read_forms()
        except FormError as e:
            messagebox.showerror("Cannot start", str(e))
            return None
        meas = self._meas_instrument()
        if not meas:
            return None
        ins = self.ctl.instruments[meas]
        if not ins.profile.potentiometric:
            messagebox.showerror("Cannot start", f"{ins.profile.title} cannot measure EMF (mV). Choose a "
                                                 "potentiometric instrument as measuring instrument.")
            return None
        for i in self.active:
            b = self.burettes[i]
            if b.device not in self.ctl.instruments:
                messagebox.showerror("Cannot start", f"{b.name}: instrument '{b.device}' is not connected.")
                return None
        path = self.file_var.get().strip()
        if not path:
            messagebox.showerror("Cannot start", "Choose a data file.")
            return None
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        except OSError as e:
            messagebox.showerror("Cannot start", f"Cannot create folder: {e}")
            return None
        if os.path.exists(path) and not messagebox.askyesno("Overwrite?", f"{path}\nexists. Overwrite it?"):
            return None
        self.sample, self.method = sample, method
        self.save_settings()
        self._prepare_simulation(sample)
        return sample, method, meas, path

    def _prepare_simulation(self, sample):
        if not self.sims:
            return
        chem = Chemistry(v0=sample.v0_ml, strong_acid=max(0.0, sample.concentrations.get("H", 0)) / 1000,
                         weak_acid=max(0.0, sample.concentrations.get("B", 0)) / 1000,
                         temp=sample.temperature_c)
        if chem.strong_acid == 0 and chem.weak_acid == 0:
            chem.strong_acid = 0.01   # something to titrate in the demo
        self.sim_chem = chem
        for name, sim in self.sims.items():
            sim.chem = chem
            sim.electrode = chem.emf()
            sim.counter = 0.0
        self._prepare_sim_burettes()

    def start(self, start_at=None):
        prep = self._prepare()
        if not prep:
            return
        sample, method, meas, path = prep
        self._clear_data("volume")
        self.manual_volume = 0.0
        self.info_vars["step"].set(f"{method.volume_step_ml:g} mL")
        extra = {"note": "Created with CrossTitrate; see the README for the file format."}
        if self.ctl.run_titration(sample, method, self.burettes, self.active, path, meas, extra, start_at):
            self.titrating = True
            self.current_path = path
            self._update_buttons()

    def apply_live(self):
        """Send edited settings (step size, stop conditions, ...) to the running titration."""
        job = getattr(self.ctl, "current", None)
        if not self.titrating or job is None:
            self.status("Not titrating - the settings will be used when you start.")
            return
        try:
            m = Method.from_dict({k: self.mform.get(k) for k in self.mform.vars})
        except FormError as e:
            messagebox.showerror("Apply", str(e))
            return
        errs = m.validate()
        if errs:
            messagebox.showerror("Apply", "; ".join(errs))
            return
        changed = {k: getattr(m, k) for k in LIVE_FIELDS if getattr(m, k) != getattr(job.m, k)}
        if abs(m.volume_step_ml - job.step) > 1e-12:
            changed["volume_step_ml"] = m.volume_step_ml
        if not changed:
            self.status("No changes to apply.")
            return
        self.ctl.request_method_change(changed)
        self.message("Applying to the running titration: " + ", ".join(f"{k} = {v}" for k, v in changed.items()))
        self.status("Changes will be applied before the next addition.")

    def delayed_start(self):
        t = dialogs.DelayedStartDialog(self.root).show()
        if t:
            self.start(start_at=t)

    def pause(self):
        if self.ctl.pause_event.is_set():
            self.ctl.pause_event.clear()
            text = "Pause"
            self.status("Resumed.")
        else:
            self.ctl.pause_event.set()
            text = "Resume"
            self.status("Paused: the waiting time is frozen (an addition in progress finishes first).")
        for b in (self.btn_pause, self.btn_pause2):
            b.config(text=text)

    def stop(self):
        if self.titrating and not messagebox.askyesno("Stop", "Stop the titration now?"):
            return
        self.ctl.stop()
        for b in (self.btn_pause, self.btn_pause2):
            b.config(text="Pause")

    def change_burette(self):
        if not self.titrating:
            messagebox.showinfo("Change burette", "Edit the burettes on the Burettes tab. During a titration "
                                                  "this button switches burettes at the next addition.")
            return
        job = getattr(self.ctl, "current", None)
        r = dialogs.ChangeBuretteDialog(self.root, self.burettes, job.active if job else self.active,
                                        job.m if job else self.method, self.components,
                                        list(self.inst_cfg)).show()
        if not r:
            return
        burettes, active, fields = r
        missing = [burettes[i].name for i in active if burettes[i].device not in self.ctl.instruments]
        if missing:
            messagebox.showerror("Change burette", f"Not connected: {', '.join(missing)}")
            return
        self.burettes, self.active = burettes, active
        self.refresh_burettes()
        self._prepare_sim_burettes()
        self.ctl.request_burette_change(burettes, active, fields)
        self.status("Burette change will be applied before the next addition.")

    def _prepare_sim_burettes(self):
        """Simulator only: titrant of each simulated instrument = its active burette
        (or its first burette if none of its burettes is active)."""
        for name, sim in self.sims.items():
            mine = [i for i, b in enumerate(self.burettes) if b.device == name]
            chosen = [i for i in mine if i in self.active] or mine
            if chosen:
                h = self.burettes[chosen[0]].concentrations.get("H", 0)
                sim.titrant_conc = -h / 1000 if h else 0.1

    # =============================================================== data
    def open_data(self):
        p = filedialog.askopenfilename(title="Open titration data", initialdir=os.path.dirname(self.file_var.get()),
                                       filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not p:
            return
        try:
            meta, rows = datafile.read(p)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Open", f"Cannot read {p}: {e}")
            return
        monitor = meta.get("type") == "EMF monitor" or any(r.get("kind") == "monitor" for r in rows)
        self._clear_data("time" if monitor else "volume")
        num = lambda x: float(x) if x not in (None, "") else None
        for r in rows:
            elapsed_min = (num(r.get("elapsed_s")) or 0.0) / 60
            if r.get("emf_mv", "") == "":
                if r.get("kind") == "event":
                    note = r.get("note", "")
                    if monitor:
                        self.markers.append((elapsed_min, note[:20]))
                    elif note.startswith("burette change"):
                        self.markers.append((num(r["v_total_ml"]) or 0, f"seg {int(r['segment'])}"))
                continue
            self._add_row(r["timestamp"][11:19], int(r["segment"] or 0), r["kind"], num(r["v_total_ml"]) or 0,
                          num(r["v_segment_ml"]) or 0, float(r["emf_mv"]), num(r["temperature_c"]),
                          num(r["drift_mv"]), x=elapsed_min if monitor else None)
        self.redraw_plot()
        self.status(f"Loaded {p} ({len(rows)} rows).")

    def save_table(self):
        """Save the rows currently in the data table (e.g. manual measurements) as CSV."""
        rows = [self.data_tree.item(i, "values") for i in self.data_tree.get_children()]
        if not rows:
            messagebox.showinfo("Save table", "The table is empty.")
            return
        p = filedialog.asksaveasfilename(title="Save table", defaultextension=".csv",
                                         initialdir=os.path.dirname(self.file_var.get()),
                                         initialfile=time.strftime("measurements_%Y%m%d_%H%M.csv"),
                                         filetypes=[("CSV files", "*.csv")])
        if not p:
            return
        import csv
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([self.data_tree.heading(c, "text") for c in self.data_tree["columns"]])
            w.writerows(rows)
        self.status(f"Table saved to {p}")

    def _add_row(self, tstr, seg, kind, vtot, vseg, emf, temp, drift, x=None):
        self.readings.append((vtot if x is None else x, emf, kind, seg))
        self.data_tree.insert("", "end", values=(tstr, seg, kind, f"{vtot:.4f}", f"{vseg:.4f}", f"{emf:.1f}",
                                                 "" if temp is None else f"{temp:.1f}",
                                                 "" if drift is None else f"{drift:.2f}"),
                              tags=("drift",) if kind == "drift" else ())
        self.data_tree.yview_moveto(1.0)

    def redraw_plot(self):
        eq = [(v, e) for v, e, k, s in self.readings if k in ("eq", "manual", "monitor")]
        dr = [(v, e) for v, e, k, s in self.readings if k == "drift"]
        self.plot.markers = list(self.markers)
        self.plot.set_series("drift", dr, color="#bbbbbb", line=False, dot=2)
        self.plot.set_series("eq", eq, color="#1f6fd1", line=True, dot=3)

    # =============================================================== events
    def poll(self):
        try:
            for _ in range(200):
                self.handle(self.ctl.events.get_nowait())
        except queue.Empty:
            pass
        try:
            for _ in range(50):
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.drain_log()
        self.after(100, self.poll)

    def handle(self, ev):
        kind, args = ev[0], ev[1:]
        if kind == "busy":
            self._update_buttons()
        elif kind == "idle":
            self.titrating = False
            self.monitoring = False
            self.info_vars["next"].set("—")
            self._update_buttons()
        elif kind == "status":
            self.status(args[0])
        elif kind == "error":
            self.message(args[0], error=True)
            self.status("Error: " + args[0].splitlines()[0])
            messagebox.showerror("Error", args[0].split("\nTraceback")[0])
        elif kind == "countdown":
            remaining, label = args
            self.info_vars["next"].set("paused" if remaining is None else f"{label}: {remaining:,.0f} s"
                                       if remaining >= 1 else label)
        elif kind == "reading":
            r = args[0]
            self.emf_var.set(f"{r.emf:.1f} mV")
            self.emf_note.set(f"{'accepted' if r.kind == 'eq' else 'reading'} at {r.v_total:.4f} mL")
            self._add_row(time.strftime("%H:%M:%S", time.localtime(r.t)), r.segment, r.kind, r.v_total,
                          r.v_segment, r.emf, r.temperature, r.drift)
            self.info_vars["vtot"].set(f"{r.v_total:.4f} mL")
            self.info_vars["vseg"].set(f"{r.v_segment:.4f} mL")
            self.redraw_plot()
        elif kind == "live":
            self.emf_var.set(f"{args[1]:.1f} mV")
            self.emf_note.set(f"live ({args[0]})")
        elif kind == "measured":
            key, v, temp = args
            self.emf_var.set(f"{v:.1f} mV")
            self.emf_note.set(f"measured ({key}) at {self.manual_volume:.4f} mL")
            self._add_row(time.strftime("%H:%M:%S"), 0, "manual", self.manual_volume, self.manual_volume, v,
                          temp, None)
            self.redraw_plot()
            self.message(f"{key}: EMF {v:.1f} mV" + (f", {temp:.1f} °C" if temp is not None else ""))
        elif kind == "monitor_started":
            self.message(f"EMF monitor started, writing {args[0]}")
        elif kind == "monitor_reading":
            t, elapsed, v, temp, vtot = args
            self._add_row(time.strftime("%H:%M:%S", time.localtime(t)), 0, "monitor", vtot, vtot, v, temp, None,
                          x=elapsed / 60)
            self.redraw_plot()
        elif kind == "monitor_finished":
            path, n = args
            self.monitoring = False
            self.message(f"EMF monitor stopped: {n} readings saved to {path}")
            self.status(f"EMF monitor stopped; {n} readings saved to {path}")
            self.file_var.set(self.default_file())
        elif kind == "note":
            elapsed, text = args
            self.message(f"Note: {text}")
            if elapsed is not None and self.x_mode == "time":
                self.markers.append((elapsed / 60, text[:20]))
                self.redraw_plot()
        elif kind == "dispensed":
            self.manual_volume += args[1]
            self.info_vars["vtot"].set(f"{self.manual_volume:.4f} mL (manual)")
            self.message(f"{args[0]}: dispensed {args[1]:.4f} mL")
            self.status(f"Dispensed {args[1]:.4f} mL.")
        elif kind == "volume":
            self.info_vars["vtot"].set(f"{args[0]:.4f} mL")
            self.info_vars["vseg"].set(f"{args[1]:.4f} mL")
        elif kind == "step":
            self.info_vars["step"].set(f"{args[0]:g} mL")
        elif kind == "drift":
            self.info_vars["drift"].set(f"{args[0]:.2f} mV (reading {args[1]})")
        elif kind == "segment":
            vt = self.readings[-1][0] if self.readings else 0.0
            self.markers.append((vt, f"seg {args[0]}"))
            self.redraw_plot()
        elif kind == "started":
            self.message(f"Titration started, writing {args[0]}")
        elif kind == "finished":
            reason, path = args
            self.message(f"Titration finished: {reason}. Data: {path}")
            self.file_var.set(self.default_file())
            self.manual_volume = self.readings[-1][0] if self.readings else 0.0
            messagebox.showinfo("Titration finished", f"{reason}\n\nData saved to {path}")

    def drain_log(self):
        n = 0
        try:
            while n < 300:
                e = self.log_queue.get_nowait()
                self.log_text.insert("end", transport.format_entry(e) + "\n")
                n += 1
        except queue.Empty:
            pass
        if n:
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 3000:
                self.log_text.delete("1.0", f"{lines - 3000}.0")
            self.log_text.see("end")

    def message(self, text, error=False):
        self.msg_text.insert("end", time.strftime("%H:%M:%S  ") + text + "\n", ("error",) if error else ())
        self.msg_text.see("end")

    def status(self, text):
        self.status_var.set(text)

    def _update_buttons(self):
        busy = self.ctl.busy
        connected = bool(self.ctl.instruments)
        idle_ok = ["!disabled"] if (not busy and connected) else ["disabled"]
        for b in (self.btn_start, self.btn_delayed, self.btn_measure, self.btn_monitor, self.btn_fill):
            b.state(idle_ok)
        self.btn_add.state(["!disabled"] if (self.titrating or (not busy and connected)) else ["disabled"])
        running = ["!disabled"] if self.titrating else ["disabled"]
        for b in (self.btn_pause, self.btn_pause2, self.btn_apply, self.btn_apply_m):
            b.state(running)
        self.btn_change.state(running)
        self.btn_stop.state(["!disabled"] if busy else ["disabled"])
        self.btn_stop_manual.state(["!disabled"] if busy and not self.titrating else ["disabled"])
        self.file_entry.state(["disabled"] if (self.titrating or self.monitoring) else ["!disabled"])
        self.btn_note.state(["!disabled"] if (self.titrating or self.monitoring) else ["disabled"])

    def on_close(self):
        if self.ctl.busy and not messagebox.askyesno("Quit", "A job is running. Stop it and quit?"):
            return
        self.ctl.stop()
        self.save_settings()
        time.sleep(0.3)
        for ins in self.ctl.instruments.values():
            try:
                ins.stop()
                ins.close()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    root.title(f"CrossTitrate {__version__}")
    root.geometry("1280x820")
    root.minsize(1000, 650)
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
