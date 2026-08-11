#!/usr/bin/env python3
"""Comprehensive tuning/characterization campaign 2.

Motor: 20-pole (10 pole-pair), rotor mass ~1.3 kg. All speeds reported in
electrical Hz AND mechanical RPM (f_mech = f_e / 10).

Sequence: full config push (incl. operator-approved fault-detector state) ->
pre-align -> speed ladder 150/300/500 duty with per-point telemetry + scope
captures -> low-side braking decel profile -> Hi-Z coast decel profile.
Telemetry at ~4 Hz: state, faults, FG register, ALGO_STATUS, MTR_PARAMS,
FG hardware counter + pin level.

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_Campaign2_RevA.py
"""
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Stator"))
from TOOL_STATOR_SweepOrchestrator_RevA import SDS1104XU, save_csv  # noqa: E402
from TOOL_MCF8316_Bringup_RevA import (  # noqa: E402
    Bridge, faults, rmw, CLR_ALL, MOTOR_RES_CODE, MOTOR_IND_CODE)
import numpy as np  # noqa: E402

PP = 10                     # pole pairs (user-provided: 20-pole rotor)
MAX_SPEED_HZ = 200.0

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"campaign2_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp, "pole_pairs": PP, "rotor_mass_kg": 1.3}

br = Bridge("/dev/ttyACM0")
scope = SDS1104XU("10.42.0.29")
def reg(a): return int(br.cmd(f"r {a:x}").split("= ")[1], 16)


def fg_hw():
    m = re.search(r"FG ([0-9.]+) Hz \((\d+) edges", br.cmd("fg"))
    return (float(m.group(1)), int(m.group(2))) if m else (None, 0)


def pins():
    m = re.search(r"nFAULT=(\d) FG=(\d)", br.cmd("pins"))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def scope_setup(tdiv="20MS", vdiv="5V"):
    for ch in ("C1", "C2", "C3"):
        scope.cmd(f"{ch}:TRA ON")
        scope.cmd(f"{ch}:ATTN 10")
        scope.cmd(f"{ch}:CPL D1M")
        scope.cmd(f"{ch}:VDIV {vdiv}")
        scope.cmd(f"{ch}:OFST -10V")
    scope.cmd(f"TDIV {tdiv}")
    scope.cmd("TRMD AUTO")


def capture(name):
    time.sleep(0.6)
    scope.cmd("STOP")
    d = {}
    for ch in ("C1", "C2", "C3"):
        br.cmd("pins")
        dt, v = scope.wave(ch)
        if len(v) == 0:
            time.sleep(0.8)
            dt, v = scope.wave(ch)
        d[f"CH{ch[1]}"] = v
    scope.cmd("TRMD AUTO")
    n = min(len(v) for v in d.values())
    if n == 0:
        print(f"  [{name}] EMPTY")
        return None
    dec = max(1, n // 100000)
    n = (n // dec) * dec
    d = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in d.items()}
    t = np.arange(n // dec) * dt * dec
    save_csv(os.path.join(out, f"{name}.csv"), t, d)
    # commutation frequency from CH1 = electrical speed ground truth
    y = d["CH1"] - d["CH1"].mean()
    sp = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    fr = np.fft.rfftfreq(len(y), t[1] - t[0])
    lo = fr > 2
    f_com = float(fr[lo][np.argmax(sp[lo])]) if lo.any() else 0.0
    print(f"  [{name}] captured, commutation ~{f_com:.1f} Hz elec "
          f"(~{f_com / PP * 60:.0f} RPM)")
    return f_com


def sample(t0):
    st = reg(0x190) & 0xFF
    ct = reg(0xE2)
    fgr = reg(0x196)
    e4 = reg(0xE4)
    e6 = reg(0xE6)
    fh, edges = fg_hw()
    _, fglvl = pins()
    return {"t": round(time.time() - t0, 2), "state": hex(st), "ctrl": hex(ct),
            "fg_reg": fgr, "algo_status": hex(e4), "mtr_params": hex(e6),
            "fg_hw_hz": fh, "fg_pin": fglvl}


# ---- full configuration -----------------------------------------------------
ok = True
ok &= rmw(br, 0x8A, (0x7 << 28) | 0xFFFF,
          (0x2 << 28) | (MOTOR_RES_CODE << 8) | MOTOR_IND_CODE, log, "CL2+brake")
ok &= rmw(br, 0x8C, 0xFF << 23, 0x3C << 23, log, "CL3_ke")
ok &= rmw(br, 0x8E, 0x7FFFFFFF, (5 << 24) | (5 << 14) | 200, log, "CL4")
ok &= rmw(br, 0x88, (0x1F << 25) | (0xF << 15), (0x2 << 25) | (0xA << 15),
          log, "CL1_acc_pwm")
ok &= rmw(br, 0x86, 0x7FF80000, (0x5 << 27) | (0x3 << 23), log, "MS2")
ok &= rmw(br, 0x84, 0xF << 25, 0x1 << 25, log, "MS1_ramp")
ok &= rmw(br, 0xA4, 0x3, 0x1, log, "PIN_pwm")
# operator-approved detector state (see TR RevA F-5/F-8): lock thresholds
# above the 2.65 A physical ceiling, 2.5 ms deglitch, ABN_BEMF lock off
ok &= rmw(br, 0x90, (0xF << 23) | (0xF << 19) | (0xF << 11),
          (0xF << 23) | (0xF << 19) | (0x5 << 11), log, "FC1")
ok &= rmw(br, 0x92, 1 << 29, 0, log, "FC2")
log["config_ok"] = ok
if not ok:
    sys.exit("config failed")

scope_setup()
try:
    # pre-align to remove starting snap
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    br.cmd(f"w ec {((1 << 14) | (1 << 10)):x}")
    br.cmd("w ea 0")
    br.cmd("drvoff 0")
    br.cmd("speed 100")
    time.sleep(8.0)
    br.cmd("speed 0")
    br.cmd("w ec 0")
    time.sleep(2.0)
    print("pre-aligned at 0 deg")

    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    ladder = [(150, "duty150"), (300, "duty300"), (500, "duty500")]
    telemetry = []
    t0 = time.time()
    aborted = False
    for duty, name in ladder:
        print(f"=== {name} (target {duty/1023*MAX_SPEED_HZ:.0f} elHz = "
              f"{duty/1023*MAX_SPEED_HZ/PP*60:.0f} RPM) ===")
        br.cmd(f"speed {duty}")
        settle_until = time.time() + 20
        fault = 0
        while time.time() < settle_until:
            s = sample(t0)
            s["phase"] = name
            telemetry.append(s)
            if s["ctrl"] != "0x0":
                fault = int(s["ctrl"], 16)
                break
            time.sleep(0.25)
        if fault:
            print(f"  FAULT 0x{fault:08X} — stopping ladder")
            log[f"{name}_fault"] = hex(fault)
            aborted = True
            break
        f_com = capture(f"{name}_steady")
        log[f"{name}_commutation_elhz"] = f_com
        scope_setup(tdiv="2MS")
        capture(f"{name}_fast")
        scope_setup(tdiv="20MS")

    # braking decel profile (low-side brake stop mode)
    if not aborted:
        print("=== braking decel (low-side brake) ===")
        br.cmd("speed 0")
        tb = time.time()
        while time.time() - tb < 10:
            s = sample(t0)
            s["phase"] = "brake_decel"
            telemetry.append(s)
            time.sleep(0.2)

        # coast decel profile for comparison (stop mode -> Hi-Z)
        print("=== coast decel (Hi-Z) — respin then release ===")
        rmw(br, 0x8A, 0x7 << 28, 0x0 << 28, log, "CL2_stop_hiz")
        br.cmd(f"w ea {CLR_ALL:x}")
        time.sleep(0.3)
        br.cmd("speed 300")
        tw = time.time()
        while time.time() - tw < 25:
            s = sample(t0)
            s["phase"] = "respin"
            telemetry.append(s)
            if s["ctrl"] != "0x0":
                break
            st = int(s["state"], 16)
            if st in (8, 9) and time.time() - tw > 8:
                break
            time.sleep(0.25)
        br.cmd("speed 0")
        tc = time.time()
        while time.time() - tc < 20:
            s = sample(t0)
            s["phase"] = "coast_decel"
            telemetry.append(s)
            time.sleep(0.25)
        rmw(br, 0x8A, 0x7 << 28, 0x2 << 28, log, "CL2_stop_brake_restore")
finally:
    br.cmd("speed 0")
    br.cmd("w ec 0")
    br.cmd("drvoff 1")
    log["faults_final"] = faults(br)
    log["n_telemetry"] = len(telemetry)
    json.dump(telemetry, open(os.path.join(out, "telemetry.json"), "w"), indent=1)
    json.dump(log, open(os.path.join(out, "campaign2_log.json"), "w"), indent=1)
    print(f"safe; {len(telemetry)} telemetry samples; data -> {out}/")
