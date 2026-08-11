#!/usr/bin/env python3
"""Back-EMF spin-test capture. Drive lead must be disconnected from terminal A;
reference is terminal B via Rsense to ground (no current -> B at ground).
CH1 = V_AB, CH3 = V_CB.

Usage: uv run --with numpy,pyserial python TOOL_STATOR_SpinCapture_RevA.py \
           [--tdiv 0.02] [--out data/spin1] [--rpm N] [--poles N]
"""
import argparse
import os
import numpy as np
from TOOL_STATOR_SweepOrchestrator_RevA import SDS1104XU, save_csv, lockin, refine_f0

ap = argparse.ArgumentParser()
ap.add_argument("--scope", default="10.42.0.29")
ap.add_argument("--tdiv", type=float, default=0.02, help="s/div; 14 div record")
ap.add_argument("--out", default="data/spin1")
ap.add_argument("--rpm", type=float, default=None, help="mechanical RPM if known")
ap.add_argument("--poles", type=int, default=None, help="magnet count if known")
args = ap.parse_args()
os.makedirs(args.out, exist_ok=True)

scope = SDS1104XU(args.scope)
print("scope:", scope.query("*IDN?"))
for ch in ("C1", "C3"):
    scope.cmd(f"{ch}:TRA ON")
    scope.cmd(f"{ch}:ATTN 10")
    scope.cmd(f"{ch}:CPL D1M")
    scope.cmd(f"{ch}:OFST 0V")
    scope.cmd(f"{ch}:VDIV 2V")
scope.cmd("TRMD AUTO")

def si(v):
    return f"{v*1e3:g}MS" if v < 1 else f"{v:g}S"

scope.cmd(f"TDIV {si(args.tdiv)}")
import time
time.sleep(0.5 + 3 * 14 * args.tdiv)
for ch in ("C1", "C3"):
    try:
        pk = scope.query_num(f"{ch}:PAVA? PKPK")
        if 0 < pk < 1e6:
            for v in (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10):
                if v >= pk / 6:
                    scope.cmd(f"{ch}:VDIV {v*1e3:g}MV")
                    break
    except ValueError:
        pass
time.sleep(0.5 + 3 * 14 * args.tdiv)
scope.cmd("STOP")
chans = {}
for ch in ("C1", "C3"):
    dt, v = scope.wave(ch)
    chans[f"CH{ch[1]}"] = v
scope.cmd("TRMD AUTO")
n = min(len(v) for v in chans.values())
dec = max(1, n // 200000)
n = (n // dec) * dec
chans = {k: v[:n].reshape(-1, dec).mean(axis=1) for k, v in chans.items()}
t = np.arange(n // dec) * dt * dec
save_csv(f"{args.out}/spin_capture.csv", t, chans)

y1, y3 = chans["CH1"], chans["CH3"]
print(f"\nCH1 (V_AB): pkpk {y1.max()-y1.min():.3f} V   CH3 (V_CB): pkpk {y3.max()-y3.min():.3f} V")
if y1.max() - y1.min() < 0.05:
    print("no significant signal — was it spinning during the capture window?")
    raise SystemExit(1)

# electrical frequency from FFT peak + refinement
spec = np.abs(np.fft.rfft((y1 - y1.mean()) * np.hanning(len(y1))))
fr = np.fft.rfftfreq(len(y1), t[1] - t[0])
f_nom = fr[np.argmax(spec[1:]) + 1]
f_e = refine_f0(t, y1, f_nom)
z1, z3 = lockin(t, y1, f_e), lockin(t, y3, f_e)
w = np.sum(np.hanning(len(t)))
V1, V3 = 2 * abs(z1) / w, 2 * abs(z3) / w
dphi = np.degrees(np.angle(z3 / z1))
# harmonic content of V_AB
thd = np.sqrt(sum((2 * abs(lockin(t, y1, k * f_e)) / w) ** 2 for k in (2, 3, 5, 7))) / V1
print(f"f_elec = {f_e:.2f} Hz   V_AB = {V1:.3f} Vpk   V_CB = {V3:.3f} Vpk   "
      f"phase(CB-AB) = {dphi:+.1f} deg   THD(2,3,5,7) = {100*thd:.1f} %")
lam = V1 / (2 * np.pi * f_e)
print(f"line-line flux linkage = {lam*1e3:.3f} mV.s/rad (electrical)")
if args.rpm:
    f_mech = args.rpm / 60
    pp = f_e / f_mech
    print(f"RPM {args.rpm:g} -> pole pairs = {pp:.2f} (magnets = {2*pp:.1f})")
    print(f"Ke line-line = {V1/(args.rpm*2*np.pi/60):.4f} V.s/rad mech "
          f"= {V1/(args.rpm/1000):.2f} V per kRPM (peak)")
if args.poles:
    pp = args.poles / 2
    f_mech = f_e / pp
    print(f"poles {args.poles} -> mech speed = {f_mech*60:.0f} RPM")
    print(f"Ke line-line = {V1/(2*np.pi*f_mech):.4f} V.s/rad mech")
