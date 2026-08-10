#!/usr/bin/env python3
"""Render report figures from a sweep summary.csv into figures/.

Usage: uv run --with numpy,matplotlib python TOOL_STATOR_ReportFigures_RevA.py \
           data/run1_pairAB/summary.csv figures
"""
import sys
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "text.color": INK,
    "axes.edgecolor": "#c3c2b7", "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": "#e1e0d9", "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
})

summary, outdir = sys.argv[1], sys.argv[2]
os.makedirs(outdir, exist_ok=True)
d = np.genfromtxt(summary, delimiter=",", names=True)
f = d["f_hz"]

plateau = (f >= 2e4) & (f <= 3e5)
L_ll = np.median(d["L_uH"][plateau])
R_lf = d["R_ohm"][f <= 2e3].mean()
fc = R_lf / (2 * np.pi * L_ll * 1e-6)

fig, (am, ap_) = plt.subplots(2, 1, sharex=True, figsize=(8, 6), dpi=180)
am.loglog(f, d["Z_mag"], "o-", color=BLUE, lw=1.8, ms=5)
am.set_ylabel("|Z| (Ω)")
am.set_title(f"Terminal-pair impedance A–B   (L_LL ≈ {L_ll:.1f} µH, corner ≈ {fc/1e3:.0f} kHz)",
             loc="left", fontsize=11, color=INK, fontweight="bold")
ap_.semilogx(f, d["Z_deg"], "o-", color=ORANGE, lw=1.8, ms=5)
ap_.axhline(45, color=MUTED, lw=0.9, ls="--")
ap_.text(f[0], 47, "45°", color=MUTED, fontsize=8.5)
ap_.set_ylabel("∠Z (deg)")
ap_.set_xlabel("frequency (Hz)")
ap_.set_ylim(0, 95)
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_BodePairAB_RevA.png", bbox_inches="tight")
plt.close(fig)

fig, (al, ar) = plt.subplots(2, 1, sharex=True, figsize=(8, 6), dpi=180)
al.semilogx(f, d["L_uH"], "o-", color=BLUE, lw=1.8, ms=5)
al.axhline(L_ll, color=MUTED, lw=0.9, ls="--")
al.set_ylabel("apparent L (µH)")
al.set_title("Series L and R vs frequency — plateau is the quoted inductance",
             loc="left", fontsize=11, color=INK, fontweight="bold")
ar.semilogx(f, d["R_ohm"], "o-", color=ORANGE, lw=1.8, ms=5)
ar.set_ylabel("series R (Ω)")
ar.set_xlabel("frequency (Hz)")
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_LandRPairAB_RevA.png", bbox_inches="tight")
plt.close(fig)

fig, ax = plt.subplots(figsize=(8, 3.6), dpi=180)
ax.semilogx(f, 100 * d["emfC_ratio"], "o-", color=AQUA, lw=1.8, ms=5)
ax.set_ylabel("|EMF_C| / (V_drive/2)  (%)")
ax.set_xlabel("frequency (Hz)")
ax.set_title("Open-phase C deviation from star midpoint (mutual asymmetry)",
             loc="left", fontsize=11, color=INK, fontweight="bold")
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_EmfCPairAB_RevA.png", bbox_inches="tight")
plt.close(fig)

print(f"L_LL plateau = {L_ll:.2f} uH ; per-phase (wye, L-M) = {L_ll/2:.2f} uH")
print(f"R low-freq = {R_lf:.3f} ohm ; corner = {fc/1e3:.1f} kHz")
print(f"Q at 300 kHz = {d['Z_mag'][f==3e5][0]*np.sin(np.radians(d['Z_deg'][f==3e5][0]))/d['R_ohm'][f==3e5][0]:.1f}"
      if (f == 3e5).any() else "")
print(f"figures -> {outdir}/")
