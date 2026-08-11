#!/usr/bin/env python3
"""Lock-detector tuning test — run BY THE OPERATOR (modifies fault config).

Default action: add 2.5 ms debounce to the ADC lock detector (threshold stays
at stock 4 A) and attempt a start. With --thresholds-8a it instead raises both
lock-current thresholds to 8 A — above the 2.65 A physical ceiling of this
6.8 ohm winding at 18 V — leaving OCP and the speed/BEMF lock detectors fully
active. All changes are shadow-register only (power cycle reverts everything).

Usage:
  uv run --with pyserial,numpy python TOOL_MCF8316_LockTune_RevA.py
  uv run --with pyserial,numpy python TOOL_MCF8316_LockTune_RevA.py --thresholds-8a
"""
import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Stator"))
from TOOL_MCF8316_Bringup_RevA import Bridge, faults, rmw, CLR_ALL  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--thresholds-8a", action="store_true",
                help="raise ADC+comparator lock thresholds to 8 A instead of debounce")
ap.add_argument("--duty", type=int, default=150)
args = ap.parse_args()

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"locktune_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp, "mode": "8A" if args.thresholds_8a else "deglitch"}

br = Bridge("/dev/ttyACM0")
def reg(a): return int(br.cmd(f"r {a:x}").split("= ")[1], 16)

if args.thresholds_8a:
    rmw(br, 0x90, (0xF << 23) | (0xF << 19), (0xF << 23) | (0xF << 19),
        log, "locks_to_8A")
else:
    rmw(br, 0x90, 0xF << 11, 0x5 << 11, log, "lock_deglitch_2.5ms")

br.cmd(f"w ea {CLR_ALL:x}")
time.sleep(0.3)
br.cmd("drvoff 0")
br.cmd(f"speed {args.duty}")
t0 = time.time()
rows = []
outcome = "timeout"
try:
    while time.time() - t0 < 45:
        el = time.time() - t0
        try:
            st, ct = reg(0x190) & 0xFF, reg(0xE2)
            fg = reg(0x196) / (1 << 27) * 200
            rows.append({"t": round(el, 1), "state": hex(st),
                         "fg": round(fg, 1) if fg <= 300 else "inv",
                         "ctrl": hex(ct)})
            if ct != 0:
                outcome = f"FAULT 0x{ct:08X}"
                break
            if fg <= 300 and fg > 20 and el > 3:
                outcome = "SPINNING"
                break
        except (IndexError, ValueError):
            pass
        time.sleep(0.12)
    if outcome == "SPINNING":
        for _ in range(40):
            br.cmd("pins")
            time.sleep(0.15)
        fg2 = reg(0x196) / (1 << 27) * 200
        log["held_fg_elhz"] = round(fg2, 1)
        log["held_faults"] = faults(br)
        print(f"HELD 6s: fg={fg2:.1f} elHz, faults:", log["held_faults"])
finally:
    br.cmd("speed 0")
    br.cmd("drvoff 1")
log["outcome"] = outcome
log["trace"] = rows
json.dump(log, open(os.path.join(out, "locktune_log.json"), "w"), indent=1)
print("outcome:", outcome)
step = max(1, len(rows) // 14)
print("trace:", [(r["t"], r["state"], r["fg"], r["ctrl"]) for r in rows[::step]])
print("data ->", out)
