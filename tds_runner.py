"""
TDS Strategy - core engine (no UI).

Reads wave/cash from the screen with OCR, runs a strategy step list in order,
fires repeating abilities on cooldowns, detects win/loss/disconnect, rejoins,
and reports to a Discord webhook. Input goes through tds_macro's SendInput.
"""

import ctypes
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from ctypes import wintypes

from PIL import Image, ImageGrab, ImageOps

import tds_macro as tm

try:
    import pytesseract
except ImportError:  # app still runs; it just can't read the screen
    pytesseract = None

user32 = ctypes.windll.user32
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

DEFAULTS = dict(
    webhook="", place_id="3260590327", tesseract_path="",
    lobby_macro="", restart_macro="",
    win_words="triumph,victory", lose_words="game over,defeat",
    drop_words="disconnected,error code,reconnect",
    upgrade_key="e", sell_key="x",
    end_wait=8, join_wait=20, start_delay=5, match_timeout=240)


def app_dir(sub=""):
    """User data folder - stable across app updates and reinstalls."""
    return tm.user_dir(sub)


def load_settings():
    tm.migrate_legacy()
    s = dict(DEFAULTS)  # new settings added by updates get their defaults
    saved = tm.load_json("settings.json", {})
    if isinstance(saved, dict):
        s.update(saved)
    return s


def save_settings(s):
    tm.save_json("settings.json", s)


class Stopped(Exception):
    pass


# ---------------------------------------------------------- adaptive timing
class Timing:
    """Scales every input delay up on slow machines or after failed checks."""

    def __init__(self):
        self.scale, self.avg = 1.0, 0.0

    def note_read(self, dt):
        self.avg = dt if not self.avg else self.avg * 0.8 + dt * 0.2
        self.scale = max(self.scale * 0.995, min(4.0, max(1.0, 0.7 + self.avg * 2.5)))

    def bump(self):
        self.scale = min(self.scale * 1.25, 4.0)

    def d(self, base):
        return base * self.scale


# -------------------------------------------------------------- game window
def find_roblox():
    found = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == "Roblox":
                    found.append(hwnd)
        return True

    user32.EnumWindows(_ENUMPROC(cb), 0)
    return found[0] if found else None


class Game:
    def __init__(self, timing):
        self.t = timing

    def rect(self):
        h = find_roblox()
        if not h:
            return None
        rc = wintypes.RECT()
        user32.GetClientRect(h, ctypes.byref(rc))
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(h, ctypes.byref(pt))
        return (pt.x, pt.y, pt.x + rc.right, pt.y + rc.bottom)

    def focus(self):
        h = find_roblox()
        if not h:
            return False
        if not tm.roblox_focused():
            user32.ShowWindow(h, 9)
            user32.SetForegroundWindow(h)
            time.sleep(0.25)
        return tm.roblox_focused()

    def pos(self, f):
        r = self.rect()
        if not r:
            return None
        return (int(r[0] + f[0] * (r[2] - r[0])),
                int(r[1] + f[1] * (r[3] - r[1])))

    def grab(self, region=None):
        r = self.rect()
        if not r:
            return None
        w, h = r[2] - r[0], r[3] - r[1]
        box = r if not region else (
            r[0] + int(region[0] * w), r[1] + int(region[1] * h),
            r[0] + int(region[2] * w), r[1] + int(region[3] * h))
        return ImageGrab.grab(bbox=box, all_screens=True)

    def click(self, f, refocus=True):
        p = self.pos(f)
        if not p or not (self.focus() if refocus else tm.roblox_focused()):
            return False
        tm.send(tm.abs_move(*p))
        time.sleep(self.t.d(0.03))
        down, up = tm.BTN["left"]
        tm.send(tm.m_input(down))
        time.sleep(self.t.d(0.03))
        tm.send(tm.m_input(up))
        time.sleep(self.t.d(0.05))
        return True

    def key(self, ch, refocus=True):
        if not ch or len(ch) != 1 or not (
                self.focus() if refocus else tm.roblox_focused()):
            return False
        vk = ord(ch.upper())
        tm.send(tm.k_input(vk, True))
        time.sleep(self.t.d(0.03))
        tm.send(tm.k_input(vk, False))
        time.sleep(self.t.d(0.05))
        return True


# ------------------------------------------------------------------ reading
class Reader:
    def __init__(self, game, timing, settings):
        self.g, self.t, self.ok = game, timing, False
        self.configure(settings)

    def configure(self, s):
        self.ok = False
        if pytesseract is None:
            return
        for c in (s.get("tesseract_path", ""),
                  r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if c and os.path.exists(c):
                pytesseract.pytesseract.tesseract_cmd = c
                break
        try:
            pytesseract.get_tesseract_version()
            self.ok = True
        except Exception:
            self.ok = False

    @staticmethod
    def _prep(img, invert):
        g = ImageOps.autocontrast(ImageOps.grayscale(img))
        g = g.resize((g.width * 3, g.height * 3), Image.LANCZOS)
        if invert:
            g = ImageOps.invert(g)
        return g.point(lambda v: 255 if v > 140 else 0)

    def number(self, region, first=False):
        if not self.ok or not region:
            return None
        img = self.g.grab(region)
        if img is None:
            return None
        wl = "0123456789/" if first else "0123456789"
        t0, txt = time.perf_counter(), ""
        for inv in (False, True):
            txt = pytesseract.image_to_string(
                self._prep(img, inv),
                config=f"--psm 7 -c tessedit_char_whitelist={wl}").strip()
            if re.search(r"\d", txt):
                break
        self.t.note_read(time.perf_counter() - t0)
        m = re.search(r"\d+", txt)
        return int(m.group()) if m else None

    def text(self, region=None):
        if not self.ok:
            return ""
        img = self.g.grab(region)
        if img is None:
            return ""
        return pytesseract.image_to_string(
            ImageOps.autocontrast(ImageOps.grayscale(img))).lower()


# ------------------------------------------------------------------ webhook
def post_webhook(url, embed, img=None):
    """Returns None on success, else an error string."""
    try:
        ua = {"User-Agent": "TDSStrategy/2.0"}
        if img is None:
            body = json.dumps({"embeds": [embed]}).encode()
            headers = {"Content-Type": "application/json", **ua}
        else:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=70)
            embed["image"] = {"url": "attachment://shot.jpg"}
            bd = uuid.uuid4().hex
            body = b"".join([
                (f'--{bd}\r\nContent-Disposition: form-data; '
                 f'name="payload_json"\r\nContent-Type: application/json'
                 f'\r\n\r\n{json.dumps({"embeds": [embed]})}\r\n').encode(),
                (f'--{bd}\r\nContent-Disposition: form-data; '
                 f'name="files[0]"; filename="shot.jpg"\r\n'
                 f'Content-Type: image/jpeg\r\n\r\n').encode(),
                buf.getvalue(), f"\r\n--{bd}--\r\n".encode()])
            headers = {"Content-Type": f"multipart/form-data; boundary={bd}",
                       **ua}
        urllib.request.urlopen(urllib.request.Request(url, body, headers),
                               timeout=15).read()
        return None
    except Exception as e:
        return str(e)


# ------------------------------------------------------------------- runner
class Runner(threading.Thread):
    def __init__(self, strat, settings, log, game, reader, timing,
                 grind=True, max_matches=0):
        super().__init__(daemon=True)
        self.strat, self.s, self.log = strat, settings, log
        self.game, self.reader, self.timing = game, reader, timing
        self.grind, self.max_matches = grind, max_matches
        self.stop_evt = threading.Event()
        self.stats = dict(wins=0, losses=0, drops=0, start=time.time())
        self.state, self.wave, self.peak, self.cash = "idle", 0, 0, None
        self.last_end = 0.0
        self.pins = {p["id"]: p for p in strat.get("pins", [])}

    # ---- helpers
    def sleep(self, secs):
        if self.stop_evt.wait(secs):
            raise Stopped()

    def stop(self):
        self.stop_evt.set()

    def click(self, f):
        while not self.game.click(f):
            if not self.game.rect():
                return False
            self.log("Waiting for Roblox to be focused...")
            self.sleep(1)
        return True

    def key(self, ch):
        while not self.game.key(ch):
            if not self.game.rect():
                return False
            self.log("Waiting for Roblox to be focused...")
            self.sleep(1)
        return True

    def regions(self):
        # boxes picked on screen (Settings) win; old strategies still work
        return self.s.get("regions") or self.strat.get("regions", {})

    def read(self):
        r = self.regions()
        w = self.reader.number(r.get("wave"), first=True)
        c = self.reader.number(r.get("cash"))
        if w is not None and w <= self.wave + 3:
            self.low = self.low + 1 if w + 3 <= self.peak else 0
            self.wave = w
            self.peak = max(self.peak, w)
        if c is not None:
            self.cash = c

    def kw(self, key):
        return [w.strip().lower() for w in self.s.get(key, "").split(",")
                if w.strip()]

    def check_end(self):
        if not self.game.rect():
            return "drop"
        if not self.reader.ok or time.time() - self.last_end < 3:
            return None
        self.last_end = time.time()
        txt = self.reader.text(self.regions().get("result"))
        for key, res in (("drop_words", "drop"), ("win_words", "win"),
                         ("lose_words", "lose")):
            if any(w in txt for w in self.kw(key)):
                return res
        return None

    # ---- actions
    def verified(self, before, cost):
        if (not self.reader.ok or not cost or before is None
                or not self.regions().get("cash")):
            return True
        self.sleep(self.timing.d(0.25))
        self.read()
        return self.cash is not None and self.cash <= before - cost * 0.5

    def select(self, pin):
        self.click((pin["x"], pin["y"]))
        self.sleep(self.timing.d(0.12))

    def exec(self, s):
        t, ui = s.get("type"), self.strat.get("ui", {})
        pin = self.pins.get(s.get("pin"))
        cost = int(s.get("cost", 0) or 0)
        if t != "skip" and pin is None:
            self.log(f"Skipped {t}: pin not found")
            return
        if t == "place":
            for attempt in range(2):
                self.read()
                before = self.cash
                self.key(str(pin.get("slot", "1")))
                self.sleep(self.timing.d(0.08))
                self.click((pin["x"], pin["y"]))
                self.sleep(self.timing.d(0.15))
                if self.verified(before, cost):
                    self.log(f"Placed {pin['name']}")
                    return
                self.timing.bump()
                self.log("Placement not confirmed - retrying slower")
        elif t == "upgrade":
            path = int(s.get("path", 1) or 1)
            for attempt in range(2):
                self.read()
                before = self.cash
                self.select(pin)
                name = f"upgrade_p{path}"
                if name in ui:
                    self.click(ui[name])
                else:
                    self.key(self.s.get("upgrade_key", "e"))
                if self.verified(before, cost):
                    self.log(f"Upgraded {pin['name']} (path {path})")
                    return
                self.timing.bump()
        elif t == "ui":
            self.select(pin)
            for name in str(s.get("arg", "")).split(","):
                name = name.strip()
                if name in ui:
                    self.click(ui[name])
                    self.sleep(self.timing.d(0.1))
                elif name:
                    self.log(f"UI point '{name}' not set")
            self.log(f"{pin['name']}: {s.get('arg', '')}")
        elif t == "target":
            self.select(pin)
            for _ in range(int(s.get("arg", 1) or 1)):
                if "target" in ui:
                    self.click(ui["target"])
                    self.sleep(self.timing.d(0.1))
            self.log(f"Retargeted {pin['name']}")
        elif t == "skip":
            if "skip" in ui:
                self.click(ui["skip"])
                self.log("Skipped wave")
        elif t == "sell":
            self.select(pin)
            if "sell" in ui:
                self.click(ui["sell"])
            else:
                self.key(self.s.get("sell_key", "x"))
            self.log(f"Sold {pin['name']}")

    def gate_ok(self, s):
        if not self.reader.ok:
            return True
        cost = int(s.get("cost", 0) or 0)
        return (self.wave >= int(s.get("wave", 0) or 0)
                and (cost <= 0 or self.cash is None or self.cash >= cost))

    # ---- one match
    def run_match(self):
        steps = self.strat.get("steps", [])
        seq = [s for s in steps if not float(s.get("cd", 0) or 0)]
        rep = [s for s in steps if float(s.get("cd", 0) or 0)]
        nxt = {id(s): 0.0 for s in rep}
        i, gate, self.low = 0, None, 0
        self.wave = self.peak = 0
        self.cash = None
        self.state = "playing"
        if not self.reader.ok:
            self.log("No OCR: running by delays only; stop it manually")
        while True:
            self.sleep(0.05)
            end = self.check_end()
            if end:
                return end
            self.read()
            if self.low >= 3:
                return "reset"
            now = time.time()
            if i < len(seq):
                s = seq[i]
                if self.gate_ok(s):
                    gate = gate or now
                    if now - gate >= float(s.get("delay", 0) or 0):
                        self.exec(s)
                        i, gate = i + 1, None
                        continue
                else:
                    gate = None
            for s in rep:
                until = int(s.get("until", 0) or 0)
                if (self.wave >= int(s.get("wave", 0) or 0)
                        and (not until or self.wave <= until)
                        and now >= nxt[id(s)]):
                    self.exec(s)
                    nxt[id(s)] = time.time() + float(s["cd"])
                    break

    # ---- grind loop
    def play_macro(self, path):
        if path and not os.path.exists(path):  # moved/reinstalled? try the
            alt = os.path.join(tm.data_dir(), os.path.basename(path))
            path = alt if os.path.exists(alt) else path  # macros folder
        if not path or not os.path.exists(path):
            return
        try:
            with open(path) as f:
                events = json.load(f)
        except Exception as e:
            self.log(f"Macro load failed: {e}")
            return
        self.log(f"Playing macro {os.path.basename(path)}")
        eng = tm.Engine(queue.Queue(), lambda: "x")
        eng.events = events
        eng.cfg.update(loops=1, speed=1.0, settle=4.0, safety=False)
        eng.start_play()
        try:
            while eng.state != "idle":
                self.sleep(0.1)
        except Stopped:
            eng.stop("Stopped")
            raise

    def rejoin(self):
        self.state = "rejoining"
        self.log("Rejoining Roblox...")
        subprocess.run(["taskkill", "/IM", "RobloxPlayerBeta.exe", "/F"],
                       capture_output=True, creationflags=0x08000000)
        self.sleep(2)
        os.startfile(f"roblox://placeId={self.s.get('place_id')}")
        for _ in range(120):
            if self.game.rect():
                break
            self.sleep(1)
        else:
            self.log("Roblox never opened")
            return False
        self.sleep(float(self.s.get("join_wait", 20)))
        self.play_macro(self.s.get("lobby_macro"))
        return True

    def wait_match(self):
        self.state = "waiting"
        self.wave, self.peak, self.cash, self.low = 0, 0, None, 0
        if not self.reader.ok or not self.regions().get("wave"):
            self.sleep(float(self.s.get("start_delay", 5)))
            return True
        t0 = time.time()
        while time.time() - t0 < float(self.s.get("match_timeout", 240)):
            self.read()
            if self.wave >= 1:
                return True
            if self.check_end() == "drop":
                return False
            self.sleep(1)
        return False

    def finish(self, result):
        st = self.stats
        key = {"win": "wins", "lose": "losses", "drop": "drops"}.get(result)
        if key:
            st[key] += 1
        self.log(f"Match result: {result} (wave {self.peak})")
        url = self.s.get("webhook", "").strip()
        if not url:
            return
        total = st["wins"] + st["losses"]
        mins = (time.time() - st["start"]) / 60
        color = {"win": 0x3DDC97, "lose": 0xFF5B5B}.get(result, 0xFFB454)
        embed = {
            "title": {"win": "Victory", "lose": "Defeat",
                      "drop": "Disconnected", "reset": "Match reset"}[result],
            "color": color,
            "description": self.strat.get("name", "strategy"),
            "fields": [
                {"name": "Wave reached", "value": str(self.peak), "inline": True},
                {"name": "W / L / DC",
                 "value": f"{st['wins']} / {st['losses']} / {st['drops']}",
                 "inline": True},
                {"name": "Win rate", "inline": True,
                 "value": f"{st['wins'] * 100 // total}%" if total else "-"},
                {"name": "Session", "value": f"{mins:.0f} min", "inline": True}]}
        img = None
        try:
            img = self.game.grab()
        except Exception:
            pass
        def send_it():
            err = post_webhook(url, embed, img)
            if err:
                self.log("Webhook failed: " + err)

        threading.Thread(target=send_it, daemon=True).start()

    def run(self):
        fails, matches = 0, 0
        try:
            while True:
                if not self.game.rect() and not self.rejoin():
                    fails += 1
                    if fails >= 3:
                        self.log("Could not reach the game 3 times - stopping")
                        break
                    continue
                ok = self.wait_match()
                if not ok:
                    fails += 1
                    self.log("No match detected - rejoining")
                    if fails >= 3:
                        self.log("Giving up after 3 failed attempts")
                        break
                    self.rejoin()
                    continue
                fails = 0
                result = self.run_match()
                self.finish(result)
                matches += 1
                if not self.grind or (self.max_matches
                                      and matches >= self.max_matches):
                    break
                if result == "drop":
                    self.rejoin()
                    continue
                self.state = "between"
                self.sleep(float(self.s.get("end_wait", 8)))
                self.play_macro(self.s.get("restart_macro"))
        except Stopped:
            pass
        except Exception as e:
            self.log(f"Runner error: {e}")
        finally:
            self.state = "idle"
            self.log("Run ended")


# -------------------------------------------------------------------- micro
class Micro:
    """Hand-play helpers: spam upgrade key / re-fire abilities on cooldown."""

    def __init__(self, game, get_settings, get_strat, log):
        self.g, self.gs, self.gt, self.log = game, get_settings, get_strat, log
        self.spam, self.abil = threading.Event(), threading.Event()
        threading.Thread(target=self._spam, daemon=True).start()
        threading.Thread(target=self._abil, daemon=True).start()

    def toggle(self, which):
        ev = self.spam if which == "spam" else self.abil
        if ev.is_set():
            ev.clear()
        else:
            ev.set()
        self.log(f"Micro {which}: {'ON' if ev.is_set() else 'off'}")

    def stop_all(self):
        self.spam.clear()
        self.abil.clear()

    def _spam(self):
        while True:
            self.spam.wait()
            self.g.key(self.gs().get("upgrade_key", "e"), refocus=False)
            time.sleep(self.g.t.d(0.1))

    def _abil(self):
        nxt = {}
        while True:
            self.abil.wait()
            strat = self.gt()
            for item in str(strat.get("micro", "")).split(","):
                name, _, cd = item.strip().partition(":")
                p = strat.get("ui", {}).get(name)
                if p and time.time() >= nxt.get(name, 0):
                    if self.g.click(p, refocus=False):
                        nxt[name] = time.time() + float(cd or 30)
            time.sleep(0.1)
