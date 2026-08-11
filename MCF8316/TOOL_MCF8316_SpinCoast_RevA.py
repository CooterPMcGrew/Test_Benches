#!/usr/bin/env python3
"""Sustained closed-loop spin, then drive cut-off with scope capture of the
coast-down back-EMF -> Ke measurement. Archives to data/spincoast_<stamp>/.

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_SpinCoast_RevA.py \
           [--duty 500] [--sustain 10]
"""
import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Stator"))
from TOOL_STATOR_SweepOrchestrator_RevA import SDS1104XU, save_csv  # noqa: E402
from TOOL_MCF8316_Bringup_RevA import Bridge, faults, CLR_ALL  # noqa: E402
import numpy as np  # noqa: E402

MAX_SPEED_HZ = 200.0  # as configured in CLOSED_LOOP4

ap = argparse.ArgumentParser()
ap.add_argument("--duty", type=int, default=500)
ap.add_argument("--sustain", type=float, default=10.0)
args = ap.parse_args()

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"spincoast_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp, "duty": args.duty}

br = Bridge("/dev/ttyACM0")
scope = SDS1104XU("10.42.0.29")
for ch in ("C1", "C2", "C3"):
    scope.cmd(f"{ch}:TRA ON")
    scope.cmd(f"{ch}:ATTN 10")
    scope.cmd(f"{ch}:CPL D1M")
    scope.cmd(f"{ch}:VDIV 5V")
    scope.cmd(f"{ch}:OFST -10V")
scope.cmd("C4:TRA OFF")
scope.cmd("TRMD AUTO")

br.cmd(f"w ea {CLR_ALL:x}")
time.sleep(0.3)
br.cmd("drvoff 0")
br.cmd(f"speed {args.duty}")

trace = []
t0 = time.time()
while time.time() - t0 < args.sustain:
    el = time.time() - t0
    try:
        st = int(br.cmd("r 190").split("= ")[1], 16)
        fg = int(br.cmd("r 196").split("= ")[1], 16)
        ct = int(br.cmd("r e2").split("= ")[1], 16)
        hz = fg / (1 << 27) * MAX_SPEED_HZ
        trace.append({"t": round(el, 2), "state": f"0x{st:X}",
                      "fg_elhz": round(hz, 2), "ctrl": f"0x{ct:08X}"})
    except (IndexError, ValueError):
        trace.append({"t": round(el, 2), "state": "err"})
    time.sleep(0.15)
log["run_trace"] = trace
pre = [x for x in trace if isinstance(x.get("fg_elhz"), float)]
f_pre = pre[-1]["fg_elhz"] if pre else None
print(f"pre-cut estimated speed: {f_pre} elec Hz")

# steady-state waveform while still spinning
scope.cmd("TDIV 20MS")
time.sleep(0.6)
scope.cmd("STOP")


def grab_save(name):
    d = {}
    dt = None
    for ch in ("C1", "C2", "C3"):
        dt, v = scope.wave(ch)
        if len(v) == 0:
            time.sleep(1.0)
            dt, v = scope.wave(ch)
        d[f"CH{ch[1]}"] = v
    n = min(len(v) for v in d.values())
    if n == 0:
        print(f"[{name}] EMPTY")
        return None, None
    dec = max(1, n // 100000)
    n = (n // dec) * dec
    d = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in d.items()}
    t = np.arange(n // dec) * dt * dec
    save_csv(os.path.join(out, f"{name}.csv"), t, d)
    print(f"[{name}] pkpk " +
          str({k: round(float(v.max() - v.min()), 2) for k, v in d.items()}))
    return t, d


grab_save("steady_state")
scope.cmd("TRMD AUTO")
time.sleep(0.4)

# coast: cut drive mid-window, capture BEMF decay
scope.cmd("TDIV 20MS")
time.sleep(0.5)
br.cmd("drvoff 1")          # outputs Hi-Z: motor coasts, phases show BEMF
time.sleep(0.10)
scope.cmd("STOP")
t, d = grab_save("coast_bemf")
br.cmd("speed 0")
log["faults_after"] = faults(br)

if t is not None:
    # Ke from the tail of the window (pure BEMF, drive gone)
    tail = t > t[-1] - 0.15
    y = d["CH1"][tail] - d["CH1"][tail].mean()
    if y.max() - y.min() > 0.05:
        sp = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        fr = np.fft.rfftfreq(len(y), t[1] - t[0])
        f_e = fr[np.argmax(sp[1:]) + 1]
        vpk = (y.max() - y.min()) / 2
        log["ke"] = {"coast_f_elhz": round(float(f_e), 2),
                     "coast_Vpk_LL": round(float(vpk), 3),
                     "Ke_V_per_elHz": round(float(vpk / f_e), 5),
                     "note": "line-to-line peak per electrical Hz, from coast"}
        print("Ke estimate:", log["ke"])

json.dump(log, open(os.path.join(out, "spincoast_log.json"), "w"), indent=1)
print(f"\nall data -> {out}/")
