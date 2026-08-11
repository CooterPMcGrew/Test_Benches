#!/usr/bin/env python3
"""Hall-referenced Ke verification: spin to cruise with the burned config,
release to Hi-Z coast, capture BEMF on the scope while halls independently
log rotor speed. Cross-references amplitude/frequency at several points down
the decay. Reports whether the burned code 0x3C (12.5 mV/Hz) stands.

Usage: uv run --with pyserial,numpy python TOOL_MCF8316_KeVerify_RevA.py
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
from TOOL_MCF8316_Bringup_RevA import Bridge, faults, rmw, CLR_ALL  # noqa: E402
import numpy as np  # noqa: E402

PP = 10
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = os.path.join(HERE, "data", f"keverify_{stamp}")
os.makedirs(out, exist_ok=True)
log = {"stamp_utc": stamp}

br = Bridge("/dev/ttyACM0")
scope = SDS1104XU("10.42.0.29")
def reg(a): return int(br.cmd(f"r {a:x}").split("= ")[1], 16)
def hall_cps():
    m = re.search(r"cps=([0-9.]+)", br.cmd("hall"))
    return float(m.group(1)) if m else None

for ch in ("C1", "C2", "C3"):
    scope.cmd(f"{ch}:TRA ON")
    scope.cmd(f"{ch}:ATTN 10")
    scope.cmd(f"{ch}:CPL D1M")
    scope.cmd(f"{ch}:VDIV 500MV")
    scope.cmd(f"{ch}:OFST 0V")
scope.cmd("TDIV 20MS")
scope.cmd("TRMD AUTO")

rmw(br, 0x8A, 0x7 << 28, 0x0 << 28, log, "stop_hiz_for_coast")

try:
    br.cmd("hallzero")
    br.cmd(f"w ea {CLR_ALL:x}")
    time.sleep(0.3)
    br.cmd("drvoff 0")
    br.cmd("speed 300")
    print("spinning up on burned config (slow ramp, be patient)...")
    t0 = time.time()
    rpm = 0.0
    while time.time() - t0 < 110:
        try:
            ct = reg(0xE2)
            if ct != 0:
                print(f"fault during spin-up: 0x{ct:08X}")
                break
            r = hall_cps()
            if r is not None:
                rpm = r
            if rpm > 150 and time.time() - t0 > 20:
                break
        except (IndexError, ValueError):
            pass
        time.sleep(1.0)
    print(f"releasing to coast at {rpm:.0f} RPM (hall)")

    # capture several windows down the decay, halls logged around each
    results = []
    br.cmd("speed 0")            # Hi-Z stop mode -> freewheel
    for i in range(4):
        time.sleep(0.8 if i == 0 else 2.0)
        r_before = hall_cps()
        scope.cmd("STOP")
        d = {}
        for ch in ("C1", "C2", "C3"):
            br.cmd("pins")
            dt, v = scope.wave(ch)
            if len(v) == 0:
                time.sleep(0.6)
                dt, v = scope.wave(ch)
            d[f"CH{ch[1]}"] = v
        scope.cmd("TRMD AUTO")
        r_after = hall_cps()
        n = min(len(v) for v in d.values())
        if n == 0:
            continue
        dec = max(1, n // 100000)
        n = (n // dec) * dec
        d = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in d.items()}
        t = np.arange(n // dec) * dt * dec
        save_csv(os.path.join(out, f"coast_{i}.csv"), t, d)
        y = d["CH1"] - d["CH1"].mean()
        pk = float(y.max() - y.min())
        if pk < 0.02:
            print(f"  window {i}: signal gone ({pk*1e3:.0f} mVpp), stopping")
            break
        sp = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        fr = np.fft.rfftfreq(len(y), t[1] - t[0])
        sel = fr > 1.5
        f_bemf = float(fr[sel][np.argmax(sp[sel])])
        rpm_hall = (r_before + r_after) / 2 if r_before and r_after else None
        f_hall_el = rpm_hall / 60 * PP if rpm_hall else None
        ke = pk / 2 / f_bemf * 1000
        results.append({"window": i, "Vpk": round(pk / 2, 4),
                        "f_bemf_elhz": round(f_bemf, 2),
                        "f_hall_elhz": round(f_hall_el, 2) if f_hall_el else None,
                        "rpm_hall": round(rpm_hall, 1) if rpm_hall else None,
                        "Ke_mV_per_elHz": round(ke, 2)})
        print(f"  window {i}: Vpk={pk/2:.3f} V  f_bemf={f_bemf:.2f} elHz  "
              f"f_hall={f_hall_el and round(f_hall_el,2)} elHz  Ke={ke:.2f} mV/Hz")
    log["coast_points"] = results
    if results:
        kes = [r["Ke_mV_per_elHz"] for r in results]
        log["Ke_mean"] = round(float(np.mean(kes)), 2)
        log["Ke_spread"] = round(float(np.std(kes)), 2)
        # frequency cross-check: BEMF vs hall must agree if both are honest
        pairs = [(r["f_bemf_elhz"], r["f_hall_elhz"]) for r in results
                 if r["f_hall_elhz"]]
        if pairs:
            ratio = float(np.mean([b / h for b, h in pairs]))
            log["bemf_vs_hall_freq_ratio"] = round(ratio, 3)
            print(f"\nBEMF-vs-hall frequency ratio: {ratio:.3f} "
                  f"(1.000 = pole count and both sensors agree)")
        print(f"Ke = {log['Ke_mean']} ± {log['Ke_spread']} mV/elHz "
              f"(burned code 0x3C = 12.5)")
finally:
    br.cmd("speed 0")
    rmw(br, 0x8A, 0x7 << 28, 0x2 << 28, log, "stop_brake_restore")
    br.cmd("drvoff 1")
    log["faults_final"] = faults(br)
    json.dump(log, open(os.path.join(out, "keverify_log.json"), "w"), indent=1)
    print(f"safe; data -> {out}/")
