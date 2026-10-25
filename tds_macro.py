"""
TDS Macro v2 - compact macro recorder/player for Roblox tower defense.

Hotkeys (work globally):  F6 record  |  F7 play/stop  |  F8 emergency stop
Run from source:  pip install pynput  ->  python tds_macro.py
"""

import ctypes
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk

from pynput import keyboard, mouse

if os.name != "nt":
    sys.exit("TDS Macro only works on Windows.")

# ---------------------------------------------------------------- Win32 setup
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()
try:
    ctypes.windll.winmm.timeBeginPeriod(1)  # 1ms timer resolution
except Exception:
    pass

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetParent.restype = wintypes.HWND
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


M_MOVE, M_ABS, M_VDESK, M_WHEEL = 0x1, 0x8000, 0x4000, 0x800
K_EXT, K_UP, K_SCAN = 0x1, 0x2, 0x8
BTN = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}
EXT_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
           0x5B, 0x5C, 0xA3, 0xA5, 0x6F, 0x90}
HOTKEYS = {0x75: "record", 0x76: "play", 0x77: "panic"}  # F6 F7 F8
ROBLOX_EXES = {"robloxplayerbeta.exe", "windows10universal.exe",
               "robloxplayer.exe"}


def send(inp):
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def m_input(flags, x=0, y=0, data=0):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(x, y, data, flags, 0, 0)
    return i


def abs_move(px, py):
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    return m_input(M_MOVE | M_ABS | M_VDESK,
                   int((px - vx) * 65535 / max(vw - 1, 1)),
                   int((py - vy) * 65535 / max(vh - 1, 1)))


def k_input(vk, down):
    flags = K_SCAN | (0 if down else K_UP) | (K_EXT if vk in EXT_VKS else 0)
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(0, user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
    return i


def roblox_focused():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value).lower() in ROBLOX_EXES
        return False
    finally:
        kernel32.CloseHandle(h)


def spin_until(target, stop_evt):
    while True:
        rem = target - time.perf_counter()
        if rem <= 0:
            return True
        if rem > 0.003:
            if stop_evt.wait(min(rem - 0.002, 0.05)):
                return False
        elif stop_evt.is_set():
            return False


def _app_base():
    return (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))


def user_dir(sub=""):
    """Per-user data folder (settings, macros, strategies, drafts). It lives
    outside the install folder and the code cache, so app updates and
    reinstalls never touch it."""
    roots = []
    if os.environ.get("APPDATA"):
        roots.append(os.path.join(os.environ["APPDATA"], "TDSStrategy"))
    roots.append(_app_base())
    for root in roots:
        d = os.path.join(root, sub) if sub else root
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except OSError:
            pass
    return _app_base()


def data_dir():
    return user_dir("macros")


def load_json(name, default):
    try:
        with open(os.path.join(user_dir(), name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(name, data):
    """Atomic write (temp file + replace) so a crash can't corrupt it."""
    try:
        path = os.path.join(user_dir(), name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
        os.path.abspath(b))


def _copy_missing(src, dst):
    """Copy files from src to dst, never overwriting anything."""
    if not os.path.isdir(src) or _same(src, dst):
        return
    os.makedirs(dst, exist_ok=True)
    for n in os.listdir(src):
        s_, t_ = os.path.join(src, n), os.path.join(dst, n)
        if os.path.isfile(s_) and not os.path.exists(t_):
            shutil.copy2(s_, t_)


def migrate_legacy():
    """One-time import of data saved by older versions next to the exe."""
    root = user_dir()
    flag = os.path.join(root, ".migrated")
    if os.path.exists(flag):
        return
    try:
        base = _app_base()
        old, new = os.path.join(base, "settings.json"), os.path.join(
            root, "settings.json")
        if (os.path.isfile(old) and not os.path.exists(new)
                and not _same(old, new)):
            shutil.copy2(old, new)
        for sub in ("macros", "strategies"):
            _copy_missing(os.path.join(base, sub), os.path.join(root, sub))
        if os.environ.get("APPDATA"):
            _copy_missing(os.path.join(os.environ["APPDATA"], "TDSMacro",
                                       "macros"), os.path.join(root, "macros"))
        open(flag, "w").close()
    except Exception:
        pass


# ---------------------------------------------------------------------- engine
class Engine:
    def __init__(self, q, name_fn):
        self.q, self.name_fn = q, name_fn
        self.cfg = dict(loops=1, speed=1.0, settle=4.0, moves=False,
                        safety=True, max_min=0)
        self.events, self.state = [], "idle"
        self.t0, self.sid, self.reason = 0.0, 0, ""
        self.down_keys = set()
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.run_start = 0.0

    def say(self, kind, text=""):
        self.q.put((kind, text))

    # ---- files
    def path(self, name):
        name = re.sub(r"[^\w \-]", "", name).strip() or "macro"
        return os.path.join(data_dir(), name + ".json")

    def load(self, name):
        try:
            with open(self.path(name)) as f:
                self.events = json.load(f)
            return len(self.events)
        except Exception:
            return None

    # ---- shared
    def _begin(self, state):
        if self.cfg["safety"] and not roblox_focused():
            self.say("msg", "Focus the Roblox window first")
            return False
        self.state, self.sid = state, self.sid + 1
        self.run_start = time.time()
        self.say("state", state)
        threading.Thread(target=self._watch, args=(self.sid,),
                         daemon=True).start()
        return True

    def _watch(self, sid):
        lost = 0
        while self.state != "idle" and self.sid == sid:
            time.sleep(0.1)
            if self.cfg["safety"]:
                lost = lost + 1 if not roblox_focused() else 0
                if lost >= 3:
                    self.stop("Stopped: Roblox lost focus")
                    return
            mm = self.cfg["max_min"]
            if (mm and self.state == "playing"
                    and time.time() - self.run_start > mm * 60):
                self.stop("Stopped: max runtime reached")
                return

    def stop(self, reason="Stopped"):
        if self.state == "recording":
            self.stop_record(reason)
        elif self.state == "playing":
            self.reason = reason
            self.stop_evt.set()

    # ---- recording
    def start_record(self):
        if self.state != "idle":
            return
        self.events, self.down_keys = [], set()
        self.t0 = time.perf_counter()
        self._begin("recording")

    def stop_record(self, reason="Recording stopped"):
        self.state = "idle"
        if self.events:
            off = self.events[0][0]
            for e in self.events:
                e[0] = round(e[0] - off, 6)
            with open(self.path(self.name_fn()), "w") as f:
                json.dump(self.events, f)
            self.say("saved")
            reason += f" - saved {len(self.events)} events"
        else:
            reason += " - nothing recorded"
        self.say("state", "idle")
        self.say("msg", reason)

    def _add(self, *ev):
        if self.state == "recording":
            with self.lock:
                self.events.append([time.perf_counter() - self.t0, *ev])

    def on_move(self, x, y):
        if self.cfg["moves"]:
            self._add("move", x, y)

    def on_click(self, x, y, button, pressed):
        self._add("click", x, y, button.name, pressed)

    def on_scroll(self, x, y, dx, dy):
        self._add("scroll", x, y, dy)

    @staticmethod
    def _vk(key):
        vk = getattr(key, "vk", None)
        if vk is None and hasattr(key, "value"):
            vk = getattr(key.value, "vk", None)
        return vk

    def on_press(self, key):
        vk = self._vk(key)
        if vk is None:
            return
        if vk in HOTKEYS:
            a = HOTKEYS[vk]
            if a == "record":
                if self.state == "recording":
                    self.stop_record()
                else:
                    self.start_record()
            elif a == "play":
                if self.state == "playing":
                    self.stop("Stopped")
                else:
                    self.start_play()
            else:
                self.stop("Emergency stop")
            return
        if self.state == "recording" and vk not in self.down_keys:
            self.down_keys.add(vk)
            self._add("key", vk, True)

    def on_release(self, key):
        vk = self._vk(key)
        if vk in HOTKEYS or vk is None:
            return
        if self.state == "recording" and vk in self.down_keys:
            self.down_keys.discard(vk)
            self._add("key", vk, False)

    # ---- playback
    def start_play(self):
        if self.state != "idle":
            return
        if not self.events:
            self.say("msg", "No macro loaded - record one first")
            return
        self.stop_evt.clear()
        self.reason = "Finished"
        if self._begin("playing"):
            threading.Thread(target=self._play, daemon=True).start()

    def _prepare(self):
        out = []
        for t, kind, *a in self.events:
            if kind == "move":
                out.append((t, kind, abs_move(a[0], a[1])))
            elif kind == "click":
                x, y, b, pr = a
                d, u = BTN[b]
                out.append((t, kind, (abs_move(x, y),
                                      m_input(d if pr else u), b, pr)))
            elif kind == "scroll":
                out.append((t, kind, (abs_move(a[0], a[1]), m_input(
                    M_WHEEL, data=(int(a[2]) * 120) & 0xFFFFFFFF))))
            elif kind == "key":
                out.append((t, kind, (k_input(a[0], a[1]), a[0], a[1])))
        return out

    def _play(self):
        prepared = self._prepare()
        cfg = dict(self.cfg)
        speed, settle = max(cfg["speed"], 0.01), cfg["settle"] / 1000.0
        held_k, held_b = set(), set()
        try:
            n = 0
            while not self.stop_evt.is_set():
                start = time.perf_counter()
                for t, kind, p in prepared:
                    if not spin_until(start + t / speed, self.stop_evt):
                        break
                    if kind == "move":
                        send(p)
                    elif kind == "click":
                        send(p[0])
                        if p[3] and settle:
                            spin_until(time.perf_counter() + settle,
                                       self.stop_evt)
                        send(p[1])
                        (held_b.add if p[3] else held_b.discard)(p[2])
                    elif kind == "scroll":
                        send(p[0])
                        send(p[1])
                    else:
                        send(p[0])
                        (held_k.add if p[2] else held_k.discard)(p[1])
                n += 1
                self.say("msg", f"Playing - loop {n}"
                         + (f"/{cfg['loops']}" if cfg["loops"] else ""))
                if cfg["loops"] and n >= cfg["loops"]:
                    break
        finally:  # never leave keys or buttons stuck down
            for vk in held_k:
                send(k_input(vk, False))
            for b in held_b:
                send(m_input(BTN[b][1]))
            self.state = "idle"
            self.say("state", "idle")
            self.say("msg", self.reason)


# -------------------------------------------------------------------------- UI
BG, PANEL, FIELD = "#14161a", "#1c1f26", "#262a33"
FG, DIM = "#e6e8ee", "#8a90a0"
ACCENT, RED, GREEN, AMBER = "#5b8cff", "#ff5b5b", "#3ddc97", "#ffb454"
FONT, FONT_B = ("Segoe UI", 9), ("Segoe UI Semibold", 9)



# ------------------------------------------------------- shared UI helpers
def ui_scale(root):
    """Windows display scaling (1.0 = 100%, 1.5 = 150%...)."""
    try:
        return max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:
        return 1.0


_tip = {"win": None}


def _tip_hide(*_):
    w, _tip["win"] = _tip["win"], None
    if w is not None:
        try:
            w.destroy()
        except Exception:
            pass


def _tip_show(widget, text):
    _tip_hide()
    try:
        sc = ui_scale(widget)
        w = tk.Toplevel(widget)
        w.wm_overrideredirect(True)
        try:
            w.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(w, text=text, justify="left", wraplength=int(300 * sc),
                 bg="#0d0f12", fg=FG, font=("Segoe UI", 9), padx=8, pady=6,
                 bd=1, relief="solid").pack()
        w.update_idletasks()
        tw, th = w.winfo_reqwidth(), w.winfo_reqheight()
        x = widget.winfo_rootx() + widget.winfo_width() + 6
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        sw, sh = widget.winfo_screenwidth(), widget.winfo_screenheight()
        if x + tw > sw:
            x = max(0, sw - tw - 8)
        if y + th > sh:
            y = widget.winfo_rooty() - th - 4
        w.wm_geometry(f"+{x}+{max(0, y)}")
        _tip["win"] = w
    except Exception:
        pass


def info(parent, text):
    """Compact '!' badge: hover (or click) shows what a feature does."""
    lbl = tk.Label(parent, text="!", bg=ACCENT, fg="white",
                   font=("Segoe UI Semibold", 7), padx=4, pady=0,
                   cursor="question_arrow")
    lbl.bind("<Enter>", lambda e: _tip_show(lbl, text))
    lbl.bind("<Leave>", _tip_hide)
    lbl.bind("<Button-1>", lambda e: _tip_show(lbl, text))
    lbl.bind("<Destroy>", _tip_hide, add="+")
    return lbl


class Flow(tk.Frame):
    """Row of widgets that wraps onto more lines when the window is narrow,
    so nothing is ever cut off. Add children (created with this frame as
    parent) via add()."""

    def __init__(self, parent, bg=BG, gap=4):
        super().__init__(parent, bg=bg, height=1)
        self.items, self.hidden, self.gap, self._fw = [], set(), gap, 0
        self.bind("<Configure>", self._on)

    def add(self, w, visible=True):
        self.items.append(w)
        if not visible:
            self.hidden.add(w)
        self.after_idle(self.layout)

    def set_visible(self, w, vis):
        (self.hidden.discard if vis else self.hidden.add)(w)
        if not vis:
            w.place_forget()
        self.layout()

    def _on(self, e):
        if e.width != self._fw:
            self._fw = e.width
            self.layout()

    def layout(self):
        width = self.winfo_width()
        if width <= 1:
            return
        x = y = rh = 0
        for w in self.items:
            if w in self.hidden:
                continue
            ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x and x + ww > width:
                x, y, rh = 0, y + rh + self.gap, 0
            w.place(x=x, y=y)
            x += ww + self.gap
            rh = max(rh, wh)
        if int(str(self.cget("height"))) != max(rh + y, 1):
            self.configure(height=max(rh + y, 1))


class Scroll(tk.Frame):
    """Vertically scrollable panel. Put content in .body - a scrollbar
    appears only when the window is too small to show it all."""

    def __init__(self, parent, bg=BG):
        super().__init__(parent, bg=bg)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0,
                            yscrollincrement=20, width=10, height=10)
        self.sb = ttk.Scrollbar(self, orient="vertical",
                                command=self.cv.yview)
        self.cv.configure(yscrollcommand=self.sb.set)
        self.cv.grid(row=0, column=0, sticky="nsew")
        self.sb.grid(row=0, column=1, sticky="ns")
        self.sb.grid_remove()
        self._need = False
        self.body = tk.Frame(self.cv, bg=bg)
        self.win = self.cv.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._sync)
        self.cv.bind("<Configure>", self._sync)

    def _sync(self, _=None):
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        if w <= 1:
            return
        bh = self.body.winfo_reqheight()
        self.cv.itemconfigure(self.win, width=w, height=max(bh, h))
        self.cv.configure(scrollregion=(0, 0, w, max(bh, h)))
        need = bh > h
        if need != self._need:
            self._need = need
            (self.sb.grid if need else self.sb.grid_remove)()

    def wheel(self, e):
        if self._need:
            self.cv.yview_scroll(-1 if e.delta > 0 else 1, "units")


def enable_wheel(root):
    """Mouse wheel scrolls whichever Scroll panel the pointer is over."""
    def on_wheel(e):
        w = e.widget
        try:
            if w.winfo_class() in ("Text", "Treeview", "Listbox",
                                   "TCombobox"):
                return
        except Exception:
            return
        while w is not None:
            if isinstance(w, Scroll):
                w.wheel(e)
                return
            w = getattr(w, "master", None)
    root.bind_all("<MouseWheel>", on_wheel)


TIPS = dict(
    macro="Type a name, press Record (or F6), do your actions in Roblox, "
          "press F6 again - it saves under that name. Pick an existing name "
          "from the list to load it. The folder button opens your macros.",
    loops="How many times the recording replays. 0 = repeat forever until "
          "you press F7 or F8.",
    speed="Playback speed. 1.0 = as recorded, 2 = twice as fast, 0.5 = half "
          "speed. Raise it gradually - too fast can miss clicks in game.",
    gap="Milliseconds between a click going down and back up. Raise to "
        "10-30 if the game ignores quick clicks.",
    maxmin="Auto-stops playback after this many minutes. 0 = no limit.",
    safety="Stops recording/playback if Roblox isn't the active window, so "
           "keys and clicks never go to other apps. Recommended on.",
    moves="Also records the cursor path. Off = clicks and keys only "
          "(smaller, more reliable).",
    top="Keeps this window above other windows.",
    hotkeys="Setup: 1) name your macro  2) F6, switch to Roblox, do your "
            "actions  3) F6 again to save  4) F7 to play.  F8 stops "
            "everything instantly. Hotkeys work while Roblox is focused.")


class App:
    def __init__(self):
        migrate_legacy()
        self.q = queue.Queue()
        self.counting = False
        self.tick = 0
        self.saved = load_json("macro_ui.json", {})
        self._last_saved = None
        self.root = root = tk.Tk()
        root.title("TDS Macro")
        root.configure(bg=BG)
        self.sc = ui_scale(root)
        self.eng = Engine(self.q, lambda: self.name.get())
        self._dark_titlebar()
        self._build()
        self.refresh_list()
        self._restore()
        self.eng.load(self.name.get())
        root.resizable(False, False)  # size follows the content at any DPI
        self.listeners = [
            mouse.Listener(on_move=self.eng.on_move,
                           on_click=self.eng.on_click,
                           on_scroll=self.eng.on_scroll),
            keyboard.Listener(on_press=self.eng.on_press,
                              on_release=self.eng.on_release)]
        for lst in self.listeners:
            lst.daemon = True
            lst.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def _dark_titlebar(self):
        try:
            self.root.update()
            hwnd = user32.GetParent(self.root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass

    def _btn(self, parent, text, cmd, color, col):
        b = tk.Button(parent, text=text, command=cmd, bg=color, fg="white",
                      activebackground=color, activeforeground="white",
                      relief="flat", bd=0, font=FONT_B, cursor="hand2",
                      pady=7)
        b.grid(row=0, column=col, sticky="ew", padx=3)
        return b

    def _field(self, parent, label, default, row, col, tip=""):
        fl = tk.Frame(parent, bg=PANEL)
        fl.grid(row=row, column=col * 2, sticky="w", padx=(10, 4), pady=5)
        tk.Label(fl, text=label, bg=PANEL, fg=DIM, font=FONT).pack(
            side="left")
        if tip:
            info(fl, tip).pack(side="left", padx=(3, 0))
        v = tk.StringVar(value=default)
        tk.Entry(parent, textvariable=v, width=6, bg=FIELD, fg=FG,
                 insertbackground=FG, relief="flat", justify="center",
                 font=FONT).grid(row=row, column=col * 2 + 1,
                                 padx=(0, 10), ipady=3)
        return v

    def _check(self, parent, text, default, tip="", cmd=None):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w", padx=8, fill="x")
        v = tk.BooleanVar(value=default)
        tk.Checkbutton(row, text=text, variable=v, command=cmd, bg=PANEL,
                       fg=FG, selectcolor=FIELD, activebackground=PANEL,
                       activeforeground=FG, font=FONT, bd=0,
                       highlightthickness=0).pack(side="left")
        if tip:
            info(row, tip).pack(side="left", padx=(2, 0))
        return v

    def _build(self):
        r = self.root
        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=14, pady=(14, 2))
        self.dot = tk.Canvas(head, width=12, height=12, bg=BG,
                             highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=DIM, outline="")
        self.state_lbl = tk.Label(head, text="IDLE", bg=BG, fg=FG,
                                  font=("Segoe UI Semibold", 11))
        self.state_lbl.pack(side="left", padx=8)
        self.info = tk.Label(head, text="", bg=BG, fg=DIM, font=FONT)
        self.info.pack(side="right")
        self.msg = tk.Label(r, text="Ready", bg=BG, fg=DIM, font=FONT,
                            anchor="w", wraplength=int(290 * self.sc), justify="left")
        self.msg.pack(fill="x", padx=14, pady=(0, 8))

        bf = tk.Frame(r, bg=BG)
        bf.pack(fill="x", padx=11)
        for c in range(3):
            bf.columnconfigure(c, weight=1)
        self._btn(bf, "●  Record", lambda: self.begin("rec"), "#c93c3c", 0)
        self._btn(bf, "▶  Play", lambda: self.begin("play"), "#2f9e6f", 1)
        self._btn(bf, "■  Stop", self.stop_all, "#3a3f4b", 2)

        st = ttk.Style()
        st.theme_use("clam")
        st.configure("D.TCombobox", fieldbackground=FIELD, background=FIELD,
                     foreground=FG, arrowcolor=FG, bordercolor=FIELD,
                     lightcolor=FIELD, darkcolor=FIELD, padding=4)
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        mf = tk.Frame(r, bg=BG)
        mf.pack(fill="x", padx=14, pady=(12, 0))
        hl = tk.Frame(mf, bg=BG)
        hl.pack(anchor="w")
        tk.Label(hl, text="MACRO", bg=BG, fg=DIM,
                 font=("Segoe UI Semibold", 8)).pack(side="left")
        info(hl, TIPS["macro"]).pack(side="left", padx=(4, 0))
        row = tk.Frame(mf, bg=BG)
        row.pack(fill="x", pady=(2, 0))
        self.name = tk.StringVar()
        self.combo = ttk.Combobox(row, textvariable=self.name,
                                  style="D.TCombobox", font=FONT)
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", self.on_pick)
        tk.Button(row, text="📁", command=lambda: os.startfile(data_dir()),
                  bg=FIELD, fg=FG, relief="flat", bd=0, cursor="hand2",
                  activebackground=FIELD, padx=8).pack(side="left", padx=(6, 0))

        p = tk.Frame(r, bg=PANEL)
        p.pack(fill="x", padx=14, pady=12)
        self.loops = self._field(p, "Loops (0=∞)", "1", 0, 0, TIPS["loops"])
        self.speed = self._field(p, "Speed ×", "1.0", 0, 1, TIPS["speed"])
        self.settle = self._field(p, "Click gap ms", "4", 1, 0, TIPS["gap"])
        self.maxmin = self._field(p, "Max min (0=off)", "0", 1, 1,
                                  TIPS["maxmin"])
        sf = tk.Frame(p, bg=PANEL)
        sf.grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 6))
        self.safety = self._check(sf, "Auto-stop if Roblox loses focus", True,
                                  TIPS["safety"])
        self.moves = self._check(sf, "Record mouse movement", False,
                                 TIPS["moves"])
        self.top = self._check(sf, "Always on top", False, TIPS["top"],
                               lambda: self.root.attributes(
                                   "-topmost", self.top.get()))

        hk = tk.Frame(r, bg=BG)
        hk.pack(pady=(0, 6))
        tk.Label(hk, text="F6 record   F7 play/stop   F8 emergency stop",
                 bg=BG, fg=DIM, font=("Segoe UI", 8)).pack(side="left")
        info(hk, TIPS["hotkeys"]).pack(side="left", padx=(4, 0))

    def ui_state(self):
        return dict(name=self.name.get(), loops=self.loops.get(),
                    speed=self.speed.get(), settle=self.settle.get(),
                    maxmin=self.maxmin.get(), safety=self.safety.get(),
                    moves=self.moves.get(), top=self.top.get())

    def _restore(self):
        sv = self.saved
        self.name.set(sv.get("name") or "macro")
        for key in ("loops", "speed", "settle", "maxmin"):
            if key in sv:
                getattr(self, key).set(str(sv[key]))
        for key in ("safety", "moves", "top"):
            if key in sv:
                getattr(self, key).set(bool(sv[key]))
        if self.top.get():
            self.root.attributes("-topmost", True)
        self._last_saved = self.ui_state()

    def save_ui(self, force=False):
        st = self.ui_state()
        if (force or st != self._last_saved) and save_json(
                "macro_ui.json", st):
            self._last_saved = st

    @staticmethod
    def _num(var, cast, default, lo=0):
        try:
            return max(cast(var.get()), lo)
        except ValueError:
            return default

    def push_cfg(self):
        self.eng.cfg.update(
            loops=self._num(self.loops, int, 1),
            speed=self._num(self.speed, float, 1.0, 0.01),
            settle=self._num(self.settle, float, 4.0),
            max_min=self._num(self.maxmin, float, 0),
            moves=self.moves.get(), safety=self.safety.get())

    def begin(self, action):
        if self.eng.state != "idle" or self.counting:
            return
        self.counting = True
        self.count(3, action)

    def count(self, n, action):
        if not self.counting:
            return
        if n:
            self.set_state("countdown",
                           f"Starting in {n}... click into Roblox")
            self.root.after(1000, lambda: self.count(n - 1, action))
            return
        self.counting = False
        self.set_state("idle", "")
        self.push_cfg()
        if action == "rec":
            self.eng.start_record()
        else:
            self.eng.start_play()

    def stop_all(self):
        if self.counting:
            self.counting = False
            self.set_state("idle", "Cancelled")
        else:
            self.eng.stop("Stopped")

    def on_pick(self, _=None):
        n = self.eng.load(self.name.get())
        self.msg.config(text=f"Loaded {n} events" if n is not None
                        else "Couldn't load macro")

    def refresh_list(self):
        self.combo["values"] = sorted(
            f[:-5] for f in os.listdir(data_dir()) if f.endswith(".json"))

    def set_state(self, state, msg=None):
        label, color = {"idle": ("IDLE", DIM), "recording": ("RECORDING", RED),
                        "playing": ("PLAYING", GREEN),
                        "countdown": ("GET READY", AMBER)}[state]
        self.state_lbl.config(text=label)
        self.dot.itemconfig(self.dot_id, fill=color)
        if msg is not None:
            self.msg.config(text=msg)

    def poll(self):
        self.push_cfg()
        self.tick += 1
        if self.tick % 25 == 0:  # ~every 2s; only writes when changed
            self.save_ui()
        while True:
            try:
                kind, text = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self.set_state(text)
            elif kind == "msg":
                self.msg.config(text=text)
            elif kind == "saved":
                self.refresh_list()
        e = self.eng
        if e.state == "recording":
            self.info.config(text=f"{len(e.events)} events  "
                             f"{time.time() - e.run_start:.0f}s")
        elif e.state == "playing":
            self.info.config(text=f"{time.time() - e.run_start:.0f}s")
        else:
            self.info.config(text=f"{len(e.events)} events loaded"
                             if e.events else "")
        self.root.after(80, self.poll)

    def close(self):
        self.save_ui(force=True)
        self.counting = False
        self.eng.stop("Closed")
        time.sleep(0.05)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
