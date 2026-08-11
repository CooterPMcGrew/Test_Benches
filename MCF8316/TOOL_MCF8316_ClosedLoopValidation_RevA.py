#!/usr/bin/env python3
"""Closed-loop FOC validation with measured Ke (code 0x3C = 12.5 mV/Hz from
coast test). Scope captures at each phase of the run: slow commutation
envelope + fast PWM zoom, at two speed points. All data archived.

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_ClosedLoopValidation_RevA.py
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
BEMF_CODE = 0x3C          # 12.5 mV/Hz, measured 12.42 by coast test

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"closedloop_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp, "bemf_code": hex(BEMF_CODE)}

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


scope_setup()
ok = rmw(br, 0x8C, 0xFF << 23, BEMF_CODE << 23, log, "CL3_bemf_measured")
log["config_ok"] = bool(ok)
if not ok:
    sys.exit("BEMF write failed")

try:
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    br.cmd("drvoff 0")
    br.cmd("speed 150")
    t0 = time.time()
    trace = []
    synced = False
    while time.time() - t0 < 40:
        el = time.time() - t0
        try:
            st = reg(0x190) & 0xFF
            ct = reg(0xE2)
            fg = reg(0x196) / (1 << 27) * MAX_SPEED_HZ
            fg_ok = 0 <= fg <= 1.5 * MAX_SPEED_HZ
            trace.append({"t": round(el, 2), "state": hex(st),
                          "fg": round(fg, 1) if fg_ok else "inv",
                          "ctrl": hex(ct)})
            if ct != 0:
                print(f"FAULT 0x{ct:08X} at {el:.1f}s state {hex(st)}")
                break
            if st == 8 and fg_ok and fg > 15:
                if not synced:
                    print(f"CLOSED LOOP at {el:.1f}s, fg={fg:.1f} elHz")
                    synced = True
                    break
        except (IndexError, ValueError):
            pass
        time.sleep(0.12)
    log["startup_trace"] = trace

    if synced:
        # hold and log speed stability
        fg_hold = []
        for _ in range(30):
            try:
                fg_hold.append(round(reg(0x196) / (1 << 27) * MAX_SPEED_HZ, 1))
            except (IndexError, ValueError):
                pass
            time.sleep(0.15)
        log["hold_fg"] = fg_hold
        print(f"held: fg mean {np.mean(fg_hold):.1f} elHz, "
              f"std {np.std(fg_hold):.2f}, faults {faults(br)}")
        capture("cl_steady_slow_tb")          # 280 ms window: commutation envelope
        scope_setup(tdiv="2MS")
        capture("cl_steady_fast_tb")          # 28 ms window: sinusoidal PWM detail
        scope_setup(tdiv="20MS")

        # step up
        br.cmd("speed 300")
        t1 = time.time()
        fg2_trace = []
        fault2 = 0
        while time.time() - t1 < 15:
            try:
                ct = reg(0xE2)
                fg = reg(0x196) / (1 << 27) * MAX_SPEED_HZ
                if 0 <= fg <= 300:
                    fg2_trace.append(round(fg, 1))
                if ct != 0:
                    fault2 = ct
                    print(f"fault during step-up: 0x{ct:08X}")
                    break
            except (IndexError, ValueError):
                pass
            time.sleep(0.15)
        log["stepup_fg"] = fg2_trace
        log["stepup_fault"] = hex(fault2)
        if fault2 == 0:
            capture("cl_duty300_slow_tb")
            scope_setup(tdiv="2MS")
            capture("cl_duty300_fast_tb")
finally:
    br.cmd("speed 0")
    time.sleep(1.5)
    br.cmd("drvoff 1")
    log["faults_final"] = faults(br)
    json.dump(log, open(os.path.join(out, "closedloop_log.json"), "w"), indent=1)
    print(f"safe; data -> {out}/")
