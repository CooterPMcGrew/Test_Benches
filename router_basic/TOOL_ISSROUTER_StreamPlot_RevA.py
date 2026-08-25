"""Live terminal display of the ISS Router 16-channel stream.

Usage: uv run --with pyserial TOOL_ISSROUTER_StreamPlot_RevA.py [port] [--log [file]]

--log tees every frame to a CSV (default data/STREAM_<UTC>.csv) while the
live display runs: t_ms + all 16 channels + FG; coils are the first 6 cols.

The display repaints in place (no scrolling). Keystrokes pass straight
through to the firmware (R m + - 0 p n a t x d ...) and the last few
firmware replies show in the footer. Ctrl-C exits, restores human-readable
mode, and closes the log.
"""
import glob
import os
import select
import sys
import termios
import time
import tty

import serial

# Net-true channel names -- AIN order, both schematic swaps already folded in
NAMES = ["ISNS_0/c1", "ISNS_1/c2", "ISNS_2/c3", "ISNS_3/c4",
         "ISNS_5/c6", "ISNS_4/c5",
         "ANALOG_0", "ANALOG_1", "ANALOG_2", "ANALOG_3",
         "ANALOG_5", "ANALOG_4", "ANALOG_6", "ANALOG_7",
         "ANALOG_8", "ANALOG_9"]
VRANGE = 2.5
BARW = 41  # odd -> exact center cell for 0 V


def find_port():
    if len(sys.argv) > 1:
        return sys.argv[1]
    for pat in ("/dev/serial/by-id/*Espressif*", "/dev/serial/by-id/*CP21*",
                "/dev/ttyACM*", "/dev/ttyUSB*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    sys.exit("no serial device found")


def bar(v):
    cells = [" "] * BARW
    mid = BARW // 2
    cells[mid] = "|"
    pos = mid + max(-mid, min(mid, round(v / VRANGE * mid)))
    lo, hi = sorted((mid, pos))
    for i in range(lo, hi + 1):
        cells[i] = "#"
    cells[mid] = "|" if pos == mid else cells[mid]
    return "".join(cells)


logf = None
if "--log" in sys.argv:
    i = sys.argv.index("--log")
    path = (sys.argv[i + 1] if len(sys.argv) > i + 1 else
            time.strftime("data/STREAM_%Y%m%dT%H%M%SZ.csv", time.gmtime()))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    logf = open(path, "w")
    logf.write("t_ms," + ",".join(NAMES) + ",FG_Hz\n")
    print(f"logging to {path}")
    sys.argv = sys.argv[:i]  # keep find_port() arg handling intact

def open_stream():
    while True:
        try:
            sp = serial.Serial(find_port(), 115200, timeout=0.05)
            time.sleep(0.3)
            sp.reset_input_buffer()
            sp.write(b"c")  # enter CSV mode
            return sp
        except (serial.SerialException, OSError):
            print("\x1b[Hreconnecting to S3...\x1b[J", end="", flush=True)
            time.sleep(1.0)


s = open_stream()

vmin = [float("inf")] * 16
vmax = [float("-inf")] * 16
frames = 0
fg = None
last = None
footer = []          # last firmware text replies (R/m/+ output, faults)
buf = b""

stdin_fd = sys.stdin.fileno()
saved_tty = termios.tcgetattr(stdin_fd)
tty.setcbreak(stdin_fd)
print("\x1b[2J", end="")  # clear once; then repaint in place

last_frame_t = time.monotonic()
last_nudge_t = 0.0
try:
    while True:
        try:
            r, _, _ = select.select([s.fileno(), stdin_fd], [], [], 0.2)
            if stdin_fd in r:
                key = os.read(stdin_fd, 8)
                if key:
                    s.write(key)       # pass keystrokes to the firmware
            if s.fileno() in r:
                chunk = s.read(65536)
                if chunk:
                    buf += chunk
            # stream watchdog: reboot/flash under us, or CSV toggled off
            now = time.monotonic()
            if now - last_frame_t > 4.0 and now - last_nudge_t > 4.0:
                s.write(b"c")
                last_nudge_t = now
                footer = (footer + ["(no frames -- nudged CSV back on)"])[-4:]
        except (serial.SerialException, OSError):
            try:
                s.close()
            except OSError:
                pass
            print("\x1b[Hserial dropped (reflash/replug?) -- reconnecting...\x1b[J",
                  end="", flush=True)
            s = open_stream()
            buf = b""
            last_frame_t = time.monotonic()
            continue
        redraw = False
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            parts = line.split(",")
            volts = None
            if len(parts) in (17, 18):
                try:
                    volts = [float(p) for p in parts[1:17]]
                    fg = float(parts[17]) if len(parts) == 18 else None
                except ValueError:
                    volts = None
            if volts is None:
                footer = (footer + [line])[-4:]   # firmware reply / fault
                redraw = True
                continue
            frames += 1
            redraw = True
            last_frame_t = time.monotonic()
            if logf:
                logf.write(line + "\n")
                if frames % 50 == 0:
                    logf.flush()
            for i, v in enumerate(volts):
                vmin[i] = min(vmin[i], v)
                vmax[i] = max(vmax[i], v)
            last = volts
        if not redraw or last is None:
            continue
        out = ["\x1b[H",
               f"ISS Router stream  frame {frames}"
               + (f"  FG {fg:0.1f} Hz" if fg is not None else ""),
               f"{'':10s} {-VRANGE:+.1f}{'':>{BARW - 8}}{VRANGE:+.1f}"]
        for i, v in enumerate(last):
            out.append(f"{NAMES[i]:10s} [{bar(v)}] {v:+7.3f} V  "
                       f"min {vmin[i]:+6.3f} max {vmax[i]:+6.3f}")
        out.append("-" * 78)
        out += [f"fw| {l[:74]}" for l in footer]
        out += [""] * (4 - len(footer))
        out.append("keys -> firmware: R clr-fault  m probe  +/-/0 speed  "
                   "t strobe  p pots | Ctrl-C quits")
        print("\n".join(out) + "\x1b[J", end="", flush=True)
except KeyboardInterrupt:
    pass
finally:
    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_tty)
    s.write(b"c")  # back to human-readable
    time.sleep(0.2)
    s.close()
    if logf:
        logf.close()
    print("\nrestored human mode" + (", log closed" if logf else ""))
