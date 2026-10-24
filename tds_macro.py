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


def data_dir():
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(base, "macros")
    try:
        os.makedirs(d, exist_ok=True)
        if os.access(d, os.W_OK):
            return d
    except OSError:
        pass
    d = os.path.join(os.environ.get("APPDATA", base), "TDSMacro", "macros")
    os.makedirs(d, exist_ok=True)
    return d


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


class App:
    def __init__(self):
        self.q = queue.Queue()
        self.counting = False
        self.root = root = tk.Tk()
        root.title("TDS Macro")
        root.geometry("320x436")
        root.resizable(False, False)
        root.configure(bg=BG)
        self.eng = Engine(self.q, lambda: self.name.get())
        self._dark_titlebar()
        self._build()
        self.refresh_list()
        self.name.set("macro")
        self.eng.load("macro")
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

    def _field(self, parent, label, default, row, col):
        tk.Label(parent, text=label, bg=PANEL, fg=DIM, font=FONT).grid(
            row=row, column=col * 2, sticky="w", padx=(10, 4), pady=5)
        v = tk.StringVar(value=default)
        tk.Entry(parent, textvariable=v, width=6, bg=FIELD, fg=FG,
                 insertbackground=FG, relief="flat", justify="center",
                 font=FONT).grid(row=row, column=col * 2 + 1,
                                 padx=(0, 10), ipady=3)
        return v

    def _check(self, parent, text, default, cmd=None):
        v = tk.BooleanVar(value=default)
        tk.Checkbutton(parent, text=text, variable=v, command=cmd, bg=PANEL,
                       fg=FG, selectcolor=FIELD, activebackground=PANEL,
                       activeforeground=FG, font=FONT, bd=0,
                       highlightthickness=0).pack(anchor="w", padx=8)
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
                            anchor="w", wraplength=290, justify="left")
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
        tk.Label(mf, text="MACRO", bg=BG, fg=DIM,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w")
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
        self.loops = self._field(p, "Loops (0=∞)", "1", 0, 0)
        self.speed = self._field(p, "Speed ×", "1.0", 0, 1)
        self.settle = self._field(p, "Click gap ms", "4", 1, 0)
        self.maxmin = self._field(p, "Max min (0=off)", "0", 1, 1)
        sf = tk.Frame(p, bg=PANEL)
        sf.grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 6))
        self.safety = self._check(sf, "Auto-stop if Roblox loses focus", True)
        self.moves = self._check(sf, "Record mouse movement", False)
        self.top = self._check(sf, "Always on top", False,
                               lambda: self.root.attributes(
                                   "-topmost", self.top.get()))

        tk.Label(r, text="F6 record   F7 play/stop   F8 emergency stop",
                 bg=BG, fg=DIM, font=("Segoe UI", 8)).pack(pady=(0, 4))

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
        self.counting = False
        self.eng.stop("Closed")
        time.sleep(0.05)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
