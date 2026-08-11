#!/usr/bin/env python3
"""Staged gentle-start spin test for high-inertia wheel. Slow open-loop ramp
(2.5 Hz/s), 1.0 A open-loop current, per-stage success criteria, automatic
abort on faults or failure to sync, safe state guaranteed by try/finally AND
the bridge's 3 s dead-man switch. Ke coast capture after the last good stage.

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_GentleSpin_RevA.py
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Stator"))
from TOOL_STATOR_SweepOrchestrator_RevA import SDS1104XU, save_csv  # noqa: E402
from TOOL_MCF8316_Bringup_RevA import (  # noqa: E402
    Bridge, faults, rmw, CLR_ALL, MOTOR_RES_CODE, MOTOR_IND_CODE)
import numpy as np  # noqa: E402

MAX_SPEED_HZ = 200.0
STAGES = [(150, 25.0)]   # known-good point only; ladder resumes after real Ke

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"gentle_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp}

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


def reg(addr):
    return int(br.cmd(f"r {addr:x}").split("= ")[1], 16)


def grab_save(name):
    d = {}
    dt = None
    for ch in ("C1", "C2", "C3"):
        br.cmd("pins")                    # feed the dead-man during long reads
        dt, v = scope.wave(ch)
        if len(v) == 0:
            time.sleep(0.6)
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


ok = True
# FULL config re-push, unconditionally — a power cycle wipes shadow registers
# (learned the hard way; see incident notes in the bring-up report).
ok &= rmw(br, 0x8A, 0x0000FFFF, (MOTOR_RES_CODE << 8) | MOTOR_IND_CODE,
          log, "CL2_res_ind")
if (int(br.cmd("r 8c").split("= ")[1], 16) >> 23) & 0xFF == 0:
    ok &= rmw(br, 0x8C, 0xFF << 23, 0x01 << 23, log, "CL3_bemf_placeholder")
ok &= rmw(br, 0x8E, 0x7FFFFFFF, (5 << 24) | (5 << 14) | 200,
          log, "CL4_spdloop_maxspeed")
ok &= rmw(br, 0xA4, 0x3, 0x1, log, "PIN_speed_mode_pwm")
# MOTOR_STARTUP2: restore full as-found profile (proven at this duty);
# slow-ramp experiments trip LOCK_ILIMIT, see incident notes
ok &= rmw(br, 0x86, 0x7FFFFFFF, 0x2306600C, log, "MS2_asfound_restore")
log["config_ok"] = ok
if not ok:
    json.dump(log, open(os.path.join(out, "gentle_log.json"), "w"), indent=1)
    sys.exit("config failed, not energizing")

last_good_duty = None
try:
    for duty, budget in STAGES:
        target_hz = duty / 1023 * MAX_SPEED_HZ
        name = f"stage_{duty}"
        print(f"=== {name}: target ~{target_hz:.0f} elHz, budget {budget}s ===")
        br.cmd(f"w ea {CLR_ALL:x}")
        time.sleep(0.3)
        br.cmd("drvoff 0")
        br.cmd(f"speed {duty}")
        trace = []
        t0 = time.time()
        result = "timeout"
        while time.time() - t0 < budget:
            el = time.time() - t0
            try:
                st = reg(0x190)
                fg = reg(0x196) / (1 << 27) * MAX_SPEED_HZ
                ct = reg(0xE2)
                fg_valid = 0.0 <= fg <= 1.5 * MAX_SPEED_HZ
                trace.append({"t": round(el, 2), "state": f"0x{st:X}",
                              "fg_elhz": round(fg, 2) if fg_valid else "INVALID",
                              "ctrl": f"0x{ct:08X}"})
                if ct != 0:
                    result = f"FAULT 0x{ct:08X}"
                    break
                if fg_valid and fg > 0.7 * target_hz and st != 0 and el > 3.0:
                    result = "synced"
                    break
                if el > 12.0 and st in (0, 1, 2, 3):
                    result = f"no_start (state 0x{st:X})"
                    break
            except (IndexError, ValueError):
                trace.append({"t": round(el, 2), "state": "err"})
            time.sleep(0.15)
        log.setdefault("stages", {})[name] = {"duty": duty, "result": result,
                                              "trace_tail": trace[-8:],
                                              "n_samples": len(trace)}
        with open(os.path.join(out, f"{name}_trace.json"), "w") as fh:
            json.dump(trace, fh, indent=1)
        print(f"[{name}] {result}, fg_last="
              f"{trace[-1].get('fg_elhz') if trace else '?'}")
        if result == "synced":
            # hold 3 s, capture steady state waveform
            for _ in range(20):
                br.cmd("pins")
                time.sleep(0.15)
            scope.cmd("TDIV 20MS")
            time.sleep(0.6)
            scope.cmd("STOP")
            grab_save(f"{name}_steady")
            scope.cmd("TRMD AUTO")
            last_good_duty = duty
            log["stages"][name]["fg_at_capture"] = round(
                reg(0x196) / (1 << 27) * MAX_SPEED_HZ, 2)
            br.cmd("speed 0")
            time.sleep(2.0)
        else:
            br.cmd("speed 0")
            br.cmd("drvoff 1")
            print("stage failed - stopping ladder")
            break

    if last_good_duty:
        # Ke coast: rerun last good stage, then cut drive mid-capture
        print(f"=== Ke coast from duty {last_good_duty} ===")
        br.cmd(f"w ea {CLR_ALL:x}")
        time.sleep(0.3)
        br.cmd("drvoff 0")
        br.cmd(f"speed {last_good_duty}")
        t0 = time.time()
        fg = 0.0
        while time.time() - t0 < 30:
            try:
                fg = reg(0x196) / (1 << 27) * MAX_SPEED_HZ
                if fg > 0.7 * last_good_duty / 1023 * MAX_SPEED_HZ and \
                        time.time() - t0 > 3:
                    break
                if reg(0xE2) != 0:
                    raise RuntimeError("fault during Ke rerun")
            except (IndexError, ValueError):
                pass
            time.sleep(0.15)
        log["ke_precut_fg_elhz"] = round(fg, 2)
        scope.cmd("TDIV 20MS")
        time.sleep(0.5)
        br.cmd("drvoff 1")             # coast: outputs Hi-Z, BEMF visible
        time.sleep(0.08)
        scope.cmd("STOP")
        br.cmd("speed 0")
        t, d = grab_save("coast_bemf")
        scope.cmd("TRMD AUTO")
        if t is not None:
            tail = t > t[-1] - 0.12
            y = d["CH1"][tail] - d["CH1"][tail].mean()
            if y.max() - y.min() > 0.05:
                sp = np.abs(np.fft.rfft(y * np.hanning(len(y))))
                fr = np.fft.rfftfreq(len(y), t[1] - t[0])
                f_e = fr[np.argmax(sp[1:]) + 1]
                vpk = (y.max() - y.min()) / 2
                log["ke"] = {"coast_f_elhz": round(float(f_e), 2),
                             "coast_Vpk_LL": round(float(vpk), 3),
                             "Ke_V_per_elHz": round(float(vpk / f_e), 5)}
                print("Ke:", log["ke"])
finally:
    br.cmd("speed 0")
    br.cmd("drvoff 1")
    log["faults_final"] = faults(br)
    json.dump(log, open(os.path.join(out, "gentle_log.json"), "w"), indent=1)
    print(f"\nsafe state asserted; all data -> {out}/")
