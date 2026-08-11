#!/usr/bin/env python3
"""Final validation: tuned config -> closed-loop spin at two speeds ->
position-hold demo via force-align servo mode -> EEPROM burn -> full dump.

Tuned configuration (every value measured or reasoned, see TR):
  CL2: MOTOR_RES 0xCB (3.4R), MOTOR_IND 0x0D (18uH), stop mode Hi-Z
  CL3: MOTOR_BEMF_CONST 0x3C (12.5 mV/Hz, measured by coast test)
  CL4: SPD_LOOP_KP 0.05, KI 0.5, MAX_SPEED 200 elHz
  CL1: PWM_FREQ_OUT 60 kHz (max), CL_ACC 5 Hz/s (heavy wheel)
  MS2: OL_ILIMIT 2.0 A, OL_ACC_A1 2.5 Hz/s (validated open-loop recipe)
  PIN: SPEED_MODE PWM
  FAULT_CONFIG1: lock thresholds 8 A (above the 2.65 A physical ceiling of
    this 6.8R winding at 18 V), 2.5 ms deglitch  [operator-approved]
  FAULT_CONFIG2: LOCK2 (abnormal BEMF) disabled  [operator-approved]

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_FinalValidation_RevA.py \
          [--no-eeprom]
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

MAX_SPEED_HZ = 200.0
ap = argparse.ArgumentParser()
ap.add_argument("--no-eeprom", action="store_true")
args = ap.parse_args()

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"final_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp}

br = Bridge("/dev/ttyACM0")
scope = SDS1104XU("10.42.0.29")
def reg(a): return int(br.cmd(f"r {a:x}").split("= ")[1], 16)


def scope_setup(vdiv="5V", ofst="-10V", tdiv="20MS"):
    for ch in ("C1", "C2", "C3"):
        scope.cmd(f"{ch}:TRA ON")
        scope.cmd(f"{ch}:ATTN 10")
        scope.cmd(f"{ch}:CPL D1M")
        scope.cmd(f"{ch}:VDIV {vdiv}")
        scope.cmd(f"{ch}:OFST {ofst}")
    scope.cmd(f"TDIV {tdiv}")
    scope.cmd("TRMD AUTO")


def capture(name):
    time.sleep(0.7)
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
        print(f"[{name}] EMPTY")
        return
    dec = max(1, n // 100000)
    n = (n // dec) * dec
    d = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in d.items()}
    t = np.arange(n // dec) * dt * dec
    save_csv(os.path.join(out, f"{name}.csv"), t, d)
    pk = {k: round(float(v.max() - v.min()), 2) for k, v in d.items()}
    log.setdefault("captures", {})[name] = pk
    print(f"[{name}] pkpk {pk}")


def fg_valid():
    v = reg(0x196) / (1 << 27) * MAX_SPEED_HZ
    return v if 0 <= v <= 1.5 * MAX_SPEED_HZ else None


# ---- full tuned configuration ----------------------------------------------
scope_setup()
ok = True
ok &= rmw(br, 0x8A, (0x7 << 28) | 0xFFFF,
          (0x0 << 28) | (MOTOR_RES_CODE << 8) | MOTOR_IND_CODE, log, "CL2")
ok &= rmw(br, 0x8C, 0xFF << 23, 0x3C << 23, log, "CL3_bemf")
ok &= rmw(br, 0x8E, 0x7FFFFFFF, (5 << 24) | (5 << 14) | 200, log, "CL4")
ok &= rmw(br, 0x88, (0x1F << 25) | (0xF << 15),
          (0x3 << 25) | (0xA << 15), log, "CL1_acc5_pwm60k")
ok &= rmw(br, 0x86, 0x7FF80000, (0x5 << 27) | (0x3 << 23), log, "MS2")
ok &= rmw(br, 0xA4, 0x3, 0x1, log, "PIN_pwm_mode")
ok &= rmw(br, 0x90, (0xF << 23) | (0xF << 19) | (0xF << 11),
          (0xF << 23) | (0xF << 19) | (0x5 << 11), log, "FC1_locks")
ok &= rmw(br, 0x92, 1 << 29, 0, log, "FC2_lock2_off")
log["config_ok"] = bool(ok)
if not ok:
    sys.exit("config failed")

try:
    # ---- spin validation ----------------------------------------------------
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    br.cmd("drvoff 0")
    br.cmd("speed 150")
    t0 = time.time()
    synced = False
    trace = []
    while time.time() - t0 < 45:
        try:
            st, ct, fg = reg(0x190) & 0xFF, reg(0xE2), fg_valid()
            trace.append({"t": round(time.time() - t0, 2), "state": hex(st),
                          "fg": fg, "ctrl": hex(ct)})
            if ct != 0:
                print(f"FAULT 0x{ct:08X}")
                break
            if st in (8, 9) and fg and fg > 15:
                synced = True
                print(f"closed loop at {time.time()-t0:.1f}s fg={fg:.1f}")
                break
        except (IndexError, ValueError):
            pass
        time.sleep(0.12)
    log["startup_trace_tail"] = trace[-10:]

    if synced:
        hold = [f for _ in range(30) if (f := fg_valid()) is not None
                and not time.sleep(0.15)]
        log["hold_fg_duty150"] = hold
        print(f"duty150 hold: fg {np.mean(hold):.1f} +/- {np.std(hold):.1f} elHz")
        capture("final_duty150_slow")
        scope_setup(tdiv="2MS")
        capture("final_duty150_fast")
        scope_setup(tdiv="20MS")

        br.cmd("speed 300")
        t1 = time.time()
        fault2 = 0
        while time.time() - t1 < 20:
            try:
                ct = reg(0xE2)
                if ct != 0:
                    fault2 = ct
                    break
            except (IndexError, ValueError):
                pass
            time.sleep(0.2)
        log["stepup_fault"] = hex(fault2)
        if fault2 == 0:
            hold2 = [f for _ in range(25) if (f := fg_valid()) is not None
                     and not time.sleep(0.15)]
            log["hold_fg_duty300"] = hold2
            print(f"duty300 hold: fg {np.mean(hold2):.1f} +/- {np.std(hold2):.1f}")
            capture("final_duty300_slow")
        else:
            print(f"step-up fault 0x{fault2:08X} (speed-loop tuning continues)")
        br.cmd("speed 0")
        time.sleep(3.0)

    # ---- position-hold demo -------------------------------------------------
    print("=== position demo: force-align servo ===")
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    # ALGO_DEBUG1: FORCE_ALIGN_EN (bit14) + angle source = register (bit10)
    br.cmd(f"w ec {((1 << 14) | (1 << 10)):x}")
    br.cmd("drvoff 0")
    br.cmd("speed 100")            # enter startup; force-align holds at align
    time.sleep(7.0)                # brake (5s) + settle into align
    scope_setup(tdiv="50MS" if False else "20MS")
    for ang in (0, 90, 180, 270, 0):
        br.cmd(f"w ea {(ang << 11):x}")
        time.sleep(1.2)
        try:
            st = reg(0x190) & 0xFF
        except (IndexError, ValueError):
            st = -1
        print(f"angle {ang:3d} deg elec: state {hex(st)}")
        log.setdefault("position_demo", []).append({"angle": ang, "state": hex(st)})
    capture("position_hold")
    br.cmd("speed 0")
    br.cmd(f"w ec 0")              # release force align
    time.sleep(1.0)

    # ---- EEPROM burn --------------------------------------------------------
    if not args.no_eeprom:
        print("=== burning tuned config to EEPROM ===")
        pre = dump(br, log, "shadow_before_burn")
        br.cmd(f"w ea {(1 << 31):x}")     # EEPROM_WRT
        time.sleep(2.0)
        br.cmd(f"w ea {(1 << 30):x}")     # EEPROM_READ (reload to verify)
        time.sleep(1.0)
        post = dump(br, log, "shadow_after_eeprom_reload")
        diffs = {k: (pre[k], post[k]) for k in pre
                 if pre[k] != post[k] and k.startswith("0x0")}
        log["burn_diffs"] = diffs
        print("EEPROM verify diffs (empty = perfect):",
              {k: v for k, v in list(diffs.items())[:6]})
finally:
    br.cmd("speed 0")
    br.cmd(f"w ec 0")
    br.cmd("drvoff 1")
    log["faults_final"] = faults(br)
    json.dump(log, open(os.path.join(out, "final_log.json"), "w"), indent=1)
    print(f"safe; data -> {out}/")
