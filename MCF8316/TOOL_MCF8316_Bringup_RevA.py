#!/usr/bin/env python3
"""MCF8316C1-Q1 bring-up: as-found dump -> parameter config -> verified
readback -> rotor-less energization tests with scope capture. All data is
archived under data/bringup_<UTCstamp>/.

Register knowledge extracted from TI SLLSFV2A (MCF8316C-Q1, rev Jul 2026).
Config is written to SHADOW registers only (no EEPROM_WRT) — power cycle
restores the as-found state by design during bring-up.

Usage:
  uv run --with pyserial,numpy python TOOL_MCF8316_Bringup_RevA.py \
      [--port /dev/ttyACM0] [--scope 10.42.0.29] [--no-energize]
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

import serial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Stator"))
from TOOL_STATOR_SweepOrchestrator_RevA import SDS1104XU, save_csv  # noqa: E402
import numpy as np  # noqa: E402

EEPROM_REGS = {
    0x80: "ISD_CONFIG", 0x82: "REV_DRIVE_CONFIG", 0x84: "MOTOR_STARTUP1",
    0x86: "MOTOR_STARTUP2", 0x88: "CLOSED_LOOP1", 0x8A: "CLOSED_LOOP2",
    0x8C: "CLOSED_LOOP3", 0x8E: "CLOSED_LOOP4", 0x90: "FAULT_CONFIG1",
    0x92: "FAULT_CONFIG2", 0x94: "REG_94", 0x96: "REG_96", 0x98: "REG_98",
    0x9A: "REG_9A", 0x9C: "REG_9C", 0x9E: "REG_9E", 0xA0: "REG_A0",
    0xA2: "REG_A2", 0xA4: "PIN_CONFIG", 0xA6: "DEVICE_CONFIG1",
    0xA8: "DEVICE_CONFIG2", 0xAA: "PERI_CONFIG1", 0xAC: "GD_CONFIG1",
    0xAE: "GD_CONFIG2",
}
RAM_REGS = {
    0xE0: "GATE_DRIVER_FAULT_STATUS", 0xE2: "CONTROLLER_FAULT_STATUS",
    0xE4: "ALGO_STATUS", 0xE6: "MTR_PARAMS", 0xE8: "ALGO_STATUS_MPET",
    0xEA: "ALGO_CTRL1", 0xEC: "ALGO_DEBUG1", 0xEE: "ALGO_DEBUG2",
    0xF0: "CURRENT_PI", 0xF2: "SPEED_PI", 0x190: "ALGORITHM_STATE",
    0x196: "FG_SPEED_FDBK",
}
CLR_FLT = 1 << 29  # ALGO_CTRL1
CLR_ALL = (1 << 29) | (1 << 28)  # CLR_FLT + CLR_FLT_RETRY_COUNT - always both

# codes from the datasheet look-up tables (parsed from SLLSFV2A):
MOTOR_RES_CODE = 0xCB   # 3.4 ohm   (measured phase R: 3.5 ohm)
MOTOR_IND_CODE = 0x0D   # 0.018 mH  (measured phase L: 18.5 uH)


class Bridge:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=2)
        time.sleep(1.2)
        self.s.reset_input_buffer()

    def cmd(self, c, wait=0.02):
        self.s.reset_input_buffer()
        self.s.write((c + "\n").encode())
        time.sleep(wait)
        return self.s.readline().decode(errors="replace").strip()

    def read(self, reg):
        for _ in range(3):   # async notices (deadman etc.) may interleave
            r = self.cmd(f"r {reg:x}")
            m = re.search(r"= ([0-9A-F]{8})", r)
            if m:
                return int(m.group(1), 16)
        raise IOError(f"read {reg:#x} failed: {r}")

    def write(self, reg, val):
        r = self.cmd(f"w {reg:x} {val:x}")
        if "ok" not in r:
            raise IOError(f"write {reg:#x} failed: {r}")


def dump(br, log, label):
    d = {}
    for regs in (EEPROM_REGS, RAM_REGS):
        for a, name in regs.items():
            try:
                d[f"0x{a:03X}_{name}"] = f"0x{br.read(a):08X}"
            except IOError as e:
                d[f"0x{a:03X}_{name}"] = f"ERR {e}"
    log[label] = d
    print(f"[{label}] dumped {len(d)} registers")
    return d


def rmw(br, reg, mask, value, log, name):
    """Read-modify-write with readback verification and parity fallback."""
    old = br.read(reg)
    new = (old & ~mask) | (value & mask)
    entry = {"reg": f"0x{reg:02X}", "old": f"0x{old:08X}", "want": f"0x{new:08X}"}
    br.write(reg, new)
    rb = br.read(reg)
    if rb != new and rb == (new ^ 0x80000000):
        entry["note"] = "device set parity bit itself"
    elif rb != new:
        # try even-parity variant
        par = bin(new & 0x7FFFFFFF).count("1") & 1
        new2 = new | (par << 31)
        br.write(reg, new2)
        rb = br.read(reg)
        entry["retry_with_parity"] = f"0x{new2:08X}"
    entry["readback"] = f"0x{rb:08X}"
    entry["verified"] = rb & 0x7FFFFFFF == new & 0x7FFFFFFF
    log.setdefault("config_writes", {})[name] = entry
    print(f"[cfg] {name}: {entry['old']} -> {entry['readback']} "
          f"({'OK' if entry['verified'] else 'MISMATCH'})")
    return entry["verified"]


def faults(br):
    gd, ct = br.read(0xE0), br.read(0xE2)
    return {"GD_FAULT": f"0x{gd:08X}", "CTRL_FAULT": f"0x{ct:08X}"}


def capture(scope, tdiv, chans=("C1", "C2", "C3")):
    scope.cmd(f"TDIV {tdiv}")
    time.sleep(0.5)
    scope.cmd("STOP")
    out = {}
    dt = None
    for ch in chans:
        dt, v = scope.wave(ch)
        out[f"CH{ch[1]}"] = v
    scope.cmd("TRMD AUTO")
    n = min(len(v) for v in out.values())
    dec = max(1, n // 100000)
    n = (n // dec) * dec
    out = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in out.items()}
    return np.arange(n // dec) * dt * dec, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--scope", default="10.42.0.29")
    ap.add_argument("--no-energize", action="store_true")
    args = ap.parse_args()

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(HERE, "data", f"bringup_{stamp}")
    os.makedirs(out, exist_ok=True)
    log = {"stamp_utc": stamp}

    br = Bridge(args.port)
    print("bridge:", br.cmd("pins"))
    br.cmd("drvoff 1")
    br.cmd("speed 0")

    dump(br, log, "asfound")
    log["faults_asfound"] = faults(br)

    # ---- configuration (shadow registers only) -----------------------------
    ok = True
    # CLOSED_LOOP2: MOTOR_RES[15:8], MOTOR_IND[7:0]
    ok &= rmw(br, 0x8A, 0x0000FFFF,
              (MOTOR_RES_CODE << 8) | MOTOR_IND_CODE, log, "CL2_res_ind")
    # CLOSED_LOOP3: MOTOR_BEMF_CONST[30:23] nonzero placeholder (MPET guard).
    # Code 0x01 = smallest table value; real Ke measured at final assembly.
    cl3 = br.read(0x8C)
    if (cl3 >> 23) & 0xFF == 0:
        ok &= rmw(br, 0x8C, 0xFF << 23, 0x01 << 23, log, "CL3_bemf_placeholder")
    # CLOSED_LOOP4: SPD_LOOP_KP[30:24]=5 (Kp 0.05), SPD_LOOP_KI[23:14]=5
    # (Ki 0.5), MAX_SPEED[13:0]=200 elec Hz
    ok &= rmw(br, 0x8E, 0x7FFFFFFF,
              (5 << 24) | (5 << 14) | 200, log, "CL4_spdloop_maxspeed")
    # PIN_CONFIG: SPEED_MODE[1:0] = 1 (PWM duty on SPEED pin)
    ok &= rmw(br, 0xA4, 0x3, 0x1, log, "PIN_speed_mode_pwm")

    dump(br, log, "postconfig")
    log["config_all_verified"] = ok

    if args.no_energize or not ok:
        print("skipping energization" if args.no_energize else
              "CONFIG MISMATCH - not energizing")
    else:
        scope = SDS1104XU(args.scope)
        print("scope:", scope.query("*IDN?"))
        for ch in ("C1", "C2", "C3"):
            scope.cmd(f"{ch}:TRA ON")
            scope.cmd(f"{ch}:ATTN 10")
            scope.cmd(f"{ch}:CPL D1M")
            scope.cmd(f"{ch}:VDIV 5V")
            scope.cmd(f"{ch}:OFST 0V")
        scope.cmd("C4:TRA OFF")
        scope.cmd("TRMD AUTO")

        tests = [("t1_slow_align_ramp", "100MS", 150, 4.0),
                 ("t2_pwm_detail", "10US", 150, 1.5),
                 ("t3_higher_speed", "100MS", 300, 4.0)]
        br.cmd("drvoff 0")
        for name, tdiv, duty, dwell in tests:
            br.cmd(f"w {0xEA:x} {CLR_FLT:x}")     # clear faults
            time.sleep(0.2)
            states = []
            br.cmd(f"speed {duty}")
            t0 = time.time()
            while time.time() - t0 < dwell:
                try:
                    states.append({
                        "t": round(time.time() - t0, 2),
                        "state": f"0x{br.read(0x190):08X}",
                        "gd": f"0x{br.read(0xE0):08X}",
                        "ctrl": f"0x{br.read(0xE2):08X}",
                    })
                except IOError:
                    states.append({"t": round(time.time() - t0, 2),
                                   "state": "read_err"})
            t, chans = capture(scope, tdiv)
            br.cmd("speed 0")
            time.sleep(0.5)
            log.setdefault("tests", {})[name] = {
                "duty": duty, "tdiv": tdiv, "state_trace": states,
                "faults_after": faults(br),
                "pkpk": {k: round(float(v.max() - v.min()), 3)
                         for k, v in chans.items()},
            }
            save_csv(os.path.join(out, f"{name}.csv"), t, chans)
            print(f"[{name}] duty={duty} pkpk={log['tests'][name]['pkpk']} "
                  f"faults={log['tests'][name]['faults_after']}")
            time.sleep(1.0)
        br.cmd("drvoff 1")
        br.cmd("speed 0")

    log["faults_final"] = faults(br)
    with open(os.path.join(out, "bringup_log.json"), "w") as fh:
        json.dump(log, fh, indent=1)
    print(f"\nall data -> {out}/")


if __name__ == "__main__":
    main()
