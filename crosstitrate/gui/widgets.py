"""Small reusable Tk widgets: form fields and a live XY plot."""

import math
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk


class FormError(ValueError):
    pass


class Form:
    """Grid of labelled entry fields with typed getters/setters."""

    def __init__(self, parent, label_width=24):
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(3, weight=1)   # spare space goes to the right, units stay next to fields
        self.vars = {}
        self.types = {}
        self.widgets = {}
        self.labels = {}
        self.row = 0
        self.label_width = label_width

    def add(self, key, label, kind=float, unit="", width=10, choices=None, optional=False, tip="", var=None):
        """Add a field. Pass `var` (e.g. other_form.vars[key]) to share the value with another form."""
        lab = ttk.Label(self.frame, text=label, width=self.label_width, anchor="w")
        lab.grid(row=self.row, column=0, sticky="w", pady=1)
        if kind is bool:
            var = var or tk.BooleanVar()
            w = ttk.Checkbutton(self.frame, variable=var)
        elif choices:
            var = var or tk.StringVar()
            w = ttk.Combobox(self.frame, textvariable=var, values=choices, state="readonly", width=width)
        else:
            var = var or tk.StringVar()
            w = ttk.Entry(self.frame, textvariable=var, width=width)
        w.grid(row=self.row, column=1, sticky="w", pady=1)
        if unit:
            ttk.Label(self.frame, text=unit, foreground="#666").grid(row=self.row, column=2, sticky="w", padx=4)
        if tip:
            Tooltip(w, tip)
            Tooltip(lab, tip)
        self.vars[key], self.types[key], self.widgets[key], self.labels[key] = var, (kind, optional, label), w, lab
        self.row += 1
        return w

    def section(self, text):
        ttk.Label(self.frame, text=text, font=("TkDefaultFont", 9, "bold")).grid(
            row=self.row, column=0, columnspan=3, sticky="w", pady=(8, 2))
        self.row += 1

    def get(self, key):
        kind, optional, label = self.types[key]
        raw = self.vars[key].get()
        if kind is bool:
            return bool(raw)
        if kind is str:
            return raw
        raw = str(raw).strip().replace(",", ".")
        if raw == "":
            if optional:
                return None
            raise FormError(f"'{label}' is empty")
        try:
            return kind(float(raw)) if kind is int else kind(raw)
        except ValueError:
            raise FormError(f"'{label}': '{raw}' is not a number") from None

    def set(self, key, value):
        kind = self.types[key][0]
        if kind is bool:
            self.vars[key].set(bool(value))
        elif value is None:
            self.vars[key].set("")
        elif kind is float and isinstance(value, float):
            self.vars[key].set(f"{value:g}")
        else:
            self.vars[key].set(str(value))

    def enable(self, key, on):
        self.widgets[key].state(["!disabled"] if on else ["disabled"])
        self.labels[key].state(["!disabled"] if on else ["disabled"])


def autosize_columns(tree, padding=16):
    """Size every column of a ttk.Treeview to fit its heading and contents."""
    head = tkfont.nametofont("TkHeadingFont")
    body = tkfont.nametofont("TkDefaultFont")
    cols = list(tree["columns"])
    if "tree" in str(tree["show"]):
        cols.insert(0, "#0")
    items = tree.get_children()
    for col in cols:
        w = head.measure(tree.heading(col, "text")) + padding
        for iid in items:
            text = tree.item(iid, "text") if col == "#0" else tree.set(iid, col)
            w = max(w, body.measure(str(text)) + padding)
        tree.column(col, width=w, minwidth=w, stretch=False)


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _e=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 2
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0", relief="solid",
                 borderwidth=1, wraplength=360, padx=4, pady=2).pack()

    def hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def nice_ticks(lo, hi, n=6):
    span = hi - lo
    if span <= 0 or not math.isfinite(span):
        return [lo]
    raw = span / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    ticks, x = [], math.ceil(lo / step) * step
    while x <= hi + step * 1e-9:
        ticks.append(0.0 if abs(x) < step * 1e-9 else x)
        x += step
    return ticks


def fmt_tick(v, span):
    if span >= 50:
        return f"{v:.0f}"
    decimals = max(1, min(6, 1 - math.floor(math.log10(span / 5)))) if span > 0 else 2
    text = f"{v:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


class Plot(tk.Canvas):
    """Auto-scaling XY plot with several series and vertical markers."""

    PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 14, 36

    def __init__(self, master, xlabel="", ylabel="", **kw):
        super().__init__(master, background="white", highlightthickness=0, **kw)
        self.xlabel, self.ylabel = xlabel, ylabel
        self.series = {}     # name -> dict(points, color, line, dot)
        self.markers = []    # vertical dashed lines: (x, label) or (x, label, color)
        self.bind("<Configure>", lambda e: self.redraw())
        self._pending = False

    def set_series(self, name, points, color="#1f6fd1", line=True, dot=3):
        self.series[name] = dict(points=list(points), color=color, line=line, dot=dot)
        self.schedule()

    def clear(self):
        self.series.clear()
        self.markers.clear()
        self.schedule()

    def schedule(self):
        if not self._pending:
            self._pending = True
            self.after(50, self.redraw)

    def redraw(self):
        self._pending = False
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        x0, x1, y0, y1 = self.PAD_L, w - self.PAD_R, self.PAD_T, h - self.PAD_B
        if x1 - x0 < 30 or y1 - y0 < 30:
            return
        pts = [p for s in self.series.values() for p in s["points"]]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            xlo, xhi = min(xs + [m[0] for m in self.markers]), max(xs + [m[0] for m in self.markers])
            ylo, yhi = min(ys), max(ys)
        else:
            xlo, xhi, ylo, yhi = 0, 1, -100, 100
        if xhi - xlo < 1e-9:
            xhi = xlo + max(1.0, abs(xlo) * 0.1)
        if yhi - ylo < 1.0:     # don't magnify noise below the 0.1 mV resolution
            mid = (yhi + ylo) / 2
            ylo, yhi = mid - 0.5, mid + 0.5
        ypad = (yhi - ylo) * 0.07
        ylo, yhi = ylo - ypad, yhi + ypad
        xhi += (xhi - xlo) * 0.03

        tx = lambda x: x0 + (x - xlo) / (xhi - xlo) * (x1 - x0)
        ty = lambda y: y1 - (y - ylo) / (yhi - ylo) * (y1 - y0)
        grid, text, font = "#e6e6e6", "#444", ("TkDefaultFont", 8)
        for v in nice_ticks(ylo, yhi, max(2, (y1 - y0) // 45)):
            y = ty(v)
            self.create_line(x0, y, x1, y, fill=grid)
            self.create_text(x0 - 5, y, text=fmt_tick(v, yhi - ylo), anchor="e", fill=text, font=font)
        for v in nice_ticks(xlo, xhi, max(2, (x1 - x0) // 70)):
            x = tx(v)
            self.create_line(x, y0, x, y1, fill=grid)
            self.create_text(x, y1 + 4, text=fmt_tick(v, xhi - xlo), anchor="n", fill=text, font=font)
        self.create_rectangle(x0, y0, x1, y1, outline="#999")
        self.create_text((x0 + x1) / 2, h - 4, text=self.xlabel, anchor="s", fill=text, font=font)
        self.create_text(12, (y0 + y1) / 2, text=self.ylabel, angle=90, fill=text, font=font)
        for i, mk in enumerate(self.markers):
            xm, label = mk[0], mk[1]
            color = mk[2] if len(mk) > 2 else "#d08000"
            x = tx(xm)
            self.create_line(x, y0, x, y1, fill=color, dash=(4, 3))
            self.create_text(x + 3, y0 + 2 + 12 * (len(mk) > 2 and i % 2), text=label, anchor="nw", fill=color,
                             font=font)
        if not pts:
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="no data yet", fill="#999")
            return
        for s in self.series.values():
            coords = [(tx(x), ty(y)) for x, y in s["points"]]
            if s["line"] and len(coords) >= 2:
                self.create_line(*[c for xy in coords for c in xy], fill=s["color"], width=1.5)
            r = s["dot"]
            if r:
                for cx, cy in coords:
                    self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=s["color"], outline="")
