"""Dialog windows."""

import datetime as dt
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .. import diagnostics, transport
from ..titration import COMPONENTS, Burette
from .widgets import Form, FormError

LEVEL_ICON = {"ok": "✔", "info": "ℹ", "warning": "⚠", "fail": "✘"}
LEVEL_COLOR = {"ok": "#1a7f37", "info": "#555", "warning": "#9a6700", "fail": "#cf222e"}

SIMULATOR = "Simulator (no hardware)"


class Dialog(tk.Toplevel):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.result = None
        self.body = ttk.Frame(self, padding=10)
        self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def buttons(self, ok_text="OK", cancel_text="Cancel"):
        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        if cancel_text:
            ttk.Button(bar, text=cancel_text, command=self.cancel).pack(side="right")
        if ok_text:
            ttk.Button(bar, text=ok_text, command=self.ok).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.cancel())
        return bar

    def show(self):
        self.update_idletasks()
        p = self.master
        x = p.winfo_rootx() + max(0, (p.winfo_width() - self.winfo_width()) // 2)
        y = p.winfo_rooty() + max(0, (p.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.wait_window()
        return self.result

    def ok(self):
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()


class TextDialog(Dialog):
    def __init__(self, parent, title, text, width=90, height=28):
        super().__init__(parent, title)
        t = tk.Text(self.body, width=width, height=height, wrap="word", font=("TkFixedFont", 10))
        t.insert("1.0", text)
        t.config(state="disabled")
        sb = ttk.Scrollbar(self.body, command=t.yview)
        t.config(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.buttons("Close", None)


def setup_guide_text():
    return (
        "CONNECTING A METROHM TITRINO\n"
        "============================\n\n"
        + diagnostics.CABLE_HELP + "\n\n" + diagnostics.TITRINO_SETUP + "\n\n"
        "In this program:\n"
        "  1. Connect… (Titration tab): choose the serial port, keep 9600 8N1 (matching the Titrino),\n"
        "     and press 'Test connection'. The diagnostics explain what is wrong if it fails.\n"
        "  2. Burettes: each burette is driven by a connected instrument. A second burette can be a\n"
        "     second Titrino on another port (or burette D1/D2 on multi-burette models like the 751).\n"
        "  3. The instrument used for measuring the EMF is the one selected as 'Measuring instrument'.\n\n"
        "The program switches the Titrino into MEAS mode (mV) and uses its dispensing function\n"
        "itself - you don't need to select a mode or method on the instrument.\n\n"
        "USB: Titrinos are RS232 instruments. Use a USB-RS232 adapter; it appears as a COM port\n"
        "(Windows), /dev/ttyUSB0 (Linux) or /dev/cu.usbserial-… (macOS). Network serial servers\n"
        "(e.g. Moxa NPort) work too: enter socket://host:port as the port.\n"
    )


class ConnectDialog(Dialog):
    """Choose a port (or the simulator), set serial parameters, test, connect."""

    def __init__(self, parent, existing_keys, initial=None):
        super().__init__(parent, "Connect instrument")
        self.existing = existing_keys
        initial = initial or {}
        f = Form(self.body, label_width=16)
        f.frame.grid(row=0, column=0, sticky="nw")
        self.form = f
        f.add("name", "Name", str, width=18, tip="Short name for this instrument, e.g. 'Titrino 1'")
        row = f.row
        ttk.Label(f.frame, text="Port", width=16, anchor="w").grid(row=row, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(f.frame, textvariable=self.port_var, width=40)
        self.port_box.grid(row=row, column=1, columnspan=2, sticky="w")
        ttk.Button(f.frame, text="Refresh", command=self.refresh).grid(row=row, column=3, padx=4)
        f.row += 1
        f.add("baud", "Baud rate", str, choices=["300", "600", "1200", "2400", "4800", "9600", "19200",
                                                  "38400", "57600", "115200"])
        f.add("bytesize", "Data bits", str, choices=["7", "8"])
        f.add("parity", "Parity", str, choices=["none", "even", "odd"])
        f.add("stopbits", "Stop bits", str, choices=["1", "2"])
        f.add("handshake", "Handshake", str, choices=["none", "rtscts", "xonxoff"],
              tip="Leave at 'none'. With handshake HWs on the Titrino, keep 'Raise RTS/DTR' ticked.")
        f.add("assert_lines", "Raise RTS/DTR", bool,
              tip="Keeps the RTS and DTR lines on, which the Titrino needs to be allowed to send.")
        f.add("speed", "Simulation speed", str, choices=["1", "10", "60", "300"],
              tip="Only for the simulator: run time faster (e.g. 60 = one minute per second).")
        self.refresh()
        s = transport.SerialSettings.from_dict(initial.get("settings"))
        f.set("name", initial.get("name") or self._free_name())
        self.port_var.set(initial.get("port", self.port_box["values"][0] if self.port_box["values"] else ""))
        f.set("baud", str(s.baud))
        f.set("bytesize", str(s.bytesize))
        f.set("parity", {"N": "none", "E": "even", "O": "odd"}[s.parity])
        f.set("stopbits", str(s.stopbits))
        f.set("handshake", s.handshake)
        f.set("assert_lines", s.assert_lines)
        f.set("speed", str(initial.get("speed", 1)))

        res = ttk.LabelFrame(self.body, text="Diagnostics", padding=6)
        res.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.body.rowconfigure(1, weight=1)
        self.body.columnconfigure(0, weight=1)
        self.out = tk.Text(res, width=88, height=16, wrap="word", font=("TkDefaultFont", 9))
        self.out.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(res, command=self.out.yview)
        sb.pack(side="left", fill="y")
        self.out.config(yscrollcommand=sb.set)
        for lvl, col in LEVEL_COLOR.items():
            self.out.tag_config(lvl, foreground=col)
        self.out.tag_config("detail", lmargin1=24, lmargin2=24, foreground="#333")

        bar = self.buttons("Connect", "Cancel")
        self.test_btn = ttk.Button(bar, text="Test connection", command=self.test)
        self.test_btn.pack(side="left")
        ttk.Button(bar, text="Scan all ports", command=self.scan).pack(side="left", padx=6)
        ttk.Button(bar, text="Setup guide", command=lambda: TextDialog(self, "Setup guide",
                                                                       setup_guide_text()).show()).pack(side="left")
        self.ui_queue = queue.Queue()
        self._drain()
        self.port_var.trace_add("write", lambda *a: self._toggle_sim())
        self._toggle_sim()
        self.write([d for d in diagnostics.environment_checks()])

    def _drain(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain)

    def _free_name(self):
        i = 1
        while f"Titrino {i}" in self.existing:
            i += 1
        return f"Titrino {i}"

    def refresh(self):
        self.ports = transport.list_ports()
        values = [f"{p['device']}  —  {p['description']}" + (f" [{p['chip']}]" if p["chip"] else "")
                  for p in self.ports]
        self.port_box["values"] = [SIMULATOR] + values

    def _toggle_sim(self):
        sim = self.port_var.get() == SIMULATOR
        for k in ("baud", "bytesize", "parity", "stopbits", "handshake", "assert_lines"):
            self.form.enable(k, not sim)
        self.form.enable("speed", sim)

    def port(self):
        p = self.port_var.get().strip()
        return p if p == SIMULATOR else p.split("  —  ")[0].strip()

    def settings(self):
        f = self.form
        return transport.SerialSettings(int(f.get("baud")), int(f.get("bytesize")),
                                        {"none": "N", "even": "E", "odd": "O"}[f.get("parity")],
                                        int(f.get("stopbits")), f.get("handshake"), f.get("assert_lines"))

    def write(self, findings, clear=False):
        if clear:
            self.out.delete("1.0", "end")
        for x in findings:
            self.out.insert("end", f"{LEVEL_ICON[x.level]} {x.title}\n", x.level)
            if x.detail:
                self.out.insert("end", x.detail + "\n", "detail")
        self.out.see("end")

    def _run_bg(self, fn):
        self.test_btn.state(["disabled"])

        def runner():
            try:
                fn()
            finally:
                self.ui_queue.put(lambda: self.test_btn.state(["!disabled"]) if self.winfo_exists() else None)
        threading.Thread(target=runner, daemon=True).start()

    def progress(self, msg):
        self.ui_queue.put(lambda: (self.out.insert("end", f"   {msg}\n", "info"), self.out.see("end"))
                   if self.winfo_exists() else None)

    def test(self):
        port = self.port()
        if port == SIMULATOR:
            self.write([diagnostics.Finding("ok", "The simulator needs no connection test")], clear=True)
            return
        if not port:
            return
        settings = self.settings()
        self.out.delete("1.0", "end")

        def job():
            findings, good, ident = diagnostics.diagnose_port(port, settings, self.progress)
            self.ui_queue.put(lambda: self._test_done(findings, good))
        self._run_bg(job)

    def _test_done(self, findings, good):
        if not self.winfo_exists():
            return
        self.write(findings)
        if good and (good.baud, good.bytesize, good.parity, good.stopbits) != (
                self.settings().baud, self.settings().bytesize, self.settings().parity, self.settings().stopbits):
            if messagebox.askyesno("Use these settings?", f"The Titrino answered at {good.describe()}.\n"
                                   "Use these settings?", parent=self):
                self.form.set("baud", str(good.baud))
                self.form.set("bytesize", str(good.bytesize))
                self.form.set("parity", {"N": "none", "E": "even", "O": "odd"}[good.parity])
                self.form.set("stopbits", str(good.stopbits))
                self.form.set("handshake", "none")

    def scan(self):
        settings = self.settings()
        self.out.delete("1.0", "end")

        def job():
            results = diagnostics.scan_ports(settings, self.progress)
            fs = [diagnostics.Finding("ok" if ok else "info", f"{port}: {text}") for port, ok, text in results]
            if not results:
                fs = [diagnostics.Finding("warning", "No serial ports found")]
            self.ui_queue.put(lambda: self.write(fs) if self.winfo_exists() else None)
        self._run_bg(job)

    def ok(self):
        try:
            name = self.form.get("name").strip()
            if not name:
                raise FormError("Please give the instrument a name")
            if name in self.existing:
                raise FormError(f"'{name}' is already used")
            port = self.port()
            if not port:
                raise FormError("Choose a port")
            self.result = dict(name=name, port=port, settings=self.settings().to_dict(),
                               speed=float(self.form.get("speed") or 1))
        except FormError as e:
            messagebox.showerror("Connect", str(e), parent=self)
            return
        self.destroy()


class BuretteDialog(Dialog):
    def __init__(self, parent, burette, instruments, components, title="Burette"):
        super().__init__(parent, title)
        f = Form(self.body, label_width=20)
        f.frame.pack(fill="x")
        self.form = f
        f.add("name", "Name", str, width=20)
        f.add("device", "Instrument", str, choices=list(instruments), width=20)
        f.add("select", "Burette on instrument", str, choices=["", "internal D0", "external D1", "external D2"],
              tip="Only for multi-burette models (751, 785, 798, 799). Leave empty otherwise.")
        f.add("rate", "Dispensing rate", float, "mL/min")
        f.add("ratio", "Volume ratio", float, "",
              tip="When several burettes dispense together, each adds step × ratio.")
        f.section("Concentrations in the burette")
        for c, label in zip(COMPONENTS, components):
            f.add("c_" + c, label, float, "mM")
        f.set("name", burette.name)
        f.set("device", burette.device if burette.device in instruments else (list(instruments) or [""])[0])
        f.set("select", burette.select)
        f.set("rate", burette.rate)
        f.set("ratio", burette.ratio)
        for c in COMPONENTS:
            f.set("c_" + c, burette.concentrations.get(c, 0.0))
        self.buttons()

    def ok(self):
        f = self.form
        try:
            b = Burette(name=f.get("name") or "Burette", device=f.get("device"), select=f.get("select"),
                        rate=f.get("rate"), ratio=f.get("ratio"),
                        concentrations={c: f.get("c_" + c) for c in COMPONENTS})
            if not (0.01 <= b.rate <= 150):
                raise FormError("Dispensing rate must be 0.01–150 mL/min")
            if b.ratio <= 0:
                raise FormError("Volume ratio must be > 0")
        except FormError as e:
            messagebox.showerror("Burette", str(e), parent=self)
            return
        self.result = b
        self.destroy()


class ChangeBuretteDialog(Dialog):
    """Change active burette(s) (and their concentrations) during a titration."""

    def __init__(self, parent, burettes, active, method, components, instruments):
        super().__init__(parent, "Change burette")
        self.burettes = [Burette.from_dict(b.to_dict()) for b in burettes]
        self.components, self.instruments = components, instruments
        ttk.Label(self.body, text="Dispense from:", font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
        self.checks = []
        lst = ttk.Frame(self.body)
        lst.pack(fill="x", pady=4)
        for i, b in enumerate(self.burettes):
            var = tk.BooleanVar(value=i in active)
            row = ttk.Frame(lst)
            row.pack(fill="x")
            ttk.Checkbutton(row, text=f"{b.name}  ({b.device})", variable=var).pack(side="left")
            ttk.Button(row, text="Edit…", command=lambda i=i: self.edit(i)).pack(side="right")
            self.checks.append(var)
        ttk.Label(self.body, text="A new segment starts: the segment volume restarts at 0 and the new "
                                  "concentrations are written to the data file.",
                  wraplength=420, foreground="#555").pack(anchor="w", pady=(4, 8))
        f = Form(self.body, label_width=22)
        f.frame.pack(fill="x")
        self.form = f
        f.add("volume_step_ml", "Volume step", float, "mL")
        f.add("stop_volume_ml", "Stop volume (segment)", float, "mL")
        f.add("stop_emf_min", "Stop EMF min", float, "mV", optional=True)
        f.add("stop_emf_max", "Stop EMF max", float, "mV", optional=True)
        for k in ("volume_step_ml", "stop_volume_ml", "stop_emf_min", "stop_emf_max"):
            f.set(k, getattr(method, k))
        self.buttons("Change")

    def edit(self, i):
        b = BuretteDialog(self, self.burettes[i], self.instruments, self.components).show()
        if b:
            self.burettes[i] = b

    def ok(self):
        active = [i for i, v in enumerate(self.checks) if v.get()]
        if not active:
            messagebox.showerror("Change burette", "Select at least one burette.", parent=self)
            return
        try:
            fields = {k: self.form.get(k) for k in ("volume_step_ml", "stop_volume_ml", "stop_emf_min",
                                                    "stop_emf_max")}
        except FormError as e:
            messagebox.showerror("Change burette", str(e), parent=self)
            return
        self.result = (self.burettes, active, fields)
        self.destroy()


class DelayedStartDialog(Dialog):
    def __init__(self, parent):
        super().__init__(parent, "Delayed start")
        start = dt.datetime.now().replace(second=0, microsecond=0) + dt.timedelta(hours=1)
        ttk.Label(self.body, text="Start the titration at (YYYY-MM-DD HH:MM):").pack(anchor="w")
        self.var = tk.StringVar(value=start.strftime("%Y-%m-%d %H:%M"))
        ttk.Entry(self.body, textvariable=self.var, width=20).pack(anchor="w", pady=4)
        self.buttons("Schedule")

    def ok(self):
        try:
            t = dt.datetime.strptime(self.var.get().strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            messagebox.showerror("Delayed start", "Use the format YYYY-MM-DD HH:MM", parent=self)
            return
        if t <= dt.datetime.now():
            messagebox.showerror("Delayed start", "That time is in the past.", parent=self)
            return
        self.result = t.timestamp()
        self.destroy()
