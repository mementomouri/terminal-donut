<div align="center">

# 🍩 Terminal Donut

**A smooth, flicker-free 3D donut that spins in your terminal.**

Pure Python · zero required dependencies · Windows, Linux, macOS and SSH

![Python](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-555)
![Dependencies](https://img.shields.io/badge/dependencies-none-2ea44f)
![License](https://img.shields.io/badge/license-MIT-blue)

<!-- Record a short clip, save it as assets/demo.gif, then remove this comment line and the one below:
![demo](assets/demo.gif)
-->

[فارسی](#فارسی) · [English](#features)

</div>

---

## Features

- **Real 3D rendering**: perspective projection, depth buffer, diffuse lighting and a soft specular highlight.
- **Five themes**: a pink glazed donut with sprinkles, plus four animated neon gradients.
- **No lag**: geometry is built once and only rotated each frame, and back faces are culled early. A built-in governor lowers detail when the CPU can't keep up, so the frame rate stays steady.
- **No flicker**: unchanged rows are never resent, colour codes are run-length packed, and each frame is drawn in one synchronized update. It stays smooth even over slow SSH links.
- **Optional NumPy acceleration**: if NumPy is installed it is used automatically. Without it, the pure-Python renderer still runs comfortably at 30–60 FPS.
- **Safe terminal handling**: the terminal is always restored on quit, Ctrl+C, errors and kill signals. Window resizing is handled live, and Ctrl+Z / `fg` works on Linux and macOS.
- **Cross-platform**: Windows 10/11 (VS Code, Windows Terminal, PowerShell, cmd), Linux, macOS and SSH sessions.

## Quick start

```bash
git clone https://github.com/mementomouri/terminal-donut.git
cd terminal-donut
python donut.py
```

On Linux and macOS, use `python3` if `python` points to Python 2.

### Optional: faster rendering

```bash
pip install numpy
```

## Controls

| Key | Action |
|-----|--------|
| `q` / `Esc` | Quit |
| `Space` | Pause / resume |
| `+` / `-` | Speed up / slow down |
| `c` | Next theme |
| `t` | Toggle full tumble rotation |
| `[` / `]` | Darker / brighter |
| `r` | Reset everything |
| `?` | Show / hide the help bar |

## Options

```bash
python donut.py --fps 60 --theme neon --color truecolor
```

| Option | Default | Description |
|--------|---------|-------------|
| `--fps N` | `30` | Target frame rate (10–120) |
| `--theme NAME` | `glazed` | `glazed`, `neon`, `sunset`, `toxic` or `ice` |
| `--color MODE` | `auto` | `auto`, `truecolor`, `256` or `mono` |
| `--speed X` | `1.0` | Animation speed multiplier |
| `--density X` | `1.0` | Surface sampling density (0.4–2.0) |
| `--no-numpy` | off | Force the pure-Python renderer |

**Colour mode:** on Windows, `auto` picks 24-bit colour. On Linux and macOS it picks 24-bit colour only when `COLORTERM` says the terminal supports it, and 256 colours otherwise. Pass `--color truecolor` for smoother gradients if your terminal supports it. The `NO_COLOR` environment variable is respected.

## How it works

The torus is sampled once into points and normals. The sampling density follows the projected size on screen, so there are no holes at any terminal size. Each frame:

1. **Rotate**: a single rotation matrix is built. Instead of rotating every normal, the light and camera vectors are rotated into the donut's local space.
2. **Cull**: back-facing samples are discarded with a three-multiply dot-product test before any projection work, which removes about half the surface.
3. **Project and depth-test**: the remaining points get a perspective projection with a character-aspect correction and are written into a depth buffer.
4. **Shade**: brightness is quantised to 12 levels, each mapped to an ASCII glyph (`.,-~:;=!*#$@`) and a precomputed colour escape code.
5. **Output**: only the rows that changed are sent, wrapped in a synchronized-update block.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Run this in an interactive terminal` | Run it in a real terminal, not through a pipe or the VS Code Output panel. |
| Colours look wrong or garbled | Try `--color 256`, or `--color mono` on very old terminals. |
| Donut looks squashed | Your font's cell ratio differs from 2:1. Adjust `ASPECT` near the top of `donut.py`. |
| Low FPS on a huge window | Run `pip install numpy`, or lower `--density`. |

## License

[MIT](LICENSE) © Seyed Morteza Hashemi (SMH)

---

<div dir="rtl" id="فارسی">

## فارسی

یک دونات سه‌بعدی که داخل ترمینال می‌چرخد. با پایتون خالص نوشته شده و هیچ پیش‌نیاز اجباری ندارد. روی ویندوز، لینوکس، مک و SSH اجرا می‌شود.

### اجرا

</div>

```bash
git clone https://github.com/mementomouri/terminal-donut.git
cd terminal-donut
python donut.py
```

<div dir="rtl">

برای رندر سریع‌تر می‌توانید numpy را نصب کنید (اختیاری): `pip install numpy`

### کلیدها

- `q` یا `Esc`: خروج
- `Space`: توقف و ادامه
- `+` و `-`: تغییر سرعت
- `c`: تغییر تم
- `t`: چرخش کامل
- `[` و `]`: تغییر روشنایی
- `r`: بازنشانی
- `?`: نمایش یا پنهان کردن راهنما

### ویژگی‌ها

- رندر سه‌بعدی واقعی با پرسپکتیو، بافر عمق و نورپردازی
- پنج تم: دونات صورتی با ترافل و چهار تم نئونی متحرک
- فریم‌ریت ثابت و بدون لگ. اگر پردازنده کند باشد، جزئیات به‌طور خودکار کم می‌شود.
- بدون فلیکر، حتی روی SSH کند
- ترمینال در هر نوع خروجی سالم برمی‌گردد.

</div>
