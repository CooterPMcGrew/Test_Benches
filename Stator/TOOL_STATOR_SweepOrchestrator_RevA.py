#!/usr/bin/env python3
"""Automated impedance sweep: JDS6600 generator (serial) + SDS1104X-U scope (SCPI/LAN).

Per frequency point: set the generator, scale the scope, capture all four
channels, save a Siglent-compatible CSV, and lock-in extract Z live.

Channel roles (see README.md wiring):
    CH1 = V across DUT+Rsense (probe at terminal A, 10x)
    CH2 = V across Rsense      (current sense, 10x)
    CH3 = third terminal       (mutual/saliency, 10x)
    CH4 = generator via T      (1x, cross-check + trigger)

Usage:
    uv run --with numpy,pyserial python TOOL_STATOR_SweepOrchestrator_RevA.py \
        --rsense 10.0 --out data/run1 [--scope 10.42.0.29] [--gen /dev/ttyUSB0] \
        [--vpp 5.0] [--freqs 1e3,2e3,5e3,1e4,2e4,5e4,1e5,2e5,5e5,1e6] [--smoke]
"""
import argparse
import os
import re
import socket
import time
import numpy as np
import serial


class JDS6600:
    """Minimal driver: sine on channel 1. Registers: 20 on/off, 21 wave,
    23 freq (Hz*100, unit 0), 25 ampl (mV), 27 offset (code-1000 = V*100)."""

    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=1.0)

    def _cmd(self, c):
        self.s.reset_input_buffer()
        self.s.write((c + "\r\n").encode())
        r = self.s.read_until(b"\n", 64)
        time.sleep(0.08)
        return r.decode(errors="replace").strip()

    def setup_sine(self, vpp):
        self._cmd(":w21=0.")                       # ch1 sine
        self._cmd(f":w25={int(vpp * 1000)}.")      # amplitude, mV
        self._cmd(":w27=1000.")                    # offset 0 V
        self._cmd(":w20=1,0.")                     # ch1 on, ch2 off

    def set_freq(self, f_hz):
        self._cmd(f":w23={int(round(f_hz * 100))},0.")

    def output_off(self):
        self._cmd(":w20=0,0.")


class SDS1104XU:
    """Raw-socket SCPI. Waveform scaling: V = code * vdiv/25 - offset."""

    def __init__(self, host, port=5025):
        self.sk = socket.create_connection((host, port), timeout=5)
        self.cmd("CHDR OFF")

    def cmd(self, c):
        self.sk.sendall((c + "\n").encode())
        time.sleep(0.05)

    def query(self, c):
        self.sk.sendall((c + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            buf += self.sk.recv(4096)
        return buf.decode().strip()

    def query_num(self, c):
        m = re.search(r"[-+0-9.Ee]+", self.query(c))
        if not m:
            raise ValueError(f"no number in reply to {c}")
        return float(m.group())

    def wave(self, ch):
        """Return (dt, volts) for channel 'C1'..'C4'."""
        self.sk.sendall(f"{ch}:WF? DAT2\n".encode())
        buf = b""
        # binary block: ...#9<9-digit len><payload>\n\n
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
        vdiv = self.query_num(f"{ch}:VDIV?")
        ofst = self.query_num(f"{ch}:OFST?")
        sara = self.query_num("SARA?")
        return 1.0 / sara, raw.astype(np.float64) * vdiv / 25.0 - ofst


TDIV_1_2_5 = [1e-9 * m * s for m in (1, 10, 100, 1000, 10**4, 10**5, 10**6, 10**7, 10**8)
              for s in (1, 2, 5)]
VDIV_1_2_5 = [1e-3 * m * s for m in (1, 10, 100, 1000, 10**4) for s in (1, 2, 5)]


def si(v):
    for scale, suf in ((1, "S"), (1e-3, "MS"), (1e-6, "US"), (1e-9, "NS")):
        if v >= scale:
            return f"{v / scale:g}{suf}"
    return f"{v * 1e9:g}NS"


def lockin(t, y, f):
    w = np.hanning(len(t))
    return np.sum(w * (y - y.mean()) * np.exp(-2j * np.pi * f * t))


def refine_f0(t, y, f_nom):
    g = (np.sqrt(5) - 1) / 2
    a, b = f_nom * 0.995, f_nom * 1.005
    c, d = b - g * (b - a), a + g * (b - a)
    for _ in range(40):
        if np.abs(lockin(t, y, c)) > np.abs(lockin(t, y, d)):
            b, d = d, c
            c = b - g * (b - a)
        else:
            a, c = c, d
            d = a + g * (b - a)
    return (a + b) / 2


def save_csv(path, t, chans):
    names = list(chans)
    with open(path, "w") as fh:
        fh.write(f"Record Length,Analog:{len(t)},\n")
        fh.write(f"Sample Interval,Analog:{t[1] - t[0]:E},\n")
        fh.write("Vertical Units," + ", ".join(f"{n}:V" for n in names) + ",,\n")
        fh.write("Vertical Scale," + ", ".join("?" for _ in names) + ",,\n")
        fh.write("Vertical Offset," + ", ".join("?" for _ in names) + ",,\n")
        fh.write("Horizontal Units,us,\nHorizontal Scale,?,\n")
        fh.write("Model Number,SDS1104X-U,\nSerial Number,SDSAHBAX5R1440,\n")
        fh.write("Software Version,2.1.1.1.5R5,\n")
        fh.write("Source," + ",".join(names) + "\n")
        fh.write("Second," + ",".join("Value" for _ in names) + "\n")
        cols = np.column_stack([t] + [chans[n] for n in names])
        np.savetxt(fh, cols, delimiter=",", fmt="%.6E")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rsense", type=float, required=True)
    ap.add_argument("--scope", default="10.42.0.29")
    ap.add_argument("--gen", default="/dev/ttyUSB0")
    ap.add_argument("--vpp", type=float, default=5.0)
    ap.add_argument("--freqs",
                    default="1e3,2e3,5e3,1e4,2e4,5e4,1e5,2e5,5e5,1e6")
    ap.add_argument("--out", default="data/run")
    ap.add_argument("--smoke", action="store_true",
                    help="single point at the first frequency, report amplitudes only")
    args = ap.parse_args()
    freqs = [float(x) for x in args.freqs.split(",")]
    if args.smoke:
        freqs = freqs[:1]
    os.makedirs(args.out, exist_ok=True)

    gen = JDS6600(args.gen)
    scope = SDS1104XU(args.scope)
    print("scope:", scope.query("*IDN?"))
    gen.setup_sine(args.vpp)

    for ch, attn in (("C1", 10), ("C2", 10), ("C3", 10), ("C4", 1)):
        scope.cmd(f"{ch}:TRA ON")
        scope.cmd(f"{ch}:ATTN {attn}")
        scope.cmd(f"{ch}:CPL D1M")
        scope.cmd(f"{ch}:OFST 0V")
    scope.cmd("TRSE EDGE,SR,C4,HT,OFF")
    scope.cmd("C4:TRLV 0V")
    scope.cmd("TRMD AUTO")
    scope.cmd("ACQW SAMPLING")

    rows = []
    for f in freqs:
        gen.set_freq(f)
        tdiv = next(td for td in TDIV_1_2_5 if 14 * td * f >= 8)  # >=8 cycles on grid
        scope.cmd(f"TDIV {si(tdiv)}")
        # coarse vertical: open wide, then fit to measured pk-pk
        for ch, v0 in (("C1", 2.0), ("C2", 1.0), ("C3", 1.0), ("C4", 2.0)):
            scope.cmd(f"{ch}:VDIV {v0}V")
        time.sleep(0.4 + 3 * 14 * tdiv)
        for ch in ("C1", "C2", "C3", "C4"):
            try:
                pk = scope.query_num(f"{ch}:PAVA? PKPK")
                if 0 < pk < 1e6:
                    vdiv = next(v for v in VDIV_1_2_5 if v >= pk / 6)
                    scope.cmd(f"{ch}:VDIV {vdiv * 1e3:g}MV")
            except (ValueError, StopIteration):
                pass
        time.sleep(0.4 + 3 * 14 * tdiv)

        scope.cmd("STOP")
        chans = {}
        dt = None
        for ch in ("C1", "C2", "C3", "C4"):
            dt, v = scope.wave(ch)
            chans[f"CH{ch[1]}"] = v
        scope.cmd("TRMD AUTO")
        n = min(len(v) for v in chans.values())
        # block-average decimate: cap ~100k rows per file, keep >=40 samples/cycle
        # so the boxcar's sinc rolloff at f0 stays negligible
        dec = max(1, min(n // 100000, int(1.0 / (dt * f * 40)) or 1))
        n = (n // dec) * dec
        chans = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in chans.items()}
        dt *= dec
        t = np.arange(n // dec) * dt
        save_csv(f"{args.out}/f_{int(f):08d}.csv", t, chans)

        if args.smoke:
            print(f"\nsmoke @ {f:g} Hz  (dt={dt:g}s, n={n}):")
            for k, v in chans.items():
                print(f"  {k}: pkpk={v.max() - v.min():7.3f} V  mean={v.mean():+7.3f} V")
            break

        f0 = refine_f0(t, chans["CH2"], f)
        z1, z2 = lockin(t, chans["CH1"], f0), lockin(t, chans["CH2"], f0)
        z3, z4 = lockin(t, chans["CH3"], f0), lockin(t, chans["CH4"], f0)
        if np.abs(z2) < 1e-12:
            print(f"f={f:g}: no current signal, skipping")
            continue
        Z = args.rsense * (z1 - z2) / z2
        w0 = 2 * np.pi * f0
        L = 1e6 * Z.imag / w0 if Z.imag > 0 else float("nan")
        # wye star point sits at (V_A + V_B)/2 for matched half-windings, so the
        # open phase C EMF (mutual asymmetry) is its deviation from that midpoint
        emf = (z3 - (z1 + z2) / 2) / ((z1 - z2) / 2)   # normalized to half-drive
        rows.append((f0, np.abs(Z), np.degrees(np.angle(Z)), Z.real, L,
                     np.abs(emf), np.degrees(np.angle(emf))))
        print(f"f={f0:9.1f} Hz  |Z|={np.abs(Z):8.3f}  ang={np.degrees(np.angle(Z)):+6.1f} deg  "
              f"R={Z.real:7.3f}  L={L:8.2f} uH  emfC={np.abs(emf):.4f} @ {np.degrees(np.angle(emf)):+.1f} deg")

    gen.output_off()
    if rows and not args.smoke:
        with open(f"{args.out}/summary.csv", "w") as fh:
            fh.write("f_hz,Z_mag,Z_deg,R_ohm,L_uH,emfC_ratio,emfC_deg\n")
            for r in rows:
                fh.write(",".join(f"{x:.6g}" for x in r) + "\n")
        print(f"\nsummary -> {args.out}/summary.csv")


if __name__ == "__main__":
    main()
