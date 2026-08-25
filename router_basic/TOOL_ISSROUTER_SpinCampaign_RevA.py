"""Spin campaign: MCT8316Z speed ladder + full logging + synced scope shots.

Usage: uv run --with numpy,pyserial python3 TOOL_ISSROUTER_SpinCampaign_RevA.py \
           [--duties 128,256,384] [--dwell 12] [--shots 1] [--tdiv 10MS] \
           [--vdiv 500MV] [--label spin1]

Per duty step: settle, dwell of continuous ADC stream logging (16 ch + FG),
CSV pause, N synchronized dual-scope shots (6 coils + FGOUT + siggen node,
EXT-triggered by the S3 strobe), CSV resume. Everything lands in
data/SPIN_<UTC>_<label>/:
    stream.csv        every ADC frame of the whole session, duty-tagged
    firmware_log.txt  every non-CSV firmware line (faults, pot guard, OVF)
    shotN_dD_scopeX.csv  scope waveforms per shot per duty
    REPORT.txt        FG and per-coil activity summary per step

Owns the S3 serial port -- close StreamPlot/dashboard first. Aborts the
ladder (motor commanded to 0) if nFAULT is asserted or the MCT SPI probe
fails; ADC logging continues regardless.
"""
import argparse
import os
import time

import numpy as np
import serial

import TOOL_ISSROUTER_DualScope_RevA as ds

CH_NAMES = ["ISNS_0/c1", "ISNS_1/c2", "ISNS_2/c3", "ISNS_3/c4",
            "ISNS_5/c6", "ISNS_4/c5",
            "ANALOG_0", "ANALOG_1", "ANALOG_2", "ANALOG_3",
            "ANALOG_5", "ANALOG_4", "ANALOG_6", "ANALOG_7",
            "ANALOG_8", "ANALOG_9"]


class Session:
    def __init__(self, out):
        self.s = serial.Serial(ds.s3_port(), 115200, timeout=0.2)
        time.sleep(0.3)
        self.s.reset_input_buffer()
        self.stream = open(f"{out}/stream.csv", "w")
        self.stream.write("t_ms," + ",".join(CH_NAMES) + ",FG_Hz,duty\n")
        self.fwlog = open(f"{out}/firmware_log.txt", "w")
        self.duty = 0
        self.frames = []          # (t_ms, [16 volts], fg) for current window
        self.fault = False

    def _stamp(self):
        return time.strftime("%H:%M:%SZ", time.gmtime())

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            raw = self.s.readline()
            if not raw:
                continue
            line = raw.decode(errors="replace").strip()
            parts = line.split(",")
            if len(parts) == 18:
                try:
                    vals = [float(p) for p in parts]
                    self.frames.append((vals[0], vals[1:17], vals[17]))
                    self.stream.write(line + f",{self.duty}\n")
                    continue
                except ValueError:
                    pass
            if line:
                self.fwlog.write(f"{self._stamp()} {line}\n")
                self.fwlog.flush()
                if "nFAULT asserted" in line:
                    self.fault = True
                    print(f"  !! {line}")
                if "OVF" in line or "pot guard] !!" in line:
                    print(f"  !! {line}")
        self.stream.flush()

    def cmd(self, ch, wait=0.5):
        self.s.write(ch.encode())
        self.pump(wait)

    def window(self):
        w, self.frames = self.frames, []
        return w

    def set_duty(self, target):
        # firmware steps 64/press; walk from current to target
        steps = (target - self.duty) // 64
        for _ in range(abs(steps)):
            self.cmd("+" if steps > 0 else "-", 0.15)
        self.duty = self.duty + steps * 64
        self.pump(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duties", default="128,256,384")
    ap.add_argument("--dwell", type=float, default=12.0)
    ap.add_argument("--shots", type=int, default=1)
    ap.add_argument("--tdiv", default="10MS")
    ap.add_argument("--vdiv", default="500MV")
    ap.add_argument("--label", default="spin")
    args = ap.parse_args()
    duties = [int(d) for d in args.duties.split(",")]

    out = time.strftime(f"data/SPIN_%Y%m%dT%H%M%SZ_{args.label}", time.gmtime())
    os.makedirs(out, exist_ok=True)
    report = [f"=== spin campaign {out} ==="]

    def rep(line):
        print(line, flush=True)
        report.append(line)

    ses = Session(out)

    # scopes: reachable ones only; roles per module docstring
    scopes = []
    for tag, ip, names in ds.SCOPES:
        try:
            sc = ds.SDS(ip)
            sc.setup(args.tdiv, args.vdiv)
            scopes.append((tag, sc, names))
            rep(f"scope {tag} [{ip}] armed: {names}")
        except OSError:
            rep(f"scope {tag} [{ip}] OFFLINE")

    # --- MCT probe gate ---
    ses.cmd("m", 3.0)
    probe = open(f"{out}/firmware_log.txt").read()
    if "RESPONDING" not in probe:
        rep("ABORT: MCT8316Z not responding on SPI (motor VM on? wiring?)")
        rep("       ADC baseline still logged for 20 s.")
        ses.cmd("c", 1.0)
        ses.pump(20)
        ses.cmd("c", 0.5)
        finish(out, report)
        return
    rep("MCT8316Z responding -- starting ladder")

    ses.cmd("c", 1.0)                      # CSV on for the whole session

    def step(duty):
        ses.set_duty(duty)
        rep(f"[duty {duty}/1023] settling 3 s...")
        ses.pump(3.0)
        ses.window()                       # discard spin-up transient
        ses.pump(args.dwell)
        w = ses.window()
        if w:
            fg = [f for _, _, f in w]
            rep(f"  {len(w)} frames  FG mean {np.mean(fg):7.1f} Hz  "
                f"min {min(fg):0.1f}  max {max(fg):0.1f}")
            for c in range(6):
                ac = np.std([v[c] for _, v, _ in w]) * 1000
                rep(f"    {CH_NAMES[c]:>9s} activity {ac:7.2f} mV rms")
        if ses.fault:
            return False
        # scope shots: pause CSV so nothing is lost during slow readout
        if scopes:
            ses.cmd("c", 0.5)
            for shot in range(args.shots):
                for _, sc, _ in scopes:
                    sc.arm()
                ses.pump(1.5)
                ses.cmd("t", 0.5)          # sync strobe
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if all(sc.stopped() for _, sc, _ in scopes):
                        break
                    time.sleep(0.3)
                for tag, sc, names in scopes:
                    cols, dt = {}, None
                    for i, ch in enumerate(("C1", "C2", "C3", "C4")):
                        dt, v = sc.wave(ch)
                        cols[names[i]] = v
                    n = len(next(iter(cols.values())))
                    t = np.arange(n) * dt
                    path = f"{out}/shot{shot}_d{duty}_scope{tag}.csv"
                    with open(path, "w") as f:
                        f.write("t_s," + ",".join(names) + "\n")
                        np.savetxt(f, np.column_stack(
                            [t] + [cols[nm] for nm in names]),
                            delimiter=",", fmt="%.6E")
                    rep(f"  wrote {path} ({n} samples @ {dt*1e6:0.3f} us)")
            ses.cmd("c", 1.0)
        return True

    try:
        for duty in duties:
            if not step(duty):
                rep("nFAULT during ladder -- stopping motor")
                break
    finally:
        ses.cmd("0", 0.5)                  # motor to zero, always
        rep("motor commanded to 0")
        ses.pump(3.0)
        ses.cmd("c", 0.5)                  # CSV off, human mode restored
        ses.stream.close()
        ses.fwlog.close()

    finish(out, report)


def finish(out, report):
    with open(f"{out}/REPORT.txt", "w") as f:
        f.write("\n".join(report) + "\n")
    print(f"\nall data in {out}/")


if __name__ == "__main__":
    main()
