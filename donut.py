#!/usr/bin/env python3
"""
donut.py - smooth, flicker-free rotating 3D donut for the terminal.

Works on Windows 10/11 (VS Code, Windows Terminal, PowerShell, cmd), Linux,
macOS and over SSH. Pure Python 3.8+; if NumPy is installed it is used
automatically for a much faster renderer (pip install numpy).

Run:
    python3 donut.py
    python3 donut.py --fps 60 --theme neon --color truecolor

Keys:
    q / Esc   quit              Space   pause / resume
    + / -     speed             c       next theme
    [ / ]     brightness        t       tumble mode on/off
    ?         help bar on/off   r       reset

Design notes
------------
* Torus samples (points, normals, colours) are generated once per layout and
  only rotated per frame. The rotation is applied to the light and camera
  vectors instead of every normal, and back-facing samples are culled with a
  3-multiply test, so roughly half of the surface is skipped early.
* Sample density follows the projected size, so small and large terminals both
  render without holes. A built-in governor lowers density if the CPU cannot
  keep up with the target frame rate.
* Output is minimal: colours are quantised to a small table of escape codes,
  runs of equal cells share one escape, unchanged rows are not resent, and
  each frame is wrapped in a synchronized-update block, so there is no
  flicker or tearing even over slow SSH links.
* The terminal is always restored: on q, Ctrl+C, SIGTERM, SIGHUP, errors, and
  Ctrl+Z (suspend / fg) is handled properly. Window resizes are detected live.
"""

from __future__ import annotations

import argparse
import math
import os
import select
import signal
import sys
import time
from itertools import groupby

IS_WINDOWS = os.name == "nt"

try:
    import termios
    import tty
except ImportError:  # Windows
    termios = tty = None

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

try:
    import numpy as np
except ImportError:
    np = None


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

TAU = math.tau
R_TUBE = 1.0                     # minor radius
R_RING = 2.0                     # major radius
R_OUT = R_TUBE + R_RING
CAM = 8.0                        # camera distance from torus centre
ASPECT = 2.0                     # terminal cell height / width
EXTENT = R_OUT / math.sqrt(CAM * CAM - R_OUT * R_OUT)  # exact max |x/z| on screen

RAMP = ".,-~:;=!*#$@"            # dark -> bright
LEVELS = len(RAMP)
AMBIENT = 0.16
OVERSAMPLE = 1.35                # samples per projected cell
Q_MIN = 0.35                     # lowest quality the governor may use

HUE_BUCKETS = 24


def _normalize(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


LIGHT = _normalize((-0.45, 0.75, -0.55))   # towards the light (upper-left, front)

# name, gradient (None = glazed donut materials)
THEMES = [
    ("glazed", None),
    ("neon",   [(0, 255, 190), (0, 190, 255), (100, 70, 255), (255, 45, 180)]),
    ("sunset", [(255, 60, 80), (255, 130, 30), (255, 230, 70), (255, 70, 150)]),
    ("toxic",  [(90, 255, 80), (0, 220, 140), (0, 160, 255), (170, 100, 255)]),
    ("ice",    [(240, 240, 255), (120, 180, 255), (80, 100, 255), (255, 100, 220)]),
]

# Glazed donut materials: dough, glaze, then sprinkles.
DONUT_COLORS = [
    (214, 150, 82),    # dough
    (255, 112, 182),   # pink glaze
    (255, 255, 255),
    (90, 200, 255),
    (255, 225, 60),
    (120, 240, 120),
    (255, 80, 80),
]
N_SPRINKLE = len(DONUT_COLORS) - 2

RESET = "\x1b[0m"
SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"


# --------------------------------------------------------------------------- #
# Colour helpers
# --------------------------------------------------------------------------- #

def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def lerp_rgb(a, b, f):
    return tuple(a[k] + (b[k] - a[k]) * f for k in range(3))


def gradient(colors, t):
    """Cyclic interpolation through a list of RGB colours, t in [0, 1)."""
    p = (t % 1.0) * len(colors)
    i = int(p)
    return lerp_rgb(colors[i % len(colors)], colors[(i + 1) % len(colors)], p - i)


def shade(rgb, level, brightness):
    """Colour of a base RGB at a lighting level, with a soft specular top."""
    f = (0.20 + 0.80 * level / (LEVELS - 1)) * brightness
    c = tuple(v * f for v in rgb)
    if level >= LEVELS - 2:
        c = lerp_rgb(c, (255, 255, 255), 0.18 * (level - (LEVELS - 3)))
    return tuple(int(clamp(v, 0, 255)) for v in c)


def rgb_to_256(r, g, b):
    def q(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


def fg_escape(rgb, mode):
    if mode == "truecolor":
        return "\x1b[38;2;%d;%d;%dm" % rgb
    if mode == "256":
        return "\x1b[38;5;%dm" % rgb_to_256(*rgb)
    return ""


def detect_color_mode():
    if os.environ.get("NO_COLOR"):
        return "mono"
    if IS_WINDOWS:
        return "truecolor"      # every Windows 10+ VT console supports 24-bit colour
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "truecolor"
    if os.environ.get("TERM", "") in ("", "dumb"):
        return "mono"
    return "256"


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def _hash2(a, b):
    h = (a * 374761393 + b * 668265263) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return h ^ (h >> 16)


def build_torus(samples_per_unit):
    """
    Sample the torus surface. The number of samples around the ring scales
    with the local ring radius, so density is uniform over the surface.
    Returns column lists: x, y, z, nx, ny, nz, n.p, hue key, material key.
    """
    n_theta = max(10, int(math.ceil(TAU * R_TUBE * samples_per_unit)))
    phi_per_radius = TAU * samples_per_unit
    xs, ys, zs, nxs, nys, nzs, dots, hues, mats = ([] for _ in range(9))

    for i in range(n_theta):
        th = TAU * (i + 0.5) / n_theta
        ct, st = math.cos(th), math.sin(th)
        rad = R_RING + R_TUBE * ct
        n_phi = max(16, int(math.ceil(phi_per_radius * rad)))
        stagger = 0.5 * (i & 1)
        gt = int(th / TAU * 34)
        for j in range(n_phi):
            ph = TAU * (j + stagger) / n_phi
            cp, sp = math.cos(ph), math.sin(ph)
            xs.append(rad * cp)
            ys.append(R_TUBE * st)
            zs.append(rad * sp)
            nxs.append(ct * cp)
            nys.append(st)
            nzs.append(ct * sp)
            dots.append(R_RING * ct + R_TUBE)          # n . p, constant per sample
            hues.append(int(ph / TAU * HUE_BUCKETS) % HUE_BUCKETS)

            # Wavy glaze edge on the top half, deterministic sprinkles on it.
            edge = -0.18 + 0.14 * math.sin(5 * ph + 0.7) + 0.06 * math.sin(13 * ph)
            if st > edge:
                mat = 1
                if st > edge + 0.12:
                    h = _hash2(int(ph / TAU * 96), gt)
                    if h % 9 == 0:
                        mat = 2 + (h >> 5) % N_SPRINKLE
            else:
                mat = 0
            mats.append(mat)

    return xs, ys, zs, nxs, nys, nzs, dots, hues, mats


def rotation(a, b, c):
    """R = Rz(c) @ Rx(a) @ Ry(b) as a 3x3 tuple of rows."""
    ca, sa = math.cos(a), math.sin(a)
    cb, sb = math.cos(b), math.sin(b)
    cc, sc = math.cos(c), math.sin(c)
    # Rx(a) @ Ry(b)
    m = ((cb, 0.0, sb),
         (sa * sb, ca, -sa * cb),
         (-ca * sb, sa, ca * cb))
    return ((cc * m[0][0] - sc * m[1][0], cc * m[0][1] - sc * m[1][1], cc * m[0][2] - sc * m[1][2]),
            (sc * m[0][0] + cc * m[1][0], sc * m[0][1] + cc * m[1][1], sc * m[0][2] + cc * m[1][2]),
            m[2])


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #

class Donut:
    def __init__(self, fps, density, theme, color_mode, use_numpy, speed):
        self.target_fps = clamp(fps, 10, 120)
        self.frame_time = 1.0 / self.target_fps
        self.density = clamp(density, 0.4, 2.0)
        self.color_mode = color_mode
        self.use_numpy = use_numpy and np is not None
        self.default_speed = clamp(speed, 0.1, 5.0)
        self.default_theme = theme
        self.reset()

        self.quality = 1.0
        self.cost_ema = None
        self.last_tune = 0.0
        self.w = self.h = self.hs = 0
        self.prev_rows = []
        self.geom = None
        self.shift = -1

        self.fps_shown = 0.0
        self.fps_frames = 0
        self.fps_t0 = time.perf_counter()

    # ---- state ---------------------------------------------------------- #

    def reset(self):
        self.speed = self.default_speed
        self.brightness = 1.0
        self.theme = self.default_theme
        self.tumble = False
        self.paused = False
        self.help_on = True
        self.phase = 0.0
        self.spin = 0.0
        self.roll = 0.0
        self.tables_dirty = True
        self.layout_dirty = True

    # ---- layout --------------------------------------------------------- #

    def set_size(self, cols, rows):
        if (cols, rows) != (self.w, self.h):
            self.w, self.h = cols, rows
            self.layout_dirty = True

    def _layout(self):
        """Fit the donut to the scene area and (re)build the sample set."""
        self.hs = max(1, self.h - 1 - (1 if self.help_on else 0))
        w, hs = self.w, self.hs
        ky = (hs / 2.0 - 0.6) / EXTENT
        kx = (w / 2.0 - 1.2) / EXTENT
        ky = max(1.0, min(ky, kx / ASPECT))
        self.ky = ky
        self.kx = ky * ASPECT
        self.cx = w / 2.0
        self.cy = hs / 2.0
        self._build_samples()
        self.prev_rows = []
        self.layout_dirty = False

    def _build_samples(self):
        spu = (self.kx / CAM) * OVERSAMPLE * self.density * self.quality
        cols = build_torus(max(1.5, spu))
        xs, ys, zs, nxs, nys, nzs, dots, hues, mats = cols
        self.keys_hue = [k * LEVELS for k in hues]
        self.keys_mat = [k * LEVELS for k in mats]
        self.n_samples = len(xs)
        if self.use_numpy:
            self.P = np.array([xs, ys, zs], dtype=np.float64).T.copy()
            self.N = np.array([nxs, nys, nzs], dtype=np.float64).T.copy()
            self.D = np.array(dots, dtype=np.float64)
            self.K_hue = np.array(self.keys_hue, dtype=np.int32)
            self.K_mat = np.array(self.keys_mat, dtype=np.int32)
        else:
            self.geom = (xs, ys, zs, nxs, nys, nzs, dots)

    # ---- colour tables -------------------------------------------------- #

    def _build_tables(self):
        _, grad = THEMES[self.theme]
        bases = DONUT_COLORS if grad is None else [
            gradient(grad, k / HUE_BUCKETS) for k in range(HUE_BUCKETS)]
        self.base_esc = [fg_escape(shade(rgb, lv, self.brightness), self.color_mode)
                         for rgb in bases for lv in range(LEVELS)]
        self.chars = [RAMP[i % LEVELS] for i in range(len(self.base_esc))]
        self.shift = -1
        self.tables_dirty = False

    def _update_shift(self):
        """Neon themes flow around the ring by rotating the colour table."""
        if THEMES[self.theme][1] is None:
            shift = 0
        else:
            shift = int(self.phase * 3.0) % HUE_BUCKETS
        if shift != self.shift:
            s = shift * LEVELS
            self.esc = self.base_esc[s:] + self.base_esc[:s]
            self.shift = shift
            self.prev_rows = []          # colours changed under the same codes

    # ---- animation ------------------------------------------------------ #

    def advance(self, dt):
        if self.paused:
            return
        d = dt * self.speed
        self.phase += d
        self.spin += 1.15 * d
        self.roll += 0.37 * d

    def matrix(self):
        if self.tumble:
            return rotation(-0.6 - 0.8 * self.phase, self.spin, 0.45 * self.roll)
        tilt = -(0.95 + 0.55 * math.sin(0.5 * self.phase))
        return rotation(tilt, self.spin, 0.22 * math.sin(self.roll))

    # ---- core rasteriser ------------------------------------------------ #

    def _shade_params(self):
        top = LEVELS - 1
        base = AMBIENT * top + 0.5
        return base, (1.0 - AMBIENT) * top, int(base)

    def _rasterise_py(self, R):
        w, hs = self.w, self.hs
        size = w * hs
        zbuf = [0.0] * size
        cbuf = [-1] * size
        (r00, r01, r02), (r10, r11, r12), (r20, r21, r22) = R
        ex, ey, ez = CAM * r20, CAM * r21, CAM * r22
        lx = r00 * LIGHT[0] + r10 * LIGHT[1] + r20 * LIGHT[2]
        ly = r01 * LIGHT[0] + r11 * LIGHT[1] + r21 * LIGHT[2]
        lz = r02 * LIGHT[0] + r12 * LIGHT[1] + r22 * LIGHT[2]
        cx, cy, kx, ky = self.cx, self.cy, self.kx, self.ky
        base, span, lo = self._shade_params()
        keys = self.keys_mat if THEMES[self.theme][1] is None else self.keys_hue
        xs, ys, zs, nxs, nys, nzs, dots = self.geom

        for x, y, z, a, b, c, s, k in zip(xs, ys, zs, nxs, nys, nzs, dots, keys):
            if s + a * ex + b * ey + c * ez >= 0.0:      # back-facing
                continue
            inv = 1.0 / (r20 * x + r21 * y + r22 * z + CAM)
            sx = int(cx + kx * (r00 * x + r01 * y + r02 * z) * inv)
            if sx < 0 or sx >= w:
                continue
            sy = int(cy - ky * (r10 * x + r11 * y + r12 * z) * inv)
            if sy < 0 or sy >= hs:
                continue
            i = sy * w + sx
            if inv > zbuf[i]:
                zbuf[i] = inv
                d = a * lx + b * ly + c * lz
                cbuf[i] = k + (int(base + span * d) if d > 0.0 else lo)

        return [cbuf[r * w:(r + 1) * w] for r in range(hs)]

    def _rasterise_np(self, R):
        w, hs = self.w, self.hs
        Rm = np.array(R, dtype=np.float64)
        e_loc = CAM * Rm[2]
        l_loc = Rm.T @ np.array(LIGHT)
        keys = self.K_mat if THEMES[self.theme][1] is None else self.K_hue

        front = (self.D + self.N @ e_loc) < 0.0
        P = self.P[front]
        pr = P @ Rm.T
        inv = 1.0 / (pr[:, 2] + CAM)
        sx = (self.cx + self.kx * pr[:, 0] * inv).astype(np.int32)
        sy = (self.cy - self.ky * pr[:, 1] * inv).astype(np.int32)
        ok = (sx >= 0) & (sx < w) & (sy >= 0) & (sy < hs)

        d = self.N[front] @ l_loc
        base, span, lo = self._shade_params()
        lv = np.where(d > 0.0, (base + span * d).astype(np.int32), lo)
        code = keys[front] + lv

        if not ok.all():
            sx, sy, inv, code = sx[ok], sy[ok], inv[ok], code[ok]
        cbuf = np.full(w * hs, -1, dtype=np.int32)
        if inv.size:
            idx = sy * w + sx
            # Sort by cell, nearest first; keep the first sample of each cell.
            t = (inv - 1.0 / (CAM + R_OUT)) / (1.0 / (CAM - R_OUT) - 1.0 / (CAM + R_OUT))
            order = np.argsort(idx - 0.5 * t)
            idx_s = idx[order]
            first = np.empty(idx_s.size, dtype=bool)
            first[0] = True
            np.not_equal(idx_s[1:], idx_s[:-1], out=first[1:])
            win = order[first]
            cbuf[idx[win]] = code[win]
        return cbuf.reshape(hs, w).tolist()

    # ---- output --------------------------------------------------------- #

    def _row_string(self, codes):
        esc, chars = self.esc, self.chars
        out = []
        last = -1
        for code, run in groupby(codes):
            n = sum(1 for _ in run)
            if code < 0:
                out.append(" " * n)
            elif code == last:
                out.append(chars[code] * n)
            else:
                out.append(esc[code] + chars[code] * n)
                last = code
        out.append(RESET)
        return "".join(out)

    def _bar(self, text, kind):
        text = text[:self.w].ljust(self.w)
        if self.color_mode == "mono":
            return "\x1b[7m" + text + RESET
        if kind == "hud":
            style = ("\x1b[48;2;18;14;28m\x1b[38;2;255;170;210m"
                     if self.color_mode == "truecolor" else "\x1b[48;5;234m\x1b[38;5;218m")
        else:
            style = ("\x1b[48;2;10;16;28m\x1b[38;2;150;210;255m"
                     if self.color_mode == "truecolor" else "\x1b[48;5;233m\x1b[38;5;153m")
        return style + text + RESET

    def _hud(self):
        backend = "numpy" if self.use_numpy else "python"
        state = "PAUSED" if self.paused else ("tumble" if self.tumble else "spin")
        return (f" DONUT | {self.fps_shown:5.1f} fps | theme {THEMES[self.theme][0]}"
                f" | speed {self.speed:.2f}x | {state} | {backend}"
                f" q{int(self.quality * 100)}% ")

    def _too_small(self):
        msg = "Terminal too small"
        rows = []
        for y in range(self.h):
            line = msg.center(self.w)[:self.w] if y == self.h // 2 else " " * self.w
            rows.append(f"\x1b[{y + 1};1H{line}")
        return SYNC_BEGIN + RESET + "".join(rows) + SYNC_END

    def frame(self, force=False):
        """Return the escape string for one frame (only changed rows)."""
        if self.w < 24 or self.h < 8:
            self.prev_rows = []
            return self._too_small()
        if self.layout_dirty:
            self._layout()
            force = True
        if self.tables_dirty:
            self._build_tables()
            force = True
        self._update_shift()
        if force:
            self.prev_rows = []

        t0 = time.perf_counter()
        R = self.matrix()
        rows = self._rasterise_np(R) if self.use_numpy else self._rasterise_py(R)

        parts = [SYNC_BEGIN, "\x1b[1;1H", self._bar(self._hud(), "hud")]
        prev = self.prev_rows
        for y, row in enumerate(rows):
            if y < len(prev) and prev[y] == row:
                continue
            parts.append(f"\x1b[{y + 2};1H")
            parts.append(self._row_string(row))
        if self.help_on:
            parts.append(f"\x1b[{self.h};1H")
            parts.append(self._bar(
                " q quit | space pause | +/- speed | c theme | t tumble"
                " | [ ] brightness | r reset | ? hide help", "help"))
        parts.append(SYNC_END)
        self.prev_rows = rows

        self._govern(time.perf_counter() - t0)
        self._count_fps()
        return "".join(parts)

    # ---- performance governor ------------------------------------------ #

    def _govern(self, cost):
        self.cost_ema = cost if self.cost_ema is None else 0.85 * self.cost_ema + 0.15 * cost
        now = time.perf_counter()
        if now - self.last_tune < 0.6:
            return
        budget = 0.6 * self.frame_time
        q = self.quality
        if self.cost_ema > budget and q > Q_MIN:
            q = max(Q_MIN, q * 0.85)
        elif self.cost_ema < 0.45 * budget and q < 1.0:
            q = min(1.0, q * 1.12)
        if q != self.quality:
            self.quality = q
            self._build_samples()
            self.cost_ema = None
            self.last_tune = time.perf_counter()

    def _count_fps(self):
        self.fps_frames += 1
        now = time.perf_counter()
        if now - self.fps_t0 >= 0.5:
            self.fps_shown = self.fps_frames / (now - self.fps_t0)
            self.fps_frames = 0
            self.fps_t0 = now

    # ---- input ---------------------------------------------------------- #

    def handle_input(self, data):
        """Process raw key bytes. Returns False when the user wants to quit."""
        i, n = 0, len(data)
        while i < n:
            ch = data[i]
            i += 1
            if ch == "\x1b":
                if i < n and data[i] in "[O":      # CSI / SS3 sequence: skip it
                    i += 1
                    while i < n and not ("@" <= data[i] <= "~"):
                        i += 1
                    i += 1
                    continue
                return False                       # lone Esc
            if ch in "qQ\x03":
                return False
            if ch == " ":
                self.paused = not self.paused
            elif ch in "+=":
                self.speed = min(5.0, round(self.speed + 0.25, 2))
            elif ch in "-_":
                self.speed = max(0.25, round(self.speed - 0.25, 2))
            elif ch in "cC":
                self.theme = (self.theme + 1) % len(THEMES)
                self.tables_dirty = True
            elif ch == "]":
                self.brightness = min(1.6, round(self.brightness + 0.1, 2))
                self.tables_dirty = True
            elif ch == "[":
                self.brightness = max(0.4, round(self.brightness - 0.1, 2))
                self.tables_dirty = True
            elif ch in "tT":
                self.tumble = not self.tumble
            elif ch in "?hH":
                self.help_on = not self.help_on
                self.layout_dirty = True
            elif ch in "rR":
                self.reset()
        return True


# --------------------------------------------------------------------------- #
# Terminal handling
# --------------------------------------------------------------------------- #

ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[0m\x1b[2J"
LEAVE = "\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l"


class WindowsConsole:
    """Enable ANSI/VT output on the Windows console and disable QuickEdit
    (which would freeze the program when the window is clicked)."""

    ENABLE_PROCESSED_OUTPUT = 0x0001
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    ENABLE_QUICK_EDIT_MODE = 0x0040
    ENABLE_EXTENDED_FLAGS = 0x0080

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes = (wintypes.DWORD,)
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetConsoleMode.restype = wintypes.BOOL
        k32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.SetConsoleMode.restype = wintypes.BOOL
        self.k32 = k32
        self.DWORD = wintypes.DWORD
        self.saved = []

    def _mode(self, handle):
        mode = self.DWORD()
        if not self.k32.GetConsoleMode(handle, self.ctypes.byref(mode)):
            return None
        return mode.value

    def enable(self):
        h_out = self.k32.GetStdHandle(0xFFFFFFF5)   # STD_OUTPUT_HANDLE (-11)
        h_in = self.k32.GetStdHandle(0xFFFFFFF6)    # STD_INPUT_HANDLE (-10)
        out_mode = self._mode(h_out)
        if out_mode is None:
            raise RuntimeError("stdout is not a Windows console")
        new_out = out_mode | self.ENABLE_PROCESSED_OUTPUT | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if not self.k32.SetConsoleMode(h_out, new_out):
            raise RuntimeError("this console does not support ANSI colours (needs Windows 10+)")
        self.saved.append((h_out, out_mode))
        in_mode = self._mode(h_in)
        if in_mode is not None:
            new_in = (in_mode | self.ENABLE_EXTENDED_FLAGS) & ~self.ENABLE_QUICK_EDIT_MODE
            if self.k32.SetConsoleMode(h_in, new_in):
                self.saved.append((h_in, in_mode))

    def restore(self):
        for handle, mode in reversed(self.saved):
            self.k32.SetConsoleMode(handle, mode)
        self.saved = []


class Terminal:
    def __init__(self):
        self.fd_in = sys.stdin.fileno()
        self.out = sys.stdout.buffer
        self.saved = None
        self.win = WindowsConsole() if IS_WINDOWS else None

    def enter(self):
        if self.win is not None:
            self.win.enable()
        else:
            self.saved = termios.tcgetattr(self.fd_in)
            tty.setcbreak(self.fd_in)
        self.write(ENTER)

    def leave(self):
        try:
            self.write(SYNC_END + LEAVE)
        except OSError:
            pass
        if self.win is not None:
            self.win.restore()
            return
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd_in, termios.TCSADRAIN, self.saved)
            except termios.error:
                pass
            self.saved = None

    def write(self, s):
        self.out.write(s.encode("ascii", "replace"))
        self.out.flush()

    def size(self):
        try:
            sz = os.get_terminal_size(sys.stdout.fileno())
            return sz.columns, sz.lines
        except OSError:
            return 80, 24

    def read_keys(self, timeout):
        if self.win is not None:
            return self._read_keys_windows(timeout)
        r, _, _ = select.select([self.fd_in], [], [], max(0.0, timeout))
        if not r:
            return None
        try:
            data = os.read(self.fd_in, 1024)
        except OSError:
            return None
        return data.decode("latin-1")

    @staticmethod
    def _read_keys_windows(timeout):
        """Wait up to `timeout` seconds for key presses (Windows console)."""
        end = time.perf_counter() + max(0.0, timeout)
        while True:
            if msvcrt.kbhit():
                chars = []
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):        # arrows / F-keys: drop scan code
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        continue
                    chars.append(ch)
                return "".join(chars) or None
            remaining = end - time.perf_counter()
            if remaining <= 0:
                return None
            time.sleep(min(0.003, remaining))


class Signals:
    """Turn async signals into flags the main loop handles safely."""

    def __init__(self):
        self.quit = False
        self.suspend = False
        self.resized = False

    def install(self):
        handlers = (("SIGTERM", self._quit), ("SIGHUP", self._quit),
                    ("SIGBREAK", self._quit), ("SIGWINCH", self._winch),
                    ("SIGTSTP", self._tstp))
        for name, handler in handlers:
            signum = getattr(signal, name, None)    # not every OS has every signal
            if signum is not None:
                try:
                    signal.signal(signum, handler)
                except (OSError, ValueError):
                    pass

    def _quit(self, *_):
        self.quit = True

    def _winch(self, *_):
        self.resized = True

    def _tstp(self, *_):
        self.suspend = True


def run(engine):
    term = Terminal()
    sig = Signals()
    sig.install()
    try:
        term.enter()
    except (RuntimeError, OSError) as exc:
        term.leave()
        print(f"Cannot start: {exc}")
        return 2
    try:
        prev = time.perf_counter()
        deadline = prev
        force = True
        while not sig.quit:
            if sig.suspend:                       # Ctrl+Z: restore, stop, resume
                sig.suspend = False
                term.leave()
                signal.signal(signal.SIGTSTP, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTSTP)
                signal.signal(signal.SIGTSTP, sig._tstp)
                term.enter()
                prev = deadline = time.perf_counter()
                force = True

            cols, rows = term.size()
            if sig.resized or (cols, rows) != (engine.w, engine.h):
                sig.resized = False
                term.write("\x1b[0m\x1b[2J")
                engine.set_size(cols, rows)
                force = True

            now = time.perf_counter()
            dt = min(0.1, now - prev)
            prev = now
            engine.advance(dt)

            if force or not engine.paused or engine.tables_dirty or engine.layout_dirty:
                term.write(engine.frame(force))
                force = False

            # Frame pacing: wait for the next deadline while staying responsive.
            deadline += engine.frame_time
            now = time.perf_counter()
            if deadline < now - engine.frame_time:
                deadline = now                    # fell behind: don't spiral
            while not sig.quit and not sig.suspend:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                keys = term.read_keys(remaining)
                if keys:
                    was_paused = engine.paused
                    if not engine.handle_input(keys):
                        sig.quit = True
                    elif engine.paused != was_paused or engine.paused:
                        force = True
    except KeyboardInterrupt:
        pass
    finally:
        term.leave()
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Smooth rotating 3D donut for the terminal.")
    ap.add_argument("--fps", type=int, default=30, help="target frame rate (10-120), default 30")
    ap.add_argument("--density", type=float, default=1.0,
                    help="surface sampling density (0.4-2.0), default 1.0")
    ap.add_argument("--theme", choices=[t[0] for t in THEMES], default="glazed")
    ap.add_argument("--color", choices=["auto", "truecolor", "256", "mono"], default="auto",
                    help="colour mode (auto: truecolor if COLORTERM says so, else 256)")
    ap.add_argument("--speed", type=float, default=1.0, help="animation speed, default 1.0")
    ap.add_argument("--no-numpy", action="store_true", help="force the pure-Python renderer")
    args = ap.parse_args()

    if (IS_WINDOWS and msvcrt is None) or (not IS_WINDOWS and termios is None):
        print("Unsupported platform: needs Windows 10+, Linux or macOS.")
        return 2
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Run this in an interactive terminal, not through a pipe.")
        return 2

    color = detect_color_mode() if args.color == "auto" else args.color
    theme = [t[0] for t in THEMES].index(args.theme)
    engine = Donut(args.fps, args.density, theme, color, not args.no_numpy, args.speed)
    return run(engine)


if __name__ == "__main__":
    sys.exit(main())
