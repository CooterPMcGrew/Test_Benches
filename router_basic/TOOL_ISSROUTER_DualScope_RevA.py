"""Synchronized 6-coil capture across both Siglent scopes.

Usage: uv run --with numpy,pyserial python3 TOOL_ISSROUTER_DualScope_RevA.py \
           [--tdiv 10MS] [--vdiv 500MV] [--n 1] [--auto] [--label spin1]

Topology (TN_ISSROUTER doc §scope-plan):
    SDS1104X-E 10.11.13.220: C1=coil1 C2=coil2 C3=coil3 C4=FGOUT
    SDS1104X-U 10.11.13.221: C1=coil4 C2=coil5 C3=coil6 C4=siggen/ANALOG_0
    SYNC = S3 GPIO15 strobe -> EXT trigger input of BOTH scopes. Both arm
    SINGLE on its rising edge; one 't' serial command fires them aligned.

--auto skips the sync trigger (TRMD AUTO + force) -- pipeline check without
the GPIO15 wire. A missing scope degrades to 3-coil single-scope capture.

Output: data/COILSCOPE_<UTC>_<label>/ -- per-scope CSVs (t, C1..C4 volts)
plus METRICS.txt (pkpk / rms / dominant frequency per coil).
"""
import argparse
import os
import re
import socket
import time

import numpy as np
import serial
import glob

SCOPES = [("A", "10.11.13.220", ("coil1", "coil2", "coil3", "fgout")),
          ("B", "10.11.13.221", ("coil4", "coil5", "coil6", "siggen"))]


class SDS:
    """Raw-socket SCPI; waveform scaling V = code * vdiv / 25 - ofst."""

    def __init__(self, host):
        self.host = host
        self.sk = socket.create_connection((host, 5025), timeout=5)
        self.sk.settimeout(5)
        self.cmd("CHDR OFF")
        self.idn = self.query("*IDN?")

    def cmd(self, c):
        self.sk.sendall((c + "\n").encode())
        time.sleep(0.25)   # X-E drops rapid-fire commands

    def query(self, c):
        self.sk.sendall((c + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            buf += self.sk.recv(4096)
        return buf.decode(errors="replace").strip()

    def qnum(self, c):
        m = re.search(r"[-+0-9.Ee]+", self.query(c))
        if not m:
            raise ValueError(f"no number in reply to {c}")
        return float(m.group())

    @staticmethod
    def _tdiv_s(tok):
        m = re.match(r"([0-9.]+)(NS|US|MS|S)", tok.upper())
        return float(m.group(1)) * {"NS": 1e-9, "US": 1e-6, "MS": 1e-3, "S": 1.0}[m.group(2)]

    def setup(self, tdiv, vdiv):
        for ch in ("C1", "C2", "C3", "C4"):
            self.cmd(f"{ch}:TRA ON")
            self.cmd(f"{ch}:CPL D1M")
        for ch in ("C1", "C2", "C3"):
            self.cmd(f"{ch}:VDIV {vdiv}")
        self.cmd("C4:VDIV 1V")        # 3.3 V sync strobe
        self.cmd(f"TDIV {tdiv}")
        self.cmd("MSIZ 700K")   # 4-ch depth family is 7K/70K/700K/7M;
                                # 5 MSa/s at 10MS/div, sane readout time
        if abs(self.qnum("TDIV?") - self._tdiv_s(tdiv)) > 1e-12:
            self.cmd(f"TDIV {tdiv}")    # retry once: X-E occasionally drops
        # sync strobe (S3 GPIO15) now feeds both EXT inputs; all 8 display
        # channels carry signals (6 coils + FGOUT + PWM)
        self.cmd("TRSE EDGE,SR,EX,HT,OFF")
        self.cmd("EX:TRLV 1.5V")
        self.cmd("EX:TRSL POS")

    def arm(self):
        self.cmd("TRMD SINGLE")

    def force(self):
        self.cmd("TRMD AUTO")
        time.sleep(2.0)     # let one full record fill
        self.cmd("STOP")    # acquisition must be stopped before readout

    def stopped(self):
        return "Stop" in self.query("SAST?")

    def wave(self, ch):
        self.sk.sendall(f"{ch}:WF? DAT2\n".encode())
        buf = b""
        while b"#9" not in buf:
            buf += self.sk.recv(4096)
        head = buf.index(b"#9")
        while len(buf) < head + 11:
            buf += self.sk.recv(4096)
        n = int(buf[head + 2:head + 11])
        need = head + 11 + n + 2
        while len(buf) < need:
            buf += self.sk.recv(65536)
        raw = np.frombuffer(buf[head + 11:head + 11 + n], dtype=np.int8)
        vdiv = self.qnum(f"{ch}:VDIV?")
        ofst = self.qnum(f"{ch}:OFST?")
        sara = self.qnum("SARA?")
        return 1.0 / sara, raw.astype(np.float64) * vdiv / 25.0 - ofst


def s3_port():
    for pat in ("/dev/serial/by-id/*Espressif*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def fire_trigger():
    port = s3_port()
    if not port:
        return False
    s = serial.Serial(port, 115200, timeout=0.3)
    time.sleep(0.2)
    s.write(b"t")
    time.sleep(0.3)
    s.close()
    return True


def dominant_freq(dt, y):
    if len(y) < 16:
        return 0.0
    spec = np.abs(np.fft.rfft((y - y.mean()) * np.hanning(len(y))))
    return float(np.fft.rfftfreq(len(y), dt)[int(np.argmax(spec))])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tdiv", default="10MS")
    ap.add_argument("--vdiv", default="500MV")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--auto", action="store_true",
                    help="no sync wire yet: AUTO+force instead of SINGLE")
    ap.add_argument("--label", default="cap")
    args = ap.parse_args()

    out = time.strftime(f"data/COILSCOPE_%Y%m%dT%H%M%SZ_{args.label}",
                        time.gmtime())
    os.makedirs(out, exist_ok=True)
    metrics = []

    scopes = []
    for tag, ip, names in SCOPES:
        try:
            sc = SDS(ip)
            sc.setup(args.tdiv, args.vdiv)
            scopes.append((tag, sc, names))
            print(f"scope {tag} [{ip}] {sc.idn.split(',')[1]}: armed roles {names}")
        except OSError:
            print(f"scope {tag} [{ip}] OFFLINE -- continuing without it")
    if not scopes:
        raise SystemExit("no scopes reachable")

    for shot in range(args.n):
        if args.auto:
            for _, sc, _ in scopes:
                sc.force()
            time.sleep(2.0)
        else:
            for _, sc, _ in scopes:
                sc.arm()
            time.sleep(1.5)             # arming latency before the strobe
            if not fire_trigger():
                raise SystemExit("S3 not reachable to fire sync trigger")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if all(sc.stopped() for _, sc, _ in scopes):
                    break
                time.sleep(0.3)
            else:
                print(f"shot {shot}: a scope never triggered -- strobe on both EXT inputs?")

        for tag, sc, names in scopes:
            cols, dt = {}, None
            for i, ch in enumerate(("C1", "C2", "C3", "C4")):
                dt, v = sc.wave(ch)
                cols[names[i]] = v
            nsamp = len(next(iter(cols.values())))
            t = np.arange(nsamp) * dt
            path = f"{out}/shot{shot}_scope{tag}.csv"
            with open(path, "w") as f:
                f.write("t_s," + ",".join(names) + "\n")
                np.savetxt(f, np.column_stack([t] + [cols[n] for n in names]),
                           delimiter=",", fmt="%.6E")
            for n in names:
                if n == "sync":
                    continue
                y = cols[n]
                line = (f"shot{shot} {n}: pkpk {np.ptp(y)*1000:8.1f} mV  "
                        f"rms {np.std(y)*1000:7.2f} mV  "
                        f"f0 {dominant_freq(dt, y):8.1f} Hz")
                metrics.append(line)
                print("  " + line)
            print(f"  wrote {path} ({nsamp} samples @ {dt*1e6:0.3f} us)")

    with open(f"{out}/METRICS.txt", "w") as f:
        f.write("\n".join(metrics) + "\n")
    print(f"\nall data in {out}/")


if __name__ == "__main__":
    main()
