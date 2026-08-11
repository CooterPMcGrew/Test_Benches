#!/usr/bin/env python3
"""Grand tuning campaign: sweep startup/acceleration parameter space with
hall-truth scoring, validate the winner in both directions, then burn the
winning configuration to EEPROM (operator-directed: "BURN IT").

Grid: CL_ACC {0.5,1,2.5 Hz/s} x OL_ACC_A1 {2.5,5 Hz/s} x OL_ILIMIT {1.5,2 A}
Fixed: duty 300 (354 RPM cmd), validated R/L/Ke, gentle gains, locks off
(validated recipe - documented rationale in TR), 60 kHz PWM, brake stop.
Score per trial (hall RPM only): RPM@60s + 0.5*RPM@30s, 0 on fault.

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_GrandTune_RevA.py
"""
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Stator"))
from TOOL_MCF8316_Bringup_RevA import (  # noqa: E402
    Bridge, faults, dump, rmw, CLR_ALL, MOTOR_RES_CODE, MOTOR_IND_CODE)

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"grandtune_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp}

br = Bridge("/dev/ttyACM0")
def reg(a): return int(br.cmd(f"r {a:x}").split("= ")[1], 16)
def hall():
    m = re.search(r"pos=(-?\d+) edges=(\d+) bad=(\d+) cps=([0-9.]+)", br.cmd("hall"))
    return (int(m.group(1)), int(m.group(3)), float(m.group(4))) if m else (None, None, None)


def base_config():
    ok = True
    ok &= rmw(br, 0x8A, (0x7 << 28) | 0xFFFF,
              (0x2 << 28) | (MOTOR_RES_CODE << 8) | MOTOR_IND_CODE, {}, "CL2")
    ok &= rmw(br, 0x8C, 0xFF << 23, 0x3C << 23, {}, "CL3")
    ok &= rmw(br, 0x8E, 0x7FFFFFFF, (5 << 24) | (5 << 14) | 200, {}, "CL4")
    ok &= rmw(br, 0x88, 0xF << 15, 0xA << 15, {}, "CL1pwm")
    ok &= rmw(br, 0x84, 0xF << 25, 0x1 << 25, {}, "MS1ramp")
    ok &= rmw(br, 0xA4, 0x3, 0x1, {}, "PIN")
    ok &= rmw(br, 0x90, (0xF << 23) | (0xF << 19) | (0xF << 11),
              (0xF << 23) | (0xF << 19) | (0x5 << 11), {}, "FC1")
    ok &= rmw(br, 0x92, (1 << 30) | (1 << 29), 0, {}, "FC2locksoff")
    return ok


def spin_down_wait(budget=25.0):
    t0 = time.time()
    while time.time() - t0 < budget:
        _, _, cps = hall()
        if cps is not None and cps < 4:
            return True
        time.sleep(0.8)
    return False


def trial(name, cl_acc, ol_a1, ol_il, direction=0, dwell=62.0, duty=300):
    print(f"=== {name} (dir={direction}) ===")
    rmw(br, 0x88, 0x1F << 25, cl_acc << 25, {}, "acc")
    rmw(br, 0x86, 0x7FF80000, (ol_il << 27) | (ol_a1 << 23), {}, "ol")
    br.cmd(f"dir {direction}")
    br.cmd("hallzero")
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    br.cmd("drvoff 0")
    br.cmd(f"speed {duty}")
    t0 = time.time()
    trace = []
    fault = 0
    while time.time() - t0 < dwell:
        el = time.time() - t0
        try:
            st = reg(0x190) & 0xFF
            ct = reg(0xE2)
            pos, bad, cps = hall()
            trace.append({"t": round(el, 1), "state": st, "rpm": cps,
                          "pos": pos})
            if ct != 0:
                fault = ct
                break
        except (IndexError, ValueError, TypeError):
            pass
        time.sleep(0.7)
    br.cmd("speed 0")
    valid = [x for x in trace if x["rpm"] is not None]
    def rpm_at(ts):
        c = [x["rpm"] for x in valid if abs(x["t"] - ts) < 3]
        return max(c) if c else 0.0
    r30, r60 = rpm_at(30), rpm_at(58)
    score = 0.0 if fault else r60 + 0.5 * r30
    net_pos = valid[-1]["pos"] if valid else 0
    res = {"name": name, "cl_acc": cl_acc, "ol_a1": ol_a1, "ol_il": ol_il,
           "dir": direction, "fault": hex(fault), "rpm30": r30, "rpm60": r60,
           "net_pos": net_pos, "score": round(score, 1)}
    json.dump(trace, open(os.path.join(out, f"{name}_trace.json"), "w"))
    print(f"  rpm@30={r30:.0f} rpm@60={r60:.0f} fault={hex(fault)} "
          f"pos_sign={'+' if net_pos >= 0 else '-'} score={score:.0f}")
    ok = spin_down_wait()
    if not ok:
        print("  (still spinning after brake budget)")
    time.sleep(1.5)
    return res


if not base_config():
    sys.exit("base config failed")

GRID = [(f"acc{a}_ola{o}_oli{i}", a, o, i)
        for a in (0x0, 0x1, 0x2)         # 0.5 / 1 / 2.5 Hz/s closed loop
        for o in (0x3, 0x4)              # 2.5 / 5 Hz/s open loop
        for i in (0x4, 0x5)]             # 1.5 / 2.0 A open loop

results = []
try:
    for name, a, o, i in GRID:
        results.append(trial(name, a, o, i))
    log["grid_results"] = results
    best = max(results, key=lambda r: r["score"])
    log["best"] = best
    print(f"\nBEST: {best['name']} score {best['score']}  "
          f"(rpm60 {best['rpm60']:.0f})")

    # winner: validate both directions
    print("\n=== direction validation with winner ===")
    fwd = trial("winner_dir0", best["cl_acc"], best["ol_a1"], best["ol_il"],
                direction=0, dwell=70)
    rev = trial("winner_dir1", best["cl_acc"], best["ol_a1"], best["ol_il"],
                direction=1, dwell=70)
    log["winner_dir0"] = fwd
    log["winner_dir1"] = rev
    dir_ok = (fwd["net_pos"] >= 0) != (rev["net_pos"] >= 0) and \
        fwd["fault"] == "0x0" and rev["fault"] == "0x0"
    log["direction_reversal_verified"] = bool(dir_ok)
    print(f"direction reversal verified: {dir_ok} "
          f"(pos {fwd['net_pos']} vs {rev['net_pos']})")

    # set winner config and BURN to EEPROM (operator-directed)
    print("\n=== EEPROM BURN ===")
    rmw(br, 0x88, 0x1F << 25, best["cl_acc"] << 25, {}, "winner_acc")
    rmw(br, 0x86, 0x7FF80000, (best["ol_il"] << 27) | (best["ol_a1"] << 23),
        {}, "winner_ol")
    br.cmd("dir 0")
    pre = dump(br, log, "shadow_before_burn")
    br.cmd(f"w ea {(1 << 31):x}")        # EEPROM_WRT
    time.sleep(2.5)
    br.cmd(f"w ea {(1 << 30):x}")        # EEPROM_READ: reload to verify
    time.sleep(1.5)
    post = dump(br, log, "eeprom_readback")
    diffs = {k: (pre[k], post[k]) for k in pre
             if pre[k] != post[k] and k.startswith("0x0")
             and not k.startswith("0x0E") and not k.startswith("0x1")}
    log["burn_diffs"] = diffs
    print("burn verify diffs (config regs; empty = burned perfectly):")
    for k, v in diffs.items():
        print("  ", k, v)
    if not diffs:
        print("  none — EEPROM matches the winning configuration")
finally:
    br.cmd("speed 0")
    br.cmd("drvoff 1")
    log["faults_final"] = faults(br)
    json.dump(log, open(os.path.join(out, "grandtune_log.json"), "w"), indent=1)
    print(f"\nsafe; data -> {out}/")
