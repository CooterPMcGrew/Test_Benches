#!/usr/bin/env python3
"""Aggregate loop iteration summaries: SRF, amplitude linearity, repeatability.

Usage: uv run --with numpy,matplotlib python TOOL_STATOR_LoopStats_RevA.py \
           data/loop1_pairAB figures
"""
import glob
import os
import re
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
BLUE, ORANGE = "#2a78d6", "#eb6834"
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "text.color": INK,
    "axes.edgecolor": "#c3c2b7", "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": "#e1e0d9", "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
})

base, outdir = sys.argv[1], sys.argv[2]
os.makedirs(outdir, exist_ok=True)

iters = []
for p in sorted(glob.glob(os.path.join(base, "iter*", "summary.csv"))):
    m = re.search(r"iter(\d+)_([\d.]+)Vpp", p)
    d = np.genfromtxt(p, delimiter=",", names=True)
    iters.append({"n": int(m.group(1)), "vpp": float(m.group(2)), "d": d})
print(f"{len(iters)} iterations loaded")

def plateau_L(d):
    sel = (d["f_hz"] >= 2e4) & (d["f_hz"] <= 3e5)
    return np.median(d["L_uH"][sel])

def mid_R(d):
    sel = (d["f_hz"] >= 2e4) & (d["f_hz"] <= 1e5)
    return np.median(d["R_ohm"][sel])

def srf(d):
    """First zero crossing of phase, descending, above 500 kHz; NaN if in-band."""
    f, ph = d["f_hz"], d["Z_deg"]
    for i in range(len(f) - 1):
        if f[i] > 5e5 and ph[i] > 0 >= ph[i + 1]:
            return f[i] + (f[i + 1] - f[i]) * ph[i] / (ph[i] - ph[i + 1])
    return float("nan")

for it in iters:
    it["L"], it["R"], it["srf"] = plateau_L(it["d"]), mid_R(it["d"]), srf(it["d"])

L_all = np.array([it["L"] for it in iters])
srf_all = np.array([it["srf"] for it in iters])
print(f"\nplateau L_LL: mean {L_all.mean():.3f} uH, std {L_all.std():.3f} "
      f"({100*L_all.std()/L_all.mean():.2f} %), span {L_all.min():.3f}-{L_all.max():.3f}")
print(f"SRF: mean {np.nanmean(srf_all)/1e6:.3f} MHz, std {np.nanstd(srf_all)/1e3:.1f} kHz, "
      f"n_found {np.sum(~np.isnan(srf_all))}/{len(iters)}")

print("\nby amplitude:")
for v in sorted(set(it["vpp"] for it in iters)):
    grp = [it for it in iters if it["vpp"] == v]
    Ls = np.array([g["L"] for g in grp])
    Rs = np.array([g["R"] for g in grp])
    print(f"  {v:5.1f} Vpp (n={len(grp)}): L = {Ls.mean():.3f} +/- {Ls.std():.3f} uH ; "
          f"R_mid = {Rs.mean():.3f} +/- {Rs.std():.3f} ohm")

# --- extended Bode from the median of all 5 Vpp iterations -------------------
ref = [it for it in iters if it["vpp"] == 5.0]
f = ref[0]["d"]["f_hz"]
Zm = np.median(np.vstack([it["d"]["Z_mag"] for it in ref]), axis=0)
Ph = np.median(np.vstack([it["d"]["Z_deg"] for it in ref]), axis=0)
srf_med = np.nanmedian(srf_all)

fig, (am, ap_) = plt.subplots(2, 1, sharex=True, figsize=(8, 6), dpi=180)
am.loglog(f, Zm, "o-", color=BLUE, lw=1.8, ms=4)
am.axvline(srf_med, color=MUTED, lw=0.9, ls="--")
am.set_ylabel("|Z| (Ω)")
am.set_title(f"Extended impedance, pair A–B, 200 Hz–5 MHz   (SRF ≈ {srf_med/1e6:.2f} MHz)",
             loc="left", fontsize=11, color=INK, fontweight="bold")
ap_.semilogx(f, Ph, "o-", color=ORANGE, lw=1.8, ms=4)
ap_.axvline(srf_med, color=MUTED, lw=0.9, ls="--")
ap_.axhline(0, color=MUTED, lw=0.9)
ap_.set_ylabel("∠Z (deg)")
ap_.set_xlabel("frequency (Hz)")
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_BodeExtended_RevB.png", bbox_inches="tight")
plt.close(fig)

# --- repeatability: plateau L per iteration, marker by amplitude -------------
fig, ax = plt.subplots(figsize=(8, 4), dpi=180)
marks = {2.0: "v", 5.0: "o", 10.0: "s", 20.0: "^"}
for v, mk in marks.items():
    grp = [(it["n"], it["L"]) for it in iters if it["vpp"] == v]
    if grp:
        ax.plot(*zip(*grp), mk, color=BLUE, ms=6, ls="none",
                label=f"{v:g} Vpp", markerfacecolor="none" if v in (2.0, 20.0) else BLUE)
ax.axhline(L_all.mean(), color=MUTED, lw=0.9, ls="--")
ax.set_xlabel("iteration")
ax.set_ylabel("plateau L_LL (µH)")
ax.set_title(f"Repeatability over 3 h, amplitude-cycled   "
             f"(σ = {100*L_all.std()/L_all.mean():.2f} %)",
             loc="left", fontsize=11, color=INK, fontweight="bold")
ax.legend(frameon=False, ncols=4, loc="upper right", labelcolor=INK2)
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_Repeatability_RevB.png", bbox_inches="tight")
plt.close(fig)
print(f"\nfigures -> {outdir}/")
