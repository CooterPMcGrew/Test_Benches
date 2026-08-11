#!/usr/bin/env python3
"""Bounded auto-tuning campaign for closed-loop spin quality, then a long
cruise demo and a force-align position demo. Uses only motor-parameter
registers (fault config untouched — assumes the operator-approved shadow
state from the handoff session is still live).

Score per trial: sync success, time-to-sync, |FG - target|, FG std
(smoothness). All trials and captures archived.

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_AutoTune_RevA.py
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Stator"))
from TOOL_STATOR_SweepOrchestrator_RevA import SDS1104XU, save_csv  # noqa: E402
from TOOL_MCF8316_Bringup_RevA import Bridge, faults, rmw, CLR_ALL  # noqa: E402
import numpy as np  # noqa: E402

MAX_SPEED_HZ = 200.0
DUTY = 300                      # ~59 elHz target: the point that faulted before
TARGET = DUTY / 1023 * MAX_SPEED_HZ

# (name, CL_ACC code, KP_lsb, KI_lsb) — CL_ACC: 2=2.5,3=5,4=7.5 Hz/s
TRIALS = [
    ("acc5_kp5_ki5",   0x3,  5,  5),
    ("acc2.5_kp5_ki5", 0x2,  5,  5),
    ("acc5_kp10_ki10", 0x3, 10, 10),
    ("acc5_kp20_ki25", 0x3, 20, 25),
    ("acc7.5_kp10_ki10", 0x4, 10, 10),
]

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"autotune_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp, "duty": DUTY, "target_elhz": round(TARGET, 1)}

br = Bridge("/dev/ttyACM0")
scope = SDS1104XU("10.42.0.29")
def reg(a): return int(br.cmd(f"r {a:x}").split("= ")[1], 16)


def scope_setup(tdiv="20MS"):
    for ch in ("C1", "C2", "C3"):
        scope.cmd(f"{ch}:TRA ON")
        scope.cmd(f"{ch}:ATTN 10")
        scope.cmd(f"{ch}:CPL D1M")
        scope.cmd(f"{ch}:VDIV 5V")
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
        return
    dec = max(1, n // 100000)
    n = (n // dec) * dec
    d = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in d.items()}
    save_csv(os.path.join(out, f"{name}.csv"),
             np.arange(n // dec) * dt * dec, d)
    print(f"  [{name}] captured")


def fg_valid():
    v = reg(0x196) / (1 << 27) * MAX_SPEED_HZ
    return v if 0 <= v <= 1.5 * MAX_SPEED_HZ else None


def set_gains(acc, kp, ki):
    ok = rmw(br, 0x88, 0x1F << 25, acc << 25, {}, "acc")
    ok &= rmw(br, 0x8E, (0x7F << 24) | (0x3FF << 14),
              (kp << 24) | (ki << 14), {}, "gains")
    ok &= rmw(br, 0x8C, 0x7, 0x0, {}, "kp_msb")
    return ok


def trial(name, acc, kp, ki):
    print(f"=== trial {name} ===")
    if not set_gains(acc, kp, ki):
        return {"name": name, "result": "config_fail", "score": -1}
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    br.cmd("drvoff 0")
    br.cmd(f"speed {DUTY}")
    t0 = time.time()
    res = {"name": name, "acc": acc, "kp": kp, "ki": ki}
    synced_at = None
    fault = 0
    while time.time() - t0 < 50:
        try:
            st = reg(0x190) & 0xFF
            ct = reg(0xE2)
            fg = fg_valid()
            if ct != 0:
                fault = ct
                break
            if st in (8, 9) and fg and fg > 0.5 * TARGET and synced_at is None:
                synced_at = time.time() - t0
                break
        except (IndexError, ValueError):
            pass
        time.sleep(0.12)
    if fault or synced_at is None:
        br.cmd("speed 0")
        time.sleep(2.5)
        res.update({"result": f"fault 0x{fault:08X}" if fault else "no_sync",
                    "score": 0})
        print(f"  {res['result']}")
        return res
    # hold 6 s, gather valid FG
    hold = []
    t1 = time.time()
    while time.time() - t1 < 6:
        try:
            f = fg_valid()
            if f is not None:
                hold.append(f)
            if reg(0xE2) != 0:
                fault = reg(0xE2)
                break
        except (IndexError, ValueError):
            pass
        time.sleep(0.12)
    br.cmd("speed 0")
    if fault or len(hold) < 5:
        res.update({"result": f"fault_in_hold 0x{fault:08X}", "score": 0.2})
        print(f"  faulted during hold")
        time.sleep(2.5)
        return res
    mean, std = float(np.mean(hold)), float(np.std(hold))
    err = abs(mean - TARGET) / TARGET
    score = max(0.0, 1.0 - err) + max(0.0, 1.0 - std / 5.0) + \
        max(0.0, 1.0 - synced_at / 30.0)
    res.update({"result": "ok", "sync_s": round(synced_at, 1),
                "fg_mean": round(mean, 1), "fg_std": round(std, 2),
                "score": round(score, 3)})
    print(f"  sync {synced_at:.1f}s  fg {mean:.1f}±{std:.1f} (target {TARGET:.0f})"
          f"  score {score:.2f}")
    time.sleep(2.5)
    return res


scope_setup()
try:
    results = [trial(*t) for t in TRIALS]
    log["trials"] = results
    best = max(results, key=lambda r: r.get("score", -1))
    log["best"] = best
    print(f"\nBEST: {best['name']} score {best.get('score')}")

    if best.get("result") == "ok":
        # long cruise demo with the winner + captures
        set_gains(best["acc"], best["kp"], best["ki"])
        br.cmd(f"w ea {CLR_ALL:x}")
        time.sleep(0.3)
        br.cmd("drvoff 0")
        br.cmd(f"speed {DUTY}")
        t0 = time.time()
        cruise = []
        while time.time() - t0 < 30:
            try:
                if reg(0xE2) != 0:
                    print("fault during cruise")
                    break
                f = fg_valid()
                if f is not None:
                    cruise.append(round(f, 1))
                if abs(time.time() - t0 - 15) < 0.2:
                    capture("cruise_steady")
            except (IndexError, ValueError):
                pass
            time.sleep(0.15)
        log["cruise_fg"] = cruise
        if cruise:
            print(f"cruise 30s: fg {np.mean(cruise):.1f} ± {np.std(cruise):.2f} elHz "
                  f"({len(cruise)} samples)")
        br.cmd("speed 0")
        time.sleep(3)

        # position demo: force-align servo stepping
        print("=== position demo ===")
        br.cmd(f"w ea {CLR_ALL:x}")
        time.sleep(0.3)
        br.cmd(f"w ec {((1 << 14) | (1 << 10)):x}")
        br.cmd("drvoff 0")
        br.cmd("speed 100")
        time.sleep(7.5)
        for ang in (0, 90, 180, 270, 135, 0):
            br.cmd(f"w ea {(ang << 11):x}")
            time.sleep(1.2)
            print(f"  angle {ang:3d} deg elec commanded")
            log.setdefault("position_angles", []).append(ang)
        capture("position_hold")
        br.cmd("speed 0")
        br.cmd("w ec 0")
finally:
    br.cmd("speed 0")
    br.cmd("w ec 0")
    br.cmd("drvoff 1")
    log["faults_final"] = faults(br)
    json.dump(log, open(os.path.join(out, "autotune_log.json"), "w"), indent=1)
    print(f"safe; data -> {out}/")
