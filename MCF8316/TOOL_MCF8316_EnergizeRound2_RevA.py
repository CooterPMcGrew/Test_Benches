#!/usr/bin/env python3
"""Round-2 rotor-less energization: edge-triggered scope captures inside the
active window, fast state polling. Assumes config from the bring-up run is
still in shadow registers (do not power-cycle in between).

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_EnergizeRound2_RevA.py
"""
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

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"energize2_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp}

br = Bridge("/dev/ttyACM0")
scope = SDS1104XU("10.42.0.29")
print("scope:", scope.query("*IDN?"))
for ch in ("C1", "C2", "C3"):
    scope.cmd(f"{ch}:TRA ON")
    scope.cmd(f"{ch}:ATTN 10")
    scope.cmd(f"{ch}:CPL D1M")
    scope.cmd(f"{ch}:VDIV 5V")
    scope.cmd(f"{ch}:OFST -10V")   # phases swing 0..18 V: center the window
scope.cmd("C4:TRA OFF")


def grab(chans=("C1", "C2", "C3")):
    d = {}
    dt = None
    for ch in chans:
        dt, v = scope.wave(ch)
        if len(v) == 0:            # roll-mode / incomplete sweep: settle, retry
            time.sleep(1.0)
            dt, v = scope.wave(ch)
        d[f"CH{ch[1]}"] = v
    n = min(len(v) for v in d.values())
    if n == 0:                     # untriggered NORM sweep: return empty marker
        return np.array([0.0]), {k: np.array([0.0]) for k in d}
    dec = max(1, n // 100000)
    n = (n // dec) * dec
    return (np.arange(n // dec) * dt * dec,
            {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in d.items()})


def run_test(name, duty, tdiv, trigger=False, stop_at_state=6, dwell=15.0,
             extra_delay=0.15):
    """Command speed, poll the algorithm state machine, STOP the scope
    extra_delay after the state reaches stop_at_state (or on first fault)."""
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    scope.cmd(f"TDIV {tdiv}")
    if trigger:
        scope.cmd("TRSE EDGE,SR,C1,HT,OFF")
        scope.cmd("C1:TRLV 5V")
        scope.cmd("TRMD NORM")
    else:
        scope.cmd("TRMD AUTO")
    time.sleep(0.4)
    states = []
    t0 = time.time()
    br.cmd(f"speed {duty}")
    captured = False
    t_cap = None
    armed_at = None
    while time.time() - t0 < dwell:
        el = time.time() - t0
        try:
            st = int(br.cmd("r 190").split("= ")[1], 16)
            ct = int(br.cmd("r e2").split("= ")[1], 16)
            states.append({"t": round(el, 2), "state": st,
                           "ctrl": f"0x{ct:08X}"})
            hit = (st >= stop_at_state) or (ct != 0)
        except (IndexError, ValueError):
            states.append({"t": round(el, 2), "state": "err"})
            hit = False
        if hit and armed_at is None:
            armed_at = el
        if armed_at is not None and not captured and el >= armed_at + extra_delay:
            scope.cmd("STOP")
            captured = True
            t_cap = el
        if captured and states and states[-1].get("ctrl", "0x0") not in ("0x00000000",):
            break
        time.sleep(0.08)
    if not captured:
        scope.cmd("STOP")
        t_cap = time.time() - t0
    br.cmd("speed 0")
    t, chans = grab()
    scope.cmd("TRMD AUTO")
    res = {"duty": duty, "tdiv": tdiv, "capture_at_s": round(t_cap, 2),
           "state_trace": states, "faults_after": faults(br),
           "pkpk": {k: round(float(v.max() - v.min()), 2) for k, v in chans.items()}}
    log.setdefault("tests", {})[name] = res
    save_csv(os.path.join(out, f"{name}.csv"), t, chans)
    print(f"[{name}] cap@{t_cap:.2f}s pkpk={res['pkpk']} faults={res['faults_after']}")
    print("   states:", [(s["t"], s["state"]) for s in states[:12]])
    return res


br.cmd("drvoff 0")
run_test("r2t1_align_overview", duty=150, tdiv="20MS", stop_at_state=6,
         extra_delay=0.12)
time.sleep(1)
run_test("r2t2_pwm_trig", duty=150, tdiv="10US", trigger=True,
         stop_at_state=6, extra_delay=0.25)
time.sleep(1)
run_test("r2t3_openloop", duty=150, tdiv="20MS", stop_at_state=7,
         extra_delay=0.12)
time.sleep(1)
run_test("r2t4_higher_duty", duty=400, tdiv="20MS", stop_at_state=7,
         extra_delay=0.12)
br.cmd("drvoff 1")
br.cmd("speed 0")

with open(os.path.join(out, "energize2_log.json"), "w") as fh:
    json.dump(log, fh, indent=1)
print(f"\nall data -> {out}/")
