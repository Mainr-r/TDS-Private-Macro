"""
TDS Strategy - visual strategy editor + auto-runner for Roblox TDS.

Hotkeys (global):  F8 panic stop | F9 spam upgrade | F10 re-fire abilities | F11 start/stop run
Needs:  pip install pynput pillow pytesseract   (+ Tesseract OCR installed for live reading)
"""

import ctypes
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk
from pynput import keyboard

import tds_runner as rn

try:
    import tds_updater
except ImportError:
    tds_updater = None

BG, PANEL, FIELD = "#14161a", "#1c1f26", "#262a33"
FG, DIM = "#e6e8ee", "#8a90a0"
ACCENT, RED, GREEN, AMBER = "#5b8cff", "#ff5b5b", "#3ddc97", "#ffb454"
FONT, FONT_B = ("Segoe UI", 9), ("Segoe UI Semibold", 9)
CW, CH = 700, 394
REGION_COLORS = {"cash": "#ffd24d", "wave": "#4dd2ff", "result": "#ff7ad9"}
UI_PRESETS = ["upgrade_p1", "upgrade_p2", "ability1", "ability2", "target",
              "attack_switch", "unit", "skip", "sell"]
STEP_TYPES = ["place", "upgrade", "ui", "target", "skip", "sell"]
HINT = ("ui = ability / attack switch / unit set: Arg = UI point names, "
        "comma-separated.  target: Arg = clicks.  Repeat cd > 0 re-fires it "
        "every cd seconds (from Wave until Until).")


def num(v, cast=int, default=0):
    try:
        return cast(float(v))
    except (ValueError, TypeError):
        return default


class App:
    def __init__(self):
        self.q = queue.Queue()
        self.settings = rn.load_settings()
        self.timing = rn.Timing()
        self.game = rn.Game(self.timing)
        self.reader = rn.Reader(self.game, self.timing, self.settings)
        self.runner = None
        self.strat = self.blank()
        self.pil = self.tkimg = None
        self.dw, self.dh = CW, CH
        self.sel = self.drag = None
        self._loading = False
        self.micro = rn.Micro(self.game, lambda: self.settings,
                              lambda: self.strat, self.log)

        self.root = root = tk.Tk()
        root.title("TDS Strategy" + (f" · build {tds_updater.build_id()}"
                                     if tds_updater else ""))
        root.geometry("1120x700")
        root.minsize(1120, 700)
        root.configure(bg=BG)
        self.style()
        self.build()
        self.dark_title()
        self.refresh_all()
        self.log("Ready. " + ("OCR available." if self.reader.ok else
                              "OCR NOT available - install Tesseract (see Settings)."))
        self.kl = keyboard.Listener(on_press=self.on_key)
        self.kl.daemon = True
        self.kl.start()
        if tds_updater:
            tds_updater.start_background(lambda names: self.log(
                "Update ready (" + ", ".join(names) + ") - restart to apply"))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    # ------------------------------------------------------------ basics
    @staticmethod
    def blank():
        return dict(name="strategy", image="", pins=[], ui={}, regions={},
                    steps=[], micro="")

    def log(self, msg):
        self.q.put(("log", msg))

    def dark_title(self):
        try:
            self.root.update()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass

    def style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=DIM,
                     padding=(14, 6), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", FIELD)],
               foreground=[("selected", FG)])
        st.configure("Treeview", background=FIELD, fieldbackground=FIELD,
                     foreground=FG, borderwidth=0, rowheight=24)
        st.configure("Treeview.Heading", background=PANEL, foreground=DIM,
                     relief="flat")
        st.map("Treeview", background=[("selected", ACCENT)])
        st.configure("D.TCombobox", fieldbackground=FIELD, background=FIELD,
                     foreground=FG, arrowcolor=FG, bordercolor=FIELD,
                     lightcolor=FIELD, darkcolor=FIELD, padding=3)
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)

    # --------------------------------------------------------- widgets
    def btn(self, parent, text, cmd, color=FIELD, **kw):
        return tk.Button(parent, text=text, command=cmd, bg=color, fg="white",
                         activebackground=color, activeforeground="white",
                         relief="flat", bd=0, font=FONT_B, cursor="hand2",
                         padx=10, pady=4, **kw)

    def entry(self, parent, var, width=8):
        return tk.Entry(parent, textvariable=var, width=width, bg=FIELD,
                        fg=FG, insertbackground=FG, relief="flat", font=FONT)

    def combo(self, parent, var, values, width=10):
        return ttk.Combobox(parent, textvariable=var, values=values,
                            width=width, style="D.TCombobox", font=FONT)

    def label(self, parent, text, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", BG),
                        fg=kw.pop("fg", DIM), font=FONT, **kw)

    def build(self):
        r = self.root
        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", padx=10, pady=(10, 6))
        self.name = tk.StringVar(value="strategy")
        self.btn(top, "New", self.new, "#3a3f4b").pack(side="left")
        self.btn(top, "Open", self.open, "#3a3f4b").pack(side="left", padx=4)
        self.btn(top, "Save", self.save, "#2f9e6f").pack(side="left")
        self.entry(top, self.name, 22).pack(side="left", padx=8, ipady=4)
        self.btn(top, "Load screenshot", self.load_image, "#3a3f4b").pack(
            side="left")
        self.btn(top, "Capture from Roblox", self.capture, ACCENT).pack(
            side="left", padx=4)

        left = tk.Frame(r, bg=BG)
        left.pack(side="left", fill="y", padx=(10, 5))
        tb = tk.Frame(left, bg=BG)
        tb.pack(fill="x", pady=(0, 4))
        self.tool = tk.StringVar(value="pin")
        for text, val in (("Pin", "pin"), ("UI point", "ui"),
                          ("Cash box", "cash"), ("Wave box", "wave"),
                          ("Result box", "result")):
            tk.Radiobutton(tb, text=text, value=val, variable=self.tool,
                           indicatoron=0, bg=PANEL, fg=FG, selectcolor=ACCENT,
                           activebackground=PANEL, relief="flat", bd=0,
                           font=FONT, padx=10, pady=4).pack(side="left",
                                                           padx=(0, 3))
        self.ui_name = tk.StringVar(value="upgrade_p1")
        self.combo(tb, self.ui_name, UI_PRESETS, 14).pack(side="left", padx=6)
        self.canvas = tk.Canvas(left, width=CW, height=CH, bg="#0d0f12",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right)
        self.label(left, "Pin: click map to place / drag to move.  UI point: "
                   "click the on-screen button.  Boxes: drag.  Right-click "
                   "deletes.", wraplength=CW, justify="left").pack(
            anchor="w", pady=(4, 6))

        pe = tk.Frame(left, bg=PANEL)
        pe.pack(fill="x")
        self.pin_name, self.pin_slot = tk.StringVar(), tk.StringVar()
        self.label(pe, "Selected pin", bg=PANEL).pack(side="left", padx=8,
                                                      pady=8)
        self.entry(pe, self.pin_name, 18).pack(side="left", ipady=3)
        self.label(pe, "Tower slot key", bg=PANEL).pack(side="left", padx=8)
        self.entry(pe, self.pin_slot, 3).pack(side="left", ipady=3)
        self.btn(pe, "Delete pin", self.delete_sel, "#c93c3c").pack(
            side="right", padx=8)
        self.pin_name.trace_add("write", self.pin_edited)
        self.pin_slot.trace_add("write", self.pin_edited)

        right = tk.Frame(r, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(5, 10))
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        for title, fn in (("Steps", self.tab_steps), ("Run", self.tab_run),
                          ("Settings", self.tab_settings)):
            f = tk.Frame(nb, bg=BG)
            nb.add(f, text=title)
            fn(f)

    def tab_steps(self, f):
        cols = ("n", "type", "pin", "wave", "cost", "delay", "what")
        widths = (28, 60, 40, 44, 52, 44, 150)
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=11)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c.upper() if c == "n" else c.title())
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", padx=6, pady=6)
        self.tree.bind("<<TreeviewSelect>>", self.step_selected)

        g = tk.Frame(f, bg=BG)
        g.pack(fill="x", padx=6)
        self.f = {k: tk.StringVar(value=v) for k, v in (
            ("type", "place"), ("pin", ""), ("path", "1"), ("wave", "0"),
            ("cost", "0"), ("delay", "0"), ("cd", "0"), ("until", "0"),
            ("arg", ""))}
        self.pin_combo = self.combo(g, self.f["pin"], [], 12)
        rows = [
            [("Type", self.combo(g, self.f["type"], STEP_TYPES, 8)),
             ("Pin", self.pin_combo), ("Path", self.combo(
                 g, self.f["path"], ["1", "2"], 3))],
            [("Wave ≥", self.entry(g, self.f["wave"], 5)),
             ("Cash needed", self.entry(g, self.f["cost"], 7)),
             ("Delay s", self.entry(g, self.f["delay"], 5))],
            [("Repeat cd s", self.entry(g, self.f["cd"], 5)),
             ("Until wave", self.entry(g, self.f["until"], 5)),
             ("Arg", self.entry(g, self.f["arg"], 14))]]
        for ri, row in enumerate(rows):
            for ci, (text, w) in enumerate(row):
                self.label(g, text).grid(row=ri, column=ci * 2, sticky="w",
                                         padx=(0, 4), pady=3)
                w.grid(row=ri, column=ci * 2 + 1, sticky="w", padx=(0, 10))
        self.label(f, HINT, wraplength=420, justify="left").pack(
            anchor="w", padx=6, pady=6)
        bf = tk.Frame(f, bg=BG)
        bf.pack(fill="x", padx=6)
        for text, cmd, col in (("Add", self.step_add, "#2f9e6f"),
                               ("Update", self.step_update, ACCENT),
                               ("Remove", self.step_remove, "#c93c3c"),
                               ("▲", lambda: self.step_move(-1), "#3a3f4b"),
                               ("▼", lambda: self.step_move(1), "#3a3f4b")):
            self.btn(bf, text, cmd, col).pack(side="left", padx=(0, 4))

    def tab_run(self, f):
        top = tk.Frame(f, bg=BG)
        top.pack(fill="x", padx=8, pady=8)
        self.btn(top, "▶ Start (F11)", self.toggle_run, "#2f9e6f").pack(
            side="left")
        self.btn(top, "■ Stop (F8)", self.panic, "#c93c3c").pack(
            side="left", padx=6)
        self.grind = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="Grind loop", variable=self.grind, bg=BG,
                       fg=FG, selectcolor=FIELD, activebackground=BG,
                       activeforeground=FG, font=FONT, bd=0,
                       highlightthickness=0).pack(side="left", padx=8)
        self.label(top, "Max matches").pack(side="left")
        self.maxm = tk.StringVar(value="0")
        self.entry(top, self.maxm, 4).pack(side="left", padx=4, ipady=2)

        self.status = tk.Label(f, text="IDLE", bg=BG, fg=FG,
                               font=("Segoe UI Semibold", 13), anchor="w")
        self.status.pack(fill="x", padx=8)
        self.statline = self.label(f, "", anchor="w")
        self.statline.pack(fill="x", padx=8)
        self.label(f, "OCR: " + ("on" if self.reader.ok else "OFF"),
                   anchor="w").pack(fill="x", padx=8)

        mf = tk.Frame(f, bg=BG)
        mf.pack(fill="x", padx=8, pady=8)
        self.btn(mf, "Test screen read", self.test_read, "#3a3f4b").pack(
            side="left")
        self.btn(mf, "Test webhook", self.test_webhook, "#3a3f4b").pack(
            side="left", padx=6)

        self.label(f, "Micro abilities (F10)  name:cooldown, ...").pack(
            anchor="w", padx=8)
        self.micro_var = tk.StringVar()
        self.micro_var.trace_add(
            "write", lambda *_: self.strat.__setitem__(
                "micro", self.micro_var.get()))
        self.entry(f, self.micro_var, 40).pack(fill="x", padx=8, pady=(2, 8),
                                               ipady=3)
        self.logbox = tk.Text(f, height=14, bg=FIELD, fg=FG, relief="flat",
                              font=("Consolas", 9), wrap="word", bd=0)
        self.logbox.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.logbox.config(state="disabled")

    SETTING_FIELDS = [
        ("webhook", "Discord webhook URL", ""), ("place_id", "Place ID", ""),
        ("tesseract_path", "Tesseract .exe", "exe"),
        ("lobby_macro", "Lobby macro (.json)", "json"),
        ("restart_macro", "Restart macro (.json)", "json"),
        ("win_words", "Win words", ""), ("lose_words", "Lose words", ""),
        ("drop_words", "Disconnect words", ""),
        ("upgrade_key", "Upgrade key", ""), ("sell_key", "Sell key", ""),
        ("end_wait", "Wait after match (s)", ""),
        ("join_wait", "Wait after join (s)", ""),
        ("start_delay", "Start delay, no OCR (s)", ""),
        ("match_timeout", "Match start timeout (s)", "")]

    def tab_settings(self, f):
        self.sv = {}
        g = tk.Frame(f, bg=BG)
        g.pack(fill="x", padx=8, pady=8)
        for i, (key, text, browse) in enumerate(self.SETTING_FIELDS):
            self.label(g, text).grid(row=i, column=0, sticky="w", pady=3)
            self.sv[key] = tk.StringVar(value=str(self.settings.get(key, "")))
            self.entry(g, self.sv[key], 30).grid(row=i, column=1, padx=6,
                                                 ipady=3, sticky="we")
            if browse:
                self.btn(g, "…", lambda k=key, b=browse: self.browse(k, b),
                         "#3a3f4b").grid(row=i, column=2)
        self.btn(g, "Save settings", self.save_settings, "#2f9e6f").grid(
            row=len(self.SETTING_FIELDS), column=1, sticky="w", pady=10)
        self.btn(g, "Check for updates", self.check_updates,
                 "#3a3f4b").grid(row=len(self.SETTING_FIELDS) + 1, column=1,
                                 sticky="w")
        self.label(f, "Keys and OCR words depend on the game's current UI - "
                   "verify them in-game with 'Test screen read'.",
                   wraplength=420, justify="left").pack(anchor="w", padx=8)

    def check_updates(self):
        def work():
            if not tds_updater:
                self.log("Updater not available")
                return
            changed, err = tds_updater.check_update()
            if err:
                self.log(f"Update check: {err}")
            elif changed:
                self.log("Update ready (" + ", ".join(changed)
                         + ") - restart to apply")
            else:
                self.log("Already up to date")
        threading.Thread(target=work, daemon=True).start()

    def browse(self, key, kind):
        types = [("JSON", "*.json")] if kind == "json" else [("Program", "*.exe")]
        p = filedialog.askopenfilename(filetypes=types)
        if p:
            self.sv[key].set(p)

    def save_settings(self):
        for k, v in self.sv.items():
            self.settings[k] = v.get().strip()
        rn.save_settings(self.settings)
        self.reader.configure(self.settings)
        self.log("Settings saved. OCR " + ("available" if self.reader.ok
                                            else "not available"))

    # ----------------------------------------------------------- canvas
    def set_image(self, img):
        self.pil = img.convert("RGB")
        sc = min(CW / img.width, CH / img.height)
        self.dw, self.dh = int(img.width * sc), int(img.height * sc)
        self.tkimg = ImageTk.PhotoImage(
            self.pil.resize((self.dw, self.dh), Image.LANCZOS))
        self.canvas.config(width=self.dw, height=self.dh)
        self.redraw()

    def redraw(self):
        c = self.canvas
        c.delete("all")
        if not self.tkimg:
            c.create_text(CW // 2, CH // 2, fill=DIM, font=FONT_B,
                          text="Load a screenshot or capture from Roblox")
            return
        c.create_image(0, 0, anchor="nw", image=self.tkimg)
        for k, (x1, y1, x2, y2) in self.strat["regions"].items():
            col = REGION_COLORS.get(k, FG)
            c.create_rectangle(x1 * self.dw, y1 * self.dh, x2 * self.dw,
                               y2 * self.dh, outline=col, width=2)
            c.create_text(x1 * self.dw + 3, y1 * self.dh - 8, text=k,
                          fill=col, anchor="w", font=FONT_B)
        for name, (x, y) in self.strat["ui"].items():
            px, py = x * self.dw, y * self.dh
            c.create_polygon(px, py - 7, px + 7, py, px, py + 7, px - 7, py,
                             fill=GREEN, outline="black")
            c.create_text(px + 10, py, text=name, fill=GREEN, anchor="w",
                          font=("Segoe UI", 8))
        for p in self.strat["pins"]:
            px, py = p["x"] * self.dw, p["y"] * self.dh
            col = AMBER if p["id"] == self.sel else ACCENT
            c.create_oval(px - 10, py - 10, px + 10, py + 10, fill=col,
                          outline="black", width=2)
            c.create_text(px, py, text=str(p["id"]), fill="white",
                          font=FONT_B)

    def frac(self, e):
        return (min(max(e.x / self.dw, 0), 1), min(max(e.y / self.dh, 0), 1))

    def nearest_pin(self, x, y, r=14):
        best = None
        for p in self.strat["pins"]:
            d = ((p["x"] * self.dw - x) ** 2 + (p["y"] * self.dh - y) ** 2) ** .5
            if d <= r and (best is None or d < best[0]):
                best = (d, p)
        return best[1] if best else None

    def on_press(self, e):
        if not self.pil:
            return
        fx, fy = self.frac(e)
        tool = self.tool.get()
        if tool == "pin":
            hit = self.nearest_pin(e.x, e.y)
            if hit:
                self.select_pin(hit["id"])
                self.drag = ("pin", hit)
            else:
                nid = max([p["id"] for p in self.strat["pins"]],
                          default=0) + 1
                self.strat["pins"].append(
                    dict(id=nid, name=f"Tower {nid}", slot="1", x=fx, y=fy))
                self.select_pin(nid)
                self.refresh_pins()
        elif tool == "ui":
            name = self.ui_name.get().strip()
            if name:
                self.strat["ui"][name] = [fx, fy]
        else:
            self.drag = ("region", tool, fx, fy)
        self.redraw()

    def on_drag(self, e):
        if not self.drag:
            return
        fx, fy = self.frac(e)
        if self.drag[0] == "pin":
            self.drag[1]["x"], self.drag[1]["y"] = fx, fy
            self.redraw()
        else:
            self.canvas.delete("tmp")
            self.canvas.create_rectangle(
                self.drag[2] * self.dw, self.drag[3] * self.dh, e.x, e.y,
                outline=REGION_COLORS[self.drag[1]], dash=(4, 3), tags="tmp")

    def on_release(self, e):
        if self.drag and self.drag[0] == "region":
            fx, fy = self.frac(e)
            x0, y0 = self.drag[2], self.drag[3]
            if abs(fx - x0) > 0.005 and abs(fy - y0) > 0.005:
                self.strat["regions"][self.drag[1]] = [
                    min(x0, fx), min(y0, fy), max(x0, fx), max(y0, fy)]
            self.redraw()
        self.drag = None

    def on_right(self, e):
        hit = self.nearest_pin(e.x, e.y)
        if hit:
            self.delete_pin(hit["id"])
            return
        for name, (x, y) in list(self.strat["ui"].items()):
            if abs(x * self.dw - e.x) < 14 and abs(y * self.dh - e.y) < 14:
                del self.strat["ui"][name]
                self.redraw()
                return

    # -------------------------------------------------------------- pins
    def select_pin(self, pid):
        self.sel = pid
        p = next((q for q in self.strat["pins"] if q["id"] == pid), None)
        self._loading = True
        self.pin_name.set(p["name"] if p else "")
        self.pin_slot.set(p["slot"] if p else "")
        self._loading = False
        self.redraw()

    def pin_edited(self, *_):
        if self._loading:
            return
        p = next((q for q in self.strat["pins"] if q["id"] == self.sel), None)
        if p:
            p["name"], p["slot"] = self.pin_name.get(), self.pin_slot.get()
            self.refresh_pins()

    def delete_sel(self):
        if self.sel:
            self.delete_pin(self.sel)

    def delete_pin(self, pid):
        self.strat["pins"] = [p for p in self.strat["pins"] if p["id"] != pid]
        self.strat["steps"] = [s for s in self.strat["steps"]
                               if s.get("pin") != pid]
        if self.sel == pid:
            self.sel = None
            self.select_pin(0)
        self.refresh_pins()
        self.refresh_steps()
        self.redraw()

    def refresh_pins(self):
        self.pin_combo["values"] = [f"{p['id']}: {p['name']}"
                                    for p in self.strat["pins"]]
        self.refresh_steps()

    # ------------------------------------------------------------- steps
    def refresh_steps(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        names = {p["id"]: p["name"] for p in self.strat["pins"]}
        for i, s in enumerate(self.strat["steps"]):
            t = s["type"]
            what = {"place": names.get(s.get("pin"), "?"),
                    "upgrade": f"{names.get(s.get('pin'), '?')} path {s.get('path', 1)}",
                    "ui": f"{names.get(s.get('pin'), '?')}: {s.get('arg', '')}",
                    "target": f"{names.get(s.get('pin'), '?')} x{s.get('arg') or 1}",
                    "skip": "skip wave",
                    "sell": names.get(s.get("pin"), "?")}[t]
            if s.get("cd"):
                what += f" ⟳{s['cd']}s"
            self.tree.insert("", "end", iid=str(i), values=(
                i + 1, t, s.get("pin") or "-", s.get("wave", 0),
                s.get("cost", 0), s.get("delay", 0), what))
        if sel and sel[0] in self.tree.get_children():
            self.tree.selection_set(sel[0])

    def form_step(self):
        pin_txt = self.f["pin"].get()
        pin = num(pin_txt.split(":")[0], int, None) if pin_txt else None
        return dict(type=self.f["type"].get(), pin=pin,
                    path=num(self.f["path"].get(), int, 1),
                    wave=num(self.f["wave"].get()),
                    cost=num(self.f["cost"].get()),
                    delay=num(self.f["delay"].get(), float, 0),
                    cd=num(self.f["cd"].get(), float, 0),
                    until=num(self.f["until"].get()),
                    arg=self.f["arg"].get().strip())

    def step_selected(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        s = self.strat["steps"][int(sel[0])]
        p = next((q for q in self.strat["pins"] if q["id"] == s.get("pin")),
                 None)
        self.f["pin"].set(f"{p['id']}: {p['name']}" if p else "")
        for k in ("type", "path", "wave", "cost", "delay", "cd", "until",
                  "arg"):
            self.f[k].set(str(s.get(k, "")))

    def step_add(self):
        s = self.form_step()
        if s["type"] != "skip" and s["pin"] is None:
            messagebox.showinfo("TDS Strategy", "Pick a pin for this step.")
            return
        self.strat["steps"].append(s)
        self.refresh_steps()

    def step_update(self):
        sel = self.tree.selection()
        if sel:
            self.strat["steps"][int(sel[0])] = self.form_step()
            self.refresh_steps()

    def step_remove(self):
        sel = self.tree.selection()
        if sel:
            del self.strat["steps"][int(sel[0])]
            self.refresh_steps()

    def step_move(self, d):
        sel = self.tree.selection()
        if not sel:
            return
        i, j = int(sel[0]), int(sel[0]) + d
        st = self.strat["steps"]
        if 0 <= j < len(st):
            st[i], st[j] = st[j], st[i]
            self.refresh_steps()
            self.tree.selection_set(str(j))

    # ---------------------------------------------------------- file ops
    def refresh_all(self):
        self.name.set(self.strat.get("name", "strategy"))
        self.micro_var.set(self.strat.get("micro", ""))
        self.select_pin(0)
        self.refresh_pins()
        self.redraw()

    def new(self):
        self.strat = self.blank()
        self.pil = self.tkimg = None
        self.dw, self.dh = CW, CH
        self.canvas.config(width=CW, height=CH)
        self.refresh_all()

    def save(self):
        name = re.sub(r"[^\w \-]", "", self.name.get()).strip() or "strategy"
        d = rn.app_dir("strategies")
        self.strat["name"] = name
        if self.pil:
            self.pil.save(os.path.join(d, name + ".png"))
            self.strat["image"] = name + ".png"
        with open(os.path.join(d, name + ".json"), "w") as f:
            json.dump(self.strat, f, indent=1)
        self.log(f"Saved strategy '{name}'")

    def open(self):
        d = rn.app_dir("strategies")
        p = filedialog.askopenfilename(initialdir=d, filetypes=[("Strategy", "*.json")])
        if not p:
            return
        try:
            with open(p) as f:
                s = json.load(f)
        except Exception as e:
            messagebox.showerror("TDS Strategy", f"Couldn't open: {e}")
            return
        self.strat = {**self.blank(), **s}
        self.pil = self.tkimg = None
        img = os.path.join(os.path.dirname(p), self.strat.get("image", ""))
        if self.strat.get("image") and os.path.exists(img):
            self.set_image(Image.open(img))
        self.refresh_all()

    def load_image(self):
        p = filedialog.askopenfilename(
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp")])
        if p:
            self.set_image(Image.open(p))

    def capture(self):
        self.log("Capturing in 3 seconds - switch to Roblox")
        self.root.iconify()
        self.root.after(3500, self.do_capture)

    def do_capture(self):
        img = self.game.grab()
        self.root.deiconify()
        if img is None:
            messagebox.showinfo("TDS Strategy", "Roblox window not found.")
            return
        self.set_image(img)

    # --------------------------------------------------------------- run
    def toggle_run(self):
        if self.runner and self.runner.is_alive():
            self.runner.stop()
            return
        self.save_settings()
        if not self.strat["pins"] or not self.strat["steps"]:
            messagebox.showinfo("TDS Strategy", "Add pins and steps first.")
            return
        if not self.reader.ok:
            self.log("Warning: no OCR - waves/cash/results can't be read")
        self.runner = rn.Runner(
            json.loads(json.dumps(self.strat)), dict(self.settings),
            self.log, self.game, self.reader, self.timing,
            self.grind.get(), num(self.maxm.get()))
        self.runner.start()
        self.log("Run started")

    def panic(self):
        if self.runner and self.runner.is_alive():
            self.runner.stop()
        self.micro.stop_all()
        self.log("Stop requested")

    def test_read(self):
        def work():
            if not self.game.rect():
                self.log("Roblox window not found")
                return
            if not self.reader.ok:
                self.log("OCR unavailable - install Tesseract, set its path")
                return
            r = self.strat["regions"]
            self.log(f"Read: wave={self.reader.number(r.get('wave'), True)}  "
                     f"cash={self.reader.number(r.get('cash'))}")
            if r.get("result"):
                self.log("Result box text: " +
                         repr(self.reader.text(r["result"]).strip()[:80]))
        threading.Thread(target=work, daemon=True).start()

    def test_webhook(self):
        self.save_settings()
        url = self.settings.get("webhook", "")
        if not url:
            self.log("Set a webhook URL in Settings first")
            return

        def work():
            err = rn.post_webhook(url, {"title": "TDS Strategy test",
                                        "description": "Webhook works.",
                                        "color": 0x5B8CFF})
            self.log("Webhook OK" if not err else f"Webhook failed: {err}")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ events
    def on_key(self, key):
        a = {keyboard.Key.f8: "panic", keyboard.Key.f9: "spam",
             keyboard.Key.f10: "abil", keyboard.Key.f11: "run"}.get(key)
        if a:
            self.q.put(("hot", a))

    def poll(self):
        while True:
            try:
                kind, text = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.logbox.config(state="normal")
                self.logbox.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
                self.logbox.see("end")
                self.logbox.config(state="disabled")
                if text.startswith("Update ready"):
                    self.root.title("TDS Strategy - update ready, restart to apply")
            elif text == "panic":
                self.panic()
            elif text in ("spam", "abil"):
                self.micro.toggle(text)
            elif text == "run":
                self.toggle_run()
        rn_ = self.runner
        if rn_ and rn_.is_alive():
            st = rn_.stats
            self.status.config(text=rn_.state.upper(), fg=GREEN)
            self.statline.config(
                text=f"wave {rn_.wave}  cash {rn_.cash if rn_.cash is not None else '-'}"
                     f"   W {st['wins']}  L {st['losses']}  DC {st['drops']}"
                     f"   timing x{self.timing.scale:.2f}")
        else:
            self.status.config(text="IDLE", fg=FG)
        self.root.after(200, self.poll)

    def close(self):
        self.panic()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
