#!/usr/bin/env python3
"""Long-run comprehensive characterization loop for the PCB stator bench.

Repeatedly invokes TOOL_STATOR_SweepOrchestrator_RevA.py on a dense log grid
(200 Hz - 5 MHz, ~10 pts/decade), cycling the drive amplitude between
iterations to test linearity. Each iteration dumps to its own UTC-stamped
subfolder under --base. Runs until --hours wall-clock elapses.

Usage:
    uv run --with numpy,pyserial python TOOL_STATOR_SweepLoop_RevA.py \
        --rsense 42.68 --hours 3 [--base data/loop1_pairAB]
"""
import argparse
import datetime
import os
import subprocess
import sys
import time

import numpy as np

AMPL_CYCLE_VPP = [5.0, 2.0, 10.0, 20.0]   # linearity check across iterations
MAX_ITER = 200                             # hard cap regardless of --hours
MAX_CONSEC_FAIL = 3

ap = argparse.ArgumentParser()
ap.add_argument("--rsense", type=float, required=True)
ap.add_argument("--hours", type=float, default=3.0)
ap.add_argument("--base", default=None)
ap.add_argument("--scope", default="10.42.0.29")
ap.add_argument("--gen", default="/dev/ttyUSB0")
args = ap.parse_args()

here = os.path.dirname(os.path.abspath(__file__))
stamp0 = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
base = args.base or os.path.join(here, "data", f"loop_{stamp0}")
os.makedirs(base, exist_ok=True)

# ~10 pts/decade, 200 Hz to 5 MHz, snapped to 3 significant digits
grid = np.unique(np.round(np.logspace(np.log10(200), np.log10(5e6), 45), -0))
freqs = ",".join(f"{f:.3g}" for f in grid)

log_path = os.path.join(base, "loop_log.txt")


def log(msg):
    line = f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    with open(log_path, "a") as fh:
        fh.write(line + "\n")


with open(os.path.join(base, "manifest.txt"), "w") as fh:
    fh.write(f"started: {stamp0}\nrsense_ohm: {args.rsense}\n"
             f"terminal pair: A-B (fixed for whole loop)\n"
             f"amplitude cycle Vpp: {AMPL_CYCLE_VPP}\nfreq grid Hz: {freqs}\n")

t0 = time.monotonic()
fails = 0
for it in range(1, MAX_ITER + 1):
    elapsed_h = (time.monotonic() - t0) / 3600
    if elapsed_h >= args.hours:
        log(f"target {args.hours} h reached after {it - 1} iterations, stopping")
        break
    vpp = AMPL_CYCLE_VPP[(it - 1) % len(AMPL_CYCLE_VPP)]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(base, f"iter{it:03d}_{vpp:g}Vpp_{stamp}")
    log(f"iter {it}: {vpp} Vpp -> {out} ({elapsed_h:.2f} h elapsed)")
    r = subprocess.run(
        [sys.executable, os.path.join(here, "TOOL_STATOR_SweepOrchestrator_RevA.py"),
         "--rsense", str(args.rsense), "--vpp", str(vpp), "--freqs", freqs,
         "--out", out, "--scope", args.scope, "--gen", args.gen],
        capture_output=True, text=True, timeout=3600)
    with open(os.path.join(out, "orchestrator_stdout.txt")
              if os.path.isdir(out) else os.path.join(base, f"iter{it:03d}_fail.txt"),
              "w") as fh:
        fh.write(r.stdout + "\n--- stderr ---\n" + r.stderr)
    if r.returncode != 0:
        fails += 1
        log(f"iter {it} FAILED (rc={r.returncode}); consecutive fails {fails}")
        if fails >= MAX_CONSEC_FAIL:
            log("too many consecutive failures, aborting loop")
            sys.exit(1)
        time.sleep(30)
    else:
        fails = 0
        log(f"iter {it} ok")
    time.sleep(10)
log("loop complete")
