"""
TDS Strategy - visual strategy editor + auto-runner for Roblox TDS.

Hotkeys (global):  F8 panic stop | F9 spam upgrade | F10 re-fire abilities | F11 start/stop run
Needs:  pip install pynput pillow pytesseract   (+ Tesseract OCR installed for live reading)

Your settings, strategies, macros and unsaved work live in
%APPDATA%\\TDSStrategy, so app updates never touch them.
"""

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk
from pynput import keyboard

import tds_macro as tm
import tds_runner as rn

try:
    import tds_updater
except ImportError:
    tds_updater = None

BG, PANEL, FIELD = "#14161a", "#1c1f26", "#262a33"
FG, DIM = "#e6e8ee", "#8a90a0"
ACCENT, RED, GREEN, AMBER = "#5b8cff", "#ff5b5b", "#3ddc97", "#ffb454"
FONT, FONT_B = ("Segoe UI", 9), ("Segoe UI Semibold", 9)
CW, CH = 700, 394  # default canvas size before it is measured
REGION_COLORS = {"cash": "#ffd24d", "wave": "#4dd2ff", "result": "#ff7ad9"}
UI_PRESETS = ["upgrade_p1", "upgrade_p2", "ability1", "ability2", "target",
              "attack_switch", "unit", "skip", "sell"]
STEP_TYPES = ["place", "upgrade", "ui", "target", "skip", "sell"]
HINT = ("ui = ability / attack switch / unit set: Arg = UI point names, "
        "comma-separated.  target: Arg = clicks.  Repeat cd > 0 re-fires it "
        "every cd seconds (from Wave until Until).")

# Text for the "!" badges: what it does + how to set it up.
HELP = dict(
    save="Saves this strategy (pins, UI points, boxes, steps + screenshot) "
         "to your strategies folder. Your work is also auto-saved every few "
         "seconds and restored next launch, even after an update.",
    capture="Minimises this window, waits 3 s, then screenshots the Roblox "
            "window. Switch to Roblox first and show the map with your "
            "tower bar visible. Positions are stored as fractions, so the "
            "strategy works at any Roblox window size.",
    undo="Ctrl+Z also works. Undoes the last pin / UI point / box / step "
         "change (up to 50).",
    tools="What a click on the screenshot does.\n"
          "Pin: a tower spot (click to add, drag to move).\n"
          "UI point: a game button - choose its name in the box, then click "
          "that button on the screenshot.\nRight-click deletes a pin or UI "
          "point. (Cash/Wave/Result boxes are in Settings.)",
    boxes="The app reads the game by OCR from three boxes. Press Select, "
          "then drag a rectangle over that text directly on your screen "
          "(Roblox must be visible - use windowed or borderless "
          "fullscreen; Esc cancels). Boxes are saved with your settings "
          "and scale with the Roblox window.\n"
          "Cash: your money number. Wave: the wave counter. Result: where "
          "the win/lose/disconnect text appears.",
    ui_name="Name for the next UI point you click. upgrade_p1/p2, skip, "
            "sell, target are used automatically by steps; ability1/2, "
            "attack_switch, unit are used by 'ui' steps and Micro.",
    pin="Label for the selected pin (shown in the step list).",
    slot="Hotbar key that selects this tower before placing (e.g. 1-5). "
         "Used by 'place' steps.",
    type="place: put the tower on its pin.\nupgrade: upgrade it (see Path).\n"
         "ui: click UI points (abilities, attack switch...).\n"
         "target: cycle targeting.\nskip: press the skip-wave button.\n"
         "sell: sell the tower.",
    stpin="Which pin this step acts on. Not needed for 'skip'.",
    path="Upgrade path 1 or 2. Clicks the upgrade_p1 / upgrade_p2 UI point "
         "if set, otherwise presses the Upgrade key from Settings.",
    wave="Step waits until the game reaches this wave. Needs OCR and the "
         "Wave box. 0 = no wave condition.",
    cost="Cash needed. The step waits until you have this much, then checks "
         "that cash really dropped and retries if not. 0 = no check.",
    delay="Extra seconds to wait once the wave/cash conditions are met.",
    cd="Repeat every this many seconds (for abilities). 0 = run once, in "
       "list order. Repeating steps run alongside the main list.",
    until="Stop repeating after this wave. 0 = never.",
    arg="ui step: UI point names, comma-separated (e.g. ability1,ability2).\n"
        "target step: number of clicks.",
    stepbtns="Add = append the form as a new step. Select a row, edit the "
             "form, press Update to change it. Duplicate copies a row. "
             "Arrows reorder - steps run top to bottom, each waiting for "
             "its wave/cash. Delete key removes the selected row.",
    grind="Keep going after each match: waits, plays your Restart macro, "
          "then queues the next match. Off = a single match.",
    maxm="Stop after this many matches. 0 = unlimited.",
    hotkeys="F8 stop everything | F9 toggle upgrade-key spam | F10 toggle "
            "ability re-fire (Micro box) | F11 start/stop run. They work "
            "while Roblox is focused.",
    testread="Reads the Wave and Cash boxes right now and logs what OCR "
             "sees (plus the Result box text). Open Roblox first. If it "
             "reads wrong, re-select the boxes (Settings) tighter around "
             "the digits.",
    testhook="Sends a test message to your Discord webhook (Settings).",
    micro="For hand-play: abilities F10 re-clicks on a timer, written "
          "name:seconds, e.g. ability1:30, ability2:45. Names are your UI "
          "points. F9 spams the Upgrade key.",
    s_webhook="Discord webhook for match results + screenshots. In Discord: "
              "channel settings > Integrations > Webhooks > New > Copy URL. "
              "Blank = off.",
    s_place_id="Roblox place ID used to rejoin after a disconnect (the "
               "number in the game's URL).",
    s_tesseract_path="Path to tesseract.exe if OCR shows unavailable. "
                     "Install Tesseract OCR, then Browse to it. Default "
                     "install folders are found automatically.",
    s_lobby_macro="Macro (from the TDS Macro recorder) played after "
                  "rejoining, to walk into a match / pick the map.",
    s_restart_macro="Macro played between matches to queue the next one "
                    "(e.g. clicking Play Again).",
    s_win_words="Comma-separated words meaning you won, matched (lowercase) "
                "against the Result box text.",
    s_lose_words="Comma-separated words meaning you lost, matched against "
                 "the Result box text.",
    s_drop_words="Words meaning you were disconnected - triggers a rejoin.",
    s_upgrade_key="Key that upgrades a selected tower when no upgrade UI "
                  "point is set (default E).",
    s_sell_key="Key that sells a selected tower when no sell UI point is "
               "set (default X).",
    s_end_wait="Seconds to wait after a match ends before the Restart macro "
               "runs.",
    s_join_wait="Seconds to wait after Roblox opens before the Lobby macro "
                "runs. Raise on slow PCs.",
    s_start_delay="Without OCR the app can't see the match start, so it "
                  "waits this long instead.",
    s_match_timeout="If no wave is read within this many seconds the match "
                    "is treated as failed and the game is rejoined.",
    s_misc="")

GUIDE = (
    "QUICK START\n"
    "1. Capture from Roblox (top bar) with the map and tower bar visible.\n"
    "2. Pin tool: click each tower spot. Name it and set its Tower slot "
    "key (hotbar number).\n"
    "3. UI point tool: pick a name (upgrade_p1, skip, sell...) and click "
    "that button on the screenshot.\n"
    "4. Settings > Screen boxes: press Select for Cash, Wave and Result "
    "and drag over that text on your screen (Roblox visible). Install "
    "Tesseract OCR so they can be read.\n"
    "5. Steps tab: add steps in order (place, upgrade...). Use Wave and "
    "Cash needed so each step waits for the right moment.\n"
    "6. Run tab: 'Test screen read' to check OCR, then Start (F11).\n\n"
    "HOTKEYS\nF8 stop all | F9 spam upgrade | F10 re-fire abilities | "
    "F11 start/stop run | Ctrl+Z undo\n\n"
    "YOUR DATA\nSettings, strategies, macros and unsaved work are kept in "
    "%APPDATA%\\TDSStrategy and survive app updates. Use 'Open data "
    "folder' to back them up or share strategies.")


def num(v, cast=int, default=0):
    try:
        return cast(float(v))
    except (ValueError, TypeError):
        return default


class App:
    def __init__(self):
        tm.migrate_legacy()
        self.q = queue.Queue()
        self.settings = rn.load_settings()
        self.timing = rn.Timing()
        self.game = rn.Game(self.timing)
        self.reader = rn.Reader(self.game, self.timing, self.settings)
        self.runner = None
        self.strat = self.blank()
        self.pil = self.tkimg = None
        self.cw, self.ch = CW, CH
        self.dw, self.dh, self.ox, self.oy = CW, CH, 0, 0
        self.sel = self.drag = None
        self._loading = False
        self._rz = self._settings_job = None
        self._img_dirty = False
        self._last_draft = self._last_state = None
        self._tess = self.settings.get("tesseract_path", "")
        self.undo = []
        self.state = tm.load_json("state.json", {})
        self.micro = rn.Micro(self.game, lambda: self.settings,
                              lambda: self.strat, self.log)

        self.root = root = tk.Tk()
        self.sc = tm.ui_scale(root)
        root.title("TDS Strategy" + (f" · build {tds_updater.build_id()}"
                                     if tds_updater else ""))
        root.configure(bg=BG)
        self.setup_geometry()
        self.style()
        self.build()
        self.dark_title()
        tm.enable_wheel(root)
        self.restore_state()
        self.restore_draft()
        self.log("Ready. " + ("OCR available." if self.reader.ok else
                              "OCR NOT available - install Tesseract (see Settings)."))
        self.kl = keyboard.Listener(on_press=self.on_key)
        self.kl.daemon = True
        self.kl.start()
        if tds_updater:
            tds_updater.start_background(lambda names: self.log(
                "Update ready (" + ", ".join(names) + ") - restart to apply"))
        root.bind("<Control-z>", self.do_undo)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(5000, self.tick)
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

    def setup_geometry(self):
        """Size the window from the screen + Windows scaling, and restore the
        last size/position if it still fits on screen."""
        r, sc = self.root, self.sc
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        r.minsize(min(int(760 * sc), sw), min(int(520 * sc), sh))
        w, h = min(int(1120 * sc), sw - 40), min(int(700 * sc), sh - 90)
        x, y = (sw - w) // 2, max(0, (sh - h) // 3)
        m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$",
                     str(self.state.get("geometry", "")))
        if m:
            gw, gh, gx, gy = (int(v) for v in m.groups())
            if (gw >= 400 and gh >= 300 and gw <= sw + 20 and gh <= sh + 20
                    and -gw + 120 < gx < sw - 120 and 0 <= gy < sh - 120):
                w, h, x, y = gw, gh, gx, gy
        r.geometry(f"{w}x{h}{x:+d}{y:+d}")

    def style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=DIM,
                     padding=(12, 6), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", FIELD)],
               foreground=[("selected", FG)])
        st.configure("Treeview", background=FIELD, fieldbackground=FIELD,
                     foreground=FG, borderwidth=0,
                     rowheight=int(24 * self.sc))
        st.configure("Treeview.Heading", background=PANEL, foreground=DIM,
                     relief="flat")
        st.map("Treeview", background=[("selected", ACCENT)])
        st.configure("D.TCombobox", fieldbackground=FIELD, background=FIELD,
                     foreground=FG, arrowcolor=FG, bordercolor=FIELD,
                     lightcolor=FIELD, darkcolor=FIELD, padding=3)
        st.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG,
                     bordercolor=BG, arrowcolor=FG, lightcolor=FIELD,
                     darkcolor=FIELD)
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

    def info(self, parent, key):
        return tm.info(parent, HELP[key])

    def wrapped(self, parent, text, **kw):
        """Label whose text re-wraps to its width, so it never gets cut off."""
        lbl = self.label(parent, text, justify="left", anchor="w",
                         wraplength=int(300 * self.sc), **kw)
        lbl.bind("<Configure>", lambda e: lbl.configure(
            wraplength=max(60, e.width - 6)))
        return lbl

    def cell(self, flow, text, key, make, bg=BG):
        """label + '!' + input, as one wrap-able unit inside a Flow."""
        c = tk.Frame(flow, bg=bg)
        self.label(c, text, bg=bg).pack(side="left")
        self.info(c, key).pack(side="left", padx=(3, 6))
        make(c).pack(side="left")
        flow.add(c)

    def build(self):
        r, sc = self.root, self.sc
        r.columnconfigure(0, weight=1)
        r.rowconfigure(1, weight=1)

        tb = self.topbar = tm.Flow(r)
        tb.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        self.name = tk.StringVar(value="strategy")
        for text, cmd, col in (("New", self.new, "#3a3f4b"),
                               ("Open", self.open, "#3a3f4b"),
                               ("Save", self.save, "#2f9e6f")):
            tb.add(self.btn(tb, text, cmd, col))
        tb.add(self.entry(tb, self.name, 18))
        tb.add(self.info(tb, "save"))
        tb.add(self.btn(tb, "Load screenshot", self.load_image, "#3a3f4b"))
        tb.add(self.btn(tb, "Capture from Roblox", self.capture, ACCENT))
        tb.add(self.info(tb, "capture"))
        tb.add(self.btn(tb, "↶ Undo", self.do_undo, "#3a3f4b"))
        tb.add(self.info(tb, "undo"))
        self.upd_btn = self.btn(tb, "⟳ Update ready - restart",
                                self.restart, "#c27d12")
        tb.add(self.upd_btn, visible=False)

        body = tk.Frame(r, bg=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=3, minsize=int(360 * sc))
        body.columnconfigure(1, weight=2, minsize=int(320 * sc))
        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        # ---- left: tools, canvas (auto-fits), pin editor
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        tl = tm.Flow(left)
        tl.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.tool = tk.StringVar(value=self.state.get("tool", "pin"))
        for text, val in (("Pin", "pin"), ("UI point", "ui")):
            tl.add(tk.Radiobutton(tl, text=text, value=val,
                                  variable=self.tool, indicatoron=0,
                                  bg=PANEL, fg=FG, selectcolor=ACCENT,
                                  activebackground=PANEL, relief="flat",
                                  bd=0, font=FONT, padx=10, pady=4))
        tl.add(self.info(tl, "tools"))
        self.ui_name = tk.StringVar(value=self.state.get("ui_name",
                                                         "upgrade_p1"))
        tl.add(self.combo(tl, self.ui_name, UI_PRESETS, 14))
        tl.add(self.info(tl, "ui_name"))

        self.canvas = tk.Canvas(left, width=10, height=10, bg="#0d0f12",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right)
        self.wrapped(left, "Pin: click map to place / drag to move.  UI "
                     "point: click the on-screen button.  "
                     "Right-click deletes.").grid(row=2, column=0,
                                                  sticky="ew", pady=(4, 6))

        pe = tm.Flow(left, bg=PANEL)
        pe.grid(row=3, column=0, sticky="ew")
        self.pin_name, self.pin_slot = tk.StringVar(), tk.StringVar()
        pe.add(self.label(pe, " Selected pin", bg=PANEL))
        pe.add(self.entry(pe, self.pin_name, 16))
        pe.add(self.info(pe, "pin"))
        pe.add(self.label(pe, "Tower slot key", bg=PANEL))
        pe.add(self.entry(pe, self.pin_slot, 3))
        pe.add(self.info(pe, "slot"))
        pe.add(self.btn(pe, "Delete pin", self.delete_sel, "#c93c3c"))
        self.pin_name.trace_add("write", self.pin_edited)
        self.pin_slot.trace_add("write", self.pin_edited)

        # ---- right: scrollable tabs (nothing can end up hidden)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.nb = nb = ttk.Notebook(right)
        nb.grid(row=0, column=0, sticky="nsew")
        for title, fn in (("Steps", self.tab_steps), ("Run", self.tab_run),
                          ("Settings", self.tab_settings),
                          ("Help", self.tab_help)):
            sp = tm.Scroll(nb)
            nb.add(sp, text=title)
            fn(sp.body)

    def tab_steps(self, f):
        sc = self.sc
        cols = ("n", "type", "pin", "wave", "cost", "delay", "what")
        widths = (28, 60, 40, 44, 52, 44, 150)
        tw = tk.Frame(f, bg=BG)
        tw.pack(fill="x", padx=6, pady=6)
        self.tree = ttk.Treeview(tw, columns=cols, show="headings", height=8)
        vs = ttk.Scrollbar(tw, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c.upper() if c == "n" else c.title())
            self.tree.column(c, width=int(w * sc), minwidth=int(20 * sc),
                             anchor="w", stretch=True)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.step_selected)
        self.tree.bind("<Delete>", lambda e: self.step_remove())

        self.f = {k: tk.StringVar(value=v) for k, v in (
            ("type", "place"), ("pin", ""), ("path", "1"), ("wave", "0"),
            ("cost", "0"), ("delay", "0"), ("cd", "0"), ("until", "0"),
            ("arg", ""))}
        fl = tm.Flow(f)
        fl.pack(fill="x", padx=6)

        def mk_pin(c):
            self.pin_combo = self.combo(c, self.f["pin"], [], 12)
            return self.pin_combo

        self.cell(fl, "Type", "type", lambda c: self.combo(
            c, self.f["type"], STEP_TYPES, 8))
        self.cell(fl, "Pin", "stpin", mk_pin)
        self.cell(fl, "Path", "path", lambda c: self.combo(
            c, self.f["path"], ["1", "2"], 3))
        self.cell(fl, "Wave ≥", "wave", lambda c: self.entry(
            c, self.f["wave"], 5))
        self.cell(fl, "Cash needed", "cost", lambda c: self.entry(
            c, self.f["cost"], 7))
        self.cell(fl, "Delay s", "delay", lambda c: self.entry(
            c, self.f["delay"], 5))
        self.cell(fl, "Repeat cd s", "cd", lambda c: self.entry(
            c, self.f["cd"], 5))
        self.cell(fl, "Until wave", "until", lambda c: self.entry(
            c, self.f["until"], 5))
        self.cell(fl, "Arg", "arg", lambda c: self.entry(
            c, self.f["arg"], 14))
        self.wrapped(f, HINT).pack(fill="x", padx=6, pady=6)
        bf = tm.Flow(f)
        bf.pack(fill="x", padx=6)
        for text, cmd, col in (("Add", self.step_add, "#2f9e6f"),
                               ("Update", self.step_update, ACCENT),
                               ("Remove", self.step_remove, "#c93c3c"),
                               ("Duplicate", self.step_dup, "#3a3f4b"),
                               ("▲", lambda: self.step_move(-1), "#3a3f4b"),
                               ("▼", lambda: self.step_move(1), "#3a3f4b")):
            bf.add(self.btn(bf, text, cmd, col))
        bf.add(self.info(bf, "stepbtns"))

    def tab_run(self, f):
        top = tm.Flow(f)
        top.pack(fill="x", padx=8, pady=8)
        top.add(self.btn(top, "▶ Start (F11)", self.toggle_run, "#2f9e6f"))
        top.add(self.btn(top, "■ Stop (F8)", self.panic, "#c93c3c"))
        self.grind = tk.BooleanVar(value=bool(self.state.get("grind", True)))
        top.add(tk.Checkbutton(top, text="Grind loop", variable=self.grind,
                               bg=BG, fg=FG, selectcolor=FIELD,
                               activebackground=BG, activeforeground=FG,
                               font=FONT, bd=0, highlightthickness=0))
        top.add(self.info(top, "grind"))
        top.add(self.label(top, "Max matches"))
        self.maxm = tk.StringVar(value=str(self.state.get("maxm", "0")))
        top.add(self.entry(top, self.maxm, 4))
        top.add(self.info(top, "maxm"))

        self.status = tk.Label(f, text="IDLE", bg=BG, fg=FG,
                               font=("Segoe UI Semibold", 13), anchor="w")
        self.status.pack(fill="x", padx=8)
        self.statline = self.wrapped(f, "")
        self.statline.pack(fill="x", padx=8)
        hk = tk.Frame(f, bg=BG)
        hk.pack(fill="x", padx=8)
        self.label(hk, "OCR: " + ("on" if self.reader.ok else "OFF")).pack(
            side="left")
        self.label(hk, "   F8 stop · F9 upgrade spam · F10 abilities · "
                   "F11 run").pack(side="left")
        self.info(hk, "hotkeys").pack(side="left", padx=4)

        mf = tm.Flow(f)
        mf.pack(fill="x", padx=8, pady=8)
        mf.add(self.btn(mf, "Test screen read", self.test_read, "#3a3f4b"))
        mf.add(self.info(mf, "testread"))
        mf.add(self.btn(mf, "Test webhook", self.test_webhook, "#3a3f4b"))
        mf.add(self.info(mf, "testhook"))

        mh = tk.Frame(f, bg=BG)
        mh.pack(fill="x", padx=8)
        self.label(mh, "Micro abilities (F10)  name:cooldown, ...").pack(
            side="left")
        self.info(mh, "micro").pack(side="left", padx=4)
        self.micro_var = tk.StringVar()
        self.micro_var.trace_add(
            "write", lambda *_: self.strat.__setitem__(
                "micro", self.micro_var.get()))
        self.entry(f, self.micro_var, 20).pack(fill="x", padx=8,
                                               pady=(2, 8), ipady=3)
        self.logbox = tk.Text(f, height=12, bg=FIELD, fg=FG, relief="flat",
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

    BOXES = (("cash", "Cash box"), ("wave", "Wave box"),
             ("result", "Result box"))

    def tab_settings(self, f):
        self.sv = {}
        bh = tk.Frame(f, bg=BG)
        bh.pack(fill="x", padx=8, pady=(8, 0))
        self.label(bh, "SCREEN BOXES", fg=FG).pack(side="left")
        self.info(bh, "boxes").pack(side="left", padx=4)
        bx = tk.Frame(f, bg=PANEL)
        bx.pack(fill="x", padx=8, pady=(4, 0))
        bx.columnconfigure(1, weight=1)
        self.box_lbl = {}
        for i, (kind, text) in enumerate(self.BOXES):
            self.label(bx, text, bg=PANEL).grid(row=i, column=0, sticky="w",
                                                padx=8, pady=4)
            self.box_lbl[kind] = self.label(bx, "", bg=PANEL)
            self.box_lbl[kind].grid(row=i, column=1, sticky="w")
            self.btn(bx, "Select on screen", lambda k=kind: self.pick_box(k),
                     ACCENT).grid(row=i, column=2, padx=4, pady=3)
            self.btn(bx, "✕", lambda k=kind: self.clear_box(k),
                     "#3a3f4b").grid(row=i, column=3, padx=(0, 6))
        self.refresh_boxes()
        g = tk.Frame(f, bg=BG)
        g.pack(fill="x", padx=8, pady=8)
        g.columnconfigure(1, weight=1)
        for i, (key, text, browse) in enumerate(self.SETTING_FIELDS):
            self.label(g, text).grid(row=i, column=0, sticky="w", pady=3)
            self.sv[key] = tk.StringVar(value=str(self.settings.get(key, "")))
            self.entry(g, self.sv[key], 10).grid(row=i, column=1, padx=6,
                                                 ipady=3, sticky="we")
            self.info(g, "s_" + key).grid(row=i, column=2, padx=(0, 4))
            if browse:
                self.btn(g, "…", lambda k=key, b=browse: self.browse(k, b),
                         "#3a3f4b").grid(row=i, column=3)
        for v in self.sv.values():  # settings save themselves
            v.trace_add("write", self.settings_changed)
        bf = tm.Flow(f)
        bf.pack(fill="x", padx=8, pady=6)
        bf.add(self.btn(bf, "Save settings", self.save_settings, "#2f9e6f"))
        bf.add(self.btn(bf, "Check for updates", self.check_updates,
                        "#3a3f4b"))
        bf.add(self.btn(bf, "Open data folder",
                        lambda: os.startfile(tm.user_dir()), "#3a3f4b"))
        self.wrapped(f, "Settings save automatically and are kept across "
                     "updates. Keys and OCR words depend on the game's "
                     "current UI - verify them in-game with 'Test screen "
                     "read'.").pack(fill="x", padx=8, pady=(0, 8))

    def tab_help(self, f):
        self.wrapped(f, GUIDE, fg=FG).pack(fill="x", padx=10, pady=10)
        self.btn(f, "Open data folder", lambda: os.startfile(tm.user_dir()),
                 "#3a3f4b").pack(anchor="w", padx=10)

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

    # ---- screen boxes (picked directly on the real screen)
    def regions(self):
        r = self.settings.get("regions")
        if not isinstance(r, dict):
            r = self.settings["regions"] = {}
        return r

    def refresh_boxes(self):
        for kind, _ in self.BOXES:
            b = self.regions().get(kind)
            self.box_lbl[kind].config(
                text=(f"set  ({b[0]:.0%},{b[1]:.0%}) - ({b[2]:.0%},{b[3]:.0%})"
                      if b else "not set"),
                fg=GREEN if b else DIM)

    def clear_box(self, kind):
        if self.regions().pop(kind, None) is not None:
            rn.save_settings(self.settings)
        self.refresh_boxes()

    def pick_box(self, kind):
        if not self.game.rect():
            messagebox.showinfo("TDS Strategy", "Open Roblox first (its "
                                "window must be visible), then try again.")
            return
        self.log(f"Select the {kind} box: drag over it on your screen "
                 "(Esc cancels)")
        self.root.iconify()
        self.root.after(500, lambda: self._overlay(kind))

    def _overlay(self, kind):
        u = ctypes.windll.user32
        vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.attributes("-alpha", 0.3)
        top.configure(bg="black")
        top.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
        cv = tk.Canvas(top, bg="black", highlightthickness=0,
                       cursor="crosshair")
        cv.pack(fill="both", expand=True)
        col = REGION_COLORS.get(kind, FG)
        cv.create_text(self.root.winfo_screenwidth() // 2 - vx, 50,
                       fill="white", font=("Segoe UI Semibold", 16),
                       text=f"Drag a box over the {kind.upper()} text   "
                            "(Esc = cancel)")
        st = {}

        def close(rect=None):
            top.destroy()
            self.root.deiconify()
            if rect:
                self._finish_box(kind, rect)
            else:
                self.log("Box selection cancelled")

        def press(e):
            st["a"] = (e.x, e.y, e.x_root, e.y_root)

        def drag(e):
            if "a" in st:
                cv.delete("r")
                cv.create_rectangle(st["a"][0], st["a"][1], e.x, e.y,
                                    outline=col, width=3, tags="r")

        def release(e):
            if "a" not in st:
                return
            x0, x1 = sorted((st["a"][2], e.x_root))
            y0, y1 = sorted((st["a"][3], e.y_root))
            close((x0, y0, x1, y1) if x1 - x0 > 3 and y1 - y0 > 3 else None)

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)
        top.bind("<Escape>", lambda e: close())
        top.focus_force()

    def _finish_box(self, kind, rect):
        r = self.game.rect()
        if not r:
            self.log("Roblox window not found - box not saved")
            return
        w, h = r[2] - r[0], r[3] - r[1]
        fr = [min(max((rect[0] - r[0]) / w, 0), 1),
              min(max((rect[1] - r[1]) / h, 0), 1),
              min(max((rect[2] - r[0]) / w, 0), 1),
              min(max((rect[3] - r[1]) / h, 0), 1)]
        if fr[2] - fr[0] < 0.002 or fr[3] - fr[1] < 0.002:
            self.log("Box was outside the Roblox window - try again")
            return
        self.regions()[kind] = fr
        rn.save_settings(self.settings)
        self.refresh_boxes()
        self.log(f"{kind} box saved - use 'Test screen read' (Run tab) to "
                 "check it")

    def browse(self, key, kind):
        types = [("JSON", "*.json")] if kind == "json" else [("Program", "*.exe")]
        p = filedialog.askopenfilename(filetypes=types)
        if p:
            self.sv[key].set(p)

    def settings_changed(self, *_):
        if self._settings_job:
            self.root.after_cancel(self._settings_job)
        self._settings_job = self.root.after(
            800, lambda: self.save_settings(quiet=True))

    def save_settings(self, quiet=False):
        self._settings_job = None
        for k, v in self.sv.items():
            self.settings[k] = v.get().strip()
        rn.save_settings(self.settings)
        if not quiet or self.settings.get("tesseract_path", "") != self._tess:
            self._tess = self.settings.get("tesseract_path", "")
            self.reader.configure(self.settings)
        if not quiet:
            self.log("Settings saved. OCR " + ("available" if self.reader.ok
                                                else "not available"))

    # ----------------------------------------------------- persistence
    def snap(self):
        """Remember the strategy so Ctrl+Z can bring it back."""
        self.undo.append(json.dumps(self.strat))
        del self.undo[:-50]

    def do_undo(self, _=None):
        if not self.undo:
            self.log("Nothing to undo")
            return
        self.strat = {**self.blank(), **json.loads(self.undo.pop())}
        self.refresh_view()

    def autosave(self, force=False):
        """Keep the current work (even unsaved) so an update/restart or crash
        never loses it."""
        try:
            s = json.loads(json.dumps(self.strat))
            s["name"] = self.name.get()
            s["image"] = "draft.png" if self.pil else ""
            blob = json.dumps(s)
            if self.pil and self._img_dirty:
                self.pil.save(os.path.join(tm.user_dir(), "draft.png"))
                self._img_dirty = False
            if force or blob != self._last_draft:
                if tm.save_json("draft.json", s):
                    self._last_draft = blob
        except Exception:
            pass

    def restore_draft(self):
        d = tm.load_json("draft.json", None)
        if not isinstance(d, dict):
            return
        self.strat = {**self.blank(), **d}
        if self.strat.get("regions") and not self.regions():
            self.settings["regions"] = dict(self.strat["regions"])
            rn.save_settings(self.settings)
            self.refresh_boxes()
        img = os.path.join(tm.user_dir(), "draft.png")
        if d.get("image") and os.path.exists(img):
            try:
                self.set_image(Image.open(img))
                self._img_dirty = False
            except Exception:
                pass
        self.refresh_all()
        if self.strat["pins"] or self.strat["steps"]:
            self.log("Restored your last session (including unsaved work)")

    def save_state(self):
        try:
            st = dict(geometry=self.root.geometry(), tool=self.tool.get(),
                      ui_name=self.ui_name.get(), grind=self.grind.get(),
                      maxm=self.maxm.get(),
                      tab=self.nb.index(self.nb.select()))
        except Exception:
            return
        if st != self._last_state and tm.save_json("state.json", st):
            self._last_state = st

    def restore_state(self):
        try:
            self.nb.select(int(self.state.get("tab", 0)))
        except Exception:
            pass

    def tick(self):
        self.autosave()
        self.save_state()
        self.root.after(5000, self.tick)

    def shutdown_save(self):
        try:
            self.save_settings(quiet=True)
        except Exception:
            pass
        self.autosave(force=True)
        self.save_state()

    def restart(self):
        if self.runner and self.runner.is_alive() and not messagebox.askyesno(
                "TDS Strategy", "A run is in progress. Stop it and restart?"):
            return
        self.panic()
        self.shutdown_save()
        cmd = [sys.executable] + (sys.argv[1:] if getattr(sys, "frozen", False)
                                  else sys.argv)
        try:
            subprocess.Popen(cmd, close_fds=True,
                             creationflags=0x00000008 | 0x00000200)
        except Exception as e:
            self.log(f"Restart failed: {e}")
            return
        self.root.destroy()

    # ----------------------------------------------------------- canvas
    def on_canvas_resize(self, e):
        self.cw, self.ch = max(e.width, 50), max(e.height, 50)
        if self._rz:
            self.root.after_cancel(self._rz)
        self._rz = self.root.after(60, self.fit)

    def fit(self):
        """Scale the screenshot to whatever size the canvas is right now."""
        self._rz = None
        if self.pil:
            k = min(self.cw / self.pil.width, self.ch / self.pil.height)
            self.dw = max(1, int(self.pil.width * k))
            self.dh = max(1, int(self.pil.height * k))
            self.tkimg = ImageTk.PhotoImage(
                self.pil.resize((self.dw, self.dh), Image.LANCZOS))
        else:
            self.dw, self.dh = self.cw, self.ch
        self.ox, self.oy = (self.cw - self.dw) // 2, (self.ch - self.dh) // 2
        self.redraw()

    def set_image(self, img):
        self.pil = img.convert("RGB")
        self._img_dirty = True
        self.fit()

    def cx(self, fx):
        return self.ox + fx * self.dw

    def cy(self, fy):
        return self.oy + fy * self.dh

    def redraw(self):
        c, sc = self.canvas, self.sc
        c.delete("all")
        if not self.tkimg or not self.pil:
            c.create_text(self.cw // 2, self.ch // 2, fill=DIM, font=FONT_B,
                          text="Load a screenshot or capture from Roblox")
            return
        c.create_image(self.ox, self.oy, anchor="nw", image=self.tkimg)
        d = 7 * sc
        for name, (x, y) in self.strat["ui"].items():
            px, py = self.cx(x), self.cy(y)
            c.create_polygon(px, py - d, px + d, py, px, py + d, px - d, py,
                             fill=GREEN, outline="black")
            c.create_text(px + d + 3, py, text=name, fill=GREEN, anchor="w",
                          font=("Segoe UI", 8))
        R = 10 * sc
        for p in self.strat["pins"]:
            px, py = self.cx(p["x"]), self.cy(p["y"])
            col = AMBER if p["id"] == self.sel else ACCENT
            c.create_oval(px - R, py - R, px + R, py + R, fill=col,
                          outline="black", width=2)
            c.create_text(px, py, text=str(p["id"]), fill="white",
                          font=FONT_B)

    def frac(self, e):
        return (min(max((e.x - self.ox) / self.dw, 0), 1),
                min(max((e.y - self.oy) / self.dh, 0), 1))

    def nearest_pin(self, x, y):
        r, best = 14 * self.sc, None
        for p in self.strat["pins"]:
            d = ((self.cx(p["x"]) - x) ** 2 + (self.cy(p["y"]) - y) ** 2) ** .5
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
            self.snap()
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
                self.snap()
                self.strat["ui"][name] = [fx, fy]
        self.redraw()

    def on_drag(self, e):
        if self.drag and self.drag[0] == "pin":
            fx, fy = self.frac(e)
            self.drag[1]["x"], self.drag[1]["y"] = fx, fy
            self.redraw()

    def on_release(self, e):
        self.drag = None

    def on_right(self, e):
        hit = self.nearest_pin(e.x, e.y)
        if hit:
            self.delete_pin(hit["id"])
            return
        r = 14 * self.sc
        for name, (x, y) in list(self.strat["ui"].items()):
            if abs(self.cx(x) - e.x) < r and abs(self.cy(y) - e.y) < r:
                self.snap()
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
        self.snap()
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
        self.snap()
        self.strat["steps"].append(s)
        self.refresh_steps()

    def step_update(self):
        sel = self.tree.selection()
        if sel:
            self.snap()
            self.strat["steps"][int(sel[0])] = self.form_step()
            self.refresh_steps()

    def step_remove(self):
        sel = self.tree.selection()
        if sel:
            self.snap()
            del self.strat["steps"][int(sel[0])]
            self.refresh_steps()

    def step_dup(self):
        sel = self.tree.selection()
        if sel:
            self.snap()
            i = int(sel[0])
            self.strat["steps"].insert(i + 1, dict(self.strat["steps"][i]))
            self.refresh_steps()
            self.tree.selection_set(str(i + 1))

    def step_move(self, d):
        sel = self.tree.selection()
        if not sel:
            return
        i, j = int(sel[0]), int(sel[0]) + d
        st = self.strat["steps"]
        if 0 <= j < len(st):
            self.snap()
            st[i], st[j] = st[j], st[i]
            self.refresh_steps()
            self.tree.selection_set(str(j))

    # ---------------------------------------------------------- file ops
    def refresh_all(self):
        self.name.set(self.strat.get("name", "strategy"))
        self.refresh_view()

    def refresh_view(self):
        self.micro_var.set(self.strat.get("micro", ""))
        self.select_pin(0)
        self.refresh_pins()
        self.redraw()

    def new(self):
        if (self.strat["pins"] or self.strat["steps"]) and not \
                messagebox.askyesno("TDS Strategy", "Start a new strategy? "
                                    "Anything you haven't saved is lost."):
            return
        self.strat = self.blank()
        self.pil = self.tkimg = None
        self._img_dirty = False
        self.undo.clear()
        self.fit()
        self.refresh_all()
        self.autosave(force=True)

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
        self.undo.clear()
        img = os.path.join(os.path.dirname(p), self.strat.get("image", ""))
        if self.strat.get("image") and os.path.exists(img):
            self.set_image(Image.open(img))
        else:
            self.fit()
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
        self.save_settings(quiet=True)
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
            r = self.regions()
            self.log(f"Read: wave={self.reader.number(r.get('wave'), True)}  "
                     f"cash={self.reader.number(r.get('cash'))}")
            if r.get("result"):
                self.log("Result box text: " +
                         repr(self.reader.text(r["result"]).strip()[:80]))
        threading.Thread(target=work, daemon=True).start()

    def test_webhook(self):
        self.save_settings(quiet=True)
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
                    self.topbar.set_visible(self.upd_btn, True)
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
        self.shutdown_save()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
