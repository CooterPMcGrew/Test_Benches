#!/usr/bin/env python3
"""Motor-assembly session: re-push config after power cycle, run MPET Ke
measurement (requires free-spinning rotor), then a low-speed closed-loop spin
attempt. All data archived under data/motor_<UTCstamp>/.

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_MotorSession_RevA.py \
           [--skip-mpet] [--spin-duty 150] [--spin-seconds 8]
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
from TOOL_MCF8316_Bringup_RevA import (  # noqa: E402
    Bridge, faults, dump, rmw, CLR_ALL, MOTOR_RES_CODE, MOTOR_IND_CODE)
import numpy as np  # noqa: E402

ALGO_DEBUG2 = 0xEE
MPET_CMD, MPET_KE, MPET_MECH, MPET_WRITE_SHADOW = 1 << 5, 1 << 2, 1 << 1, 1 << 0

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="/dev/ttyACM0")
ap.add_argument("--scope", default="10.42.0.29")
ap.add_argument("--skip-mpet", action="store_true")
ap.add_argument("--spin-duty", type=int, default=150)
ap.add_argument("--spin-seconds", type=float, default=8.0)
args = ap.parse_args()

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"motor_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp}

br = Bridge(args.port)
br.cmd("drvoff 1")
br.cmd("speed 0")
scope = SDS1104XU(args.scope)
for ch in ("C1", "C2", "C3"):
    scope.cmd(f"{ch}:TRA ON")
    scope.cmd(f"{ch}:ATTN 10")
    scope.cmd(f"{ch}:CPL D1M")
    scope.cmd(f"{ch}:VDIV 5V")
    scope.cmd(f"{ch}:OFST -10V")
scope.cmd("C4:TRA OFF")

# ---- re-push configuration (power cycle wiped shadow registers) -----------
dump(br, log, "asfound_after_powercycle")
ok = True
ok &= rmw(br, 0x8A, 0x0000FFFF, (MOTOR_RES_CODE << 8) | MOTOR_IND_CODE,
          log, "CL2_res_ind")
if (br.read(0x8C) >> 23) & 0xFF == 0:
    ok &= rmw(br, 0x8C, 0xFF << 23, 0x01 << 23, log, "CL3_bemf_placeholder")
ok &= rmw(br, 0x8E, 0x7FFFFFFF, (5 << 24) | (5 << 14) | 200,
          log, "CL4_spdloop_maxspeed")
ok &= rmw(br, 0xA4, 0x3, 0x1, log, "PIN_speed_mode_pwm")
log["config_ok"] = ok
if not ok:
    print("CONFIG FAILED - aborting before any energization")
    json.dump(log, open(os.path.join(out, "motor_log.json"), "w"), indent=1)
    sys.exit(1)


def poll(dur, tag, capture_on_state=None, extra=0.2):
    states = []
    t0 = time.time()
    captured = False
    armed = None
    while time.time() - t0 < dur:
        el = time.time() - t0
        try:
            st = int(br.cmd("r 190").split("= ")[1], 16)
            ct = int(br.cmd("r e2").split("= ")[1], 16)
            gd = int(br.cmd("r e0").split("= ")[1], 16)
            states.append({"t": round(el, 2), "state": st,
                           "ctrl": f"0x{ct:08X}", "gd": f"0x{gd:08X}"})
            if capture_on_state is not None and st >= capture_on_state and armed is None:
                armed = el
        except (IndexError, ValueError):
            states.append({"t": round(el, 2), "state": "err"})
        if armed is not None and not captured and el >= armed + extra:
            scope.cmd("STOP")
            captured = True
        time.sleep(0.08)
    log[tag] = states
    return captured


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
        print(f"[{name}] empty capture")
        return
    dec = max(1, n // 100000)
    n = (n // dec) * dec
    d = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in d.items()}
    t = np.arange(n // dec) * dt * dec
    save_csv(os.path.join(out, f"{name}.csv"), t, d)
    print(f"[{name}] saved, pkpk " +
          str({k: round(float(v.max() - v.min()), 1) for k, v in d.items()}))


# ---- MPET Ke + mechanical measurement --------------------------------------
if not args.skip_mpet:
    print("=== MPET Ke/mech measurement (motor will spin!) ===")
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    br.cmd("drvoff 0")
    # wake pulse then idle duty (nonzero target enters MPET with CMD set)
    br.cmd(f"w {ALGO_DEBUG2:x} {MPET_CMD | MPET_KE | MPET_MECH | MPET_WRITE_SHADOW:x}")
    time.sleep(0.2)
    scope.cmd("TDIV 20MS")
    scope.cmd("TRMD AUTO")
    br.cmd("speed 200")
    poll(25.0, "mpet_trace", capture_on_state=8, extra=0.3)
    br.cmd("speed 0")
    grab_save("mpet_capture")
    log["mpet_results"] = {
        "ALGO_STATUS_MPET_0xE8": f"0x{br.read(0xE8):08X}",
        "MTR_PARAMS_0xE6": f"0x{br.read(0xE6):08X}",
        "CL3_after": f"0x{br.read(0x8C):08X}",
        "CL2_after": f"0x{br.read(0x8A):08X}",
    }
    log["mpet_faults"] = faults(br)
    br.cmd("drvoff 1")
    print("MPET results:", log["mpet_results"], log["mpet_faults"])
    time.sleep(1)

# ---- closed-loop spin attempt ----------------------------------------------
print("=== closed-loop spin attempt ===")
br.cmd(f"w ea {CLR_ALL:x}")
time.sleep(0.3)
br.cmd("drvoff 0")
scope.cmd("TDIV 20MS")
scope.cmd("TRMD AUTO")
br.cmd(f"speed {args.spin_duty}")
poll(args.spin_seconds, "spin_trace", capture_on_state=9, extra=0.5)
fg1 = br.read(0x196)
grab_save("spin_capture")
log["spin"] = {"FG_SPEED_FDBK": f"0x{fg1:08X}", "faults": faults(br)}
br.cmd("speed 0")
time.sleep(1.0)
br.cmd("drvoff 1")
print("spin:", log["spin"])

dump(br, log, "final_registers")
json.dump(log, open(os.path.join(out, "motor_log.json"), "w"), indent=1)
print(f"\nall data -> {out}/")
