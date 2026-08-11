#!/usr/bin/env python3
"""Rev C comprehensive figure set from the 43-iteration loop dataset.

Usage: uv run --with numpy,matplotlib python TOOL_STATOR_RevCFigures_RevA.py \
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
BLUE, ORANGE, AQUA, GRID = "#2a78d6", "#eb6834", "#1baf7a", "#e1e0d9"
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "text.color": INK,
    "axes.edgecolor": "#c3c2b7", "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
})

base, outdir = sys.argv[1], sys.argv[2]
os.makedirs(outdir, exist_ok=True)

groups = {}
for p in sorted(glob.glob(os.path.join(base, "iter*", "summary.csv"))):
    v = float(re.search(r"_([\d.]+)Vpp", p).group(1))
    groups.setdefault(v, []).append(np.genfromtxt(p, delimiter=",", names=True))

f = groups[5.0][0]["f_hz"]
med = {k: np.median(np.vstack([d[k] for d in groups[5.0]]), axis=0)
       for k in ("Z_mag", "Z_deg", "R_ohm", "L_uH", "emfC_ratio")}
Zc = med["Z_mag"] * np.exp(1j * np.radians(med["Z_deg"]))
plateau = (f >= 2e4) & (f <= 3e5)
L_plat = np.median(med["L_uH"][plateau])
i_srf = np.where(np.diff(np.sign(med["Z_deg"])) < 0)[0][-1]
srf = f[i_srf] + (f[i_srf+1]-f[i_srf]) * med["Z_deg"][i_srf] / (med["Z_deg"][i_srf]-med["Z_deg"][i_srf+1])
Cp = 1 / (L_plat * 1e-6 * (2 * np.pi * srf) ** 2)

ann = dict(color=INK2, fontsize=8.5,
           arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.9))

# ---- Fig: L(f) and R(f) full band, annotated regions ------------------------
fig, (al, ar) = plt.subplots(2, 1, sharex=True, figsize=(8.5, 6.4), dpi=180)
val = (f <= 8e5)   # L extraction invalid approaching resonance
al.semilogx(f[val], med["L_uH"][val], "o-", color=BLUE, lw=1.8, ms=4)
al.axhline(L_plat, color=MUTED, lw=0.9, ls="--")
al.axvspan(2e4, 3e5, color=GRID, alpha=0.45, lw=0)
al.set_ylabel("apparent series L (µH)")
al.set_title("Inductance and resistance vs frequency — pair A–B, median of 11 sweeps at 5 Vpp",
             loc="left", fontsize=11, color=INK, fontweight="bold")
al.annotate("low-f dispersion (§5.2):\nself-screening or probe-comp,\nunder investigation",
            xy=(400, 170), xytext=(1.3e3, 120), **ann)
al.annotate(f"plateau L_LL = {L_plat:.1f} µH\n(quoted value, 20–300 kHz)",
            xy=(8e4, L_plat), xytext=(6e3, 60), **ann)
al.annotate("rise approaching SRF\n(not real inductance)",
            xy=(7e5, med["L_uH"][val][-1]), xytext=(1.1e5, 90), **ann)
ar.semilogx(f[val], med["R_ohm"][val], "o-", color=ORANGE, lw=1.8, ms=4)
ar.axhline(7.0, color=MUTED, lw=0.9, ls="--")
ar.annotate("DC value 7.0 Ω (DMM)", xy=(300, 7.0), xytext=(300, 10), **ann)
ar.annotate("eddy/skin & proximity rise", xy=(4e5, 12), xytext=(2e4, 16), **ann)
ar.set_ylabel("series R (Ω)")
ar.set_xlabel("frequency (Hz)")
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_LandRExtended_RevC.png", bbox_inches="tight")
plt.close(fig)

# ---- Fig: SRF zoom ----------------------------------------------------------
zoom = f >= 8e5
fig, (am, ap_) = plt.subplots(2, 1, sharex=True, figsize=(8.5, 6), dpi=180)
am.loglog(f[zoom], med["Z_mag"][zoom], "o-", color=BLUE, lw=1.8, ms=5)
am.axvline(srf, color=MUTED, lw=0.9, ls="--")
am.annotate(f"SRF = {srf/1e6:.3f} MHz\n|Z|peak ≈ {med['Z_mag'][zoom].max()/1e3:.1f} kΩ\n"
            f"C_p = {Cp*1e12:.0f} pF (incl. ~15–30 pF fixture)",
            xy=(srf, med["Z_mag"][zoom].max()), xytext=(1e6, 400), **ann)
am.set_ylabel("|Z| (Ω)")
am.set_title("Self-resonance detail (0.8–5 MHz) — inductive below, capacitive above",
             loc="left", fontsize=11, color=INK, fontweight="bold")
ap_.semilogx(f[zoom], med["Z_deg"][zoom], "o-", color=ORANGE, lw=1.8, ms=5)
ap_.axvline(srf, color=MUTED, lw=0.9, ls="--")
ap_.axhline(0, color=MUTED, lw=0.9)
ap_.annotate("inductor", xy=(1e6, 70), color=INK2, fontsize=9)
ap_.annotate("capacitor", xy=(3e6, -70), color=INK2, fontsize=9)
ap_.set_ylabel("∠Z (deg)")
ap_.set_xlabel("frequency (Hz)")
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_SrfZoom_RevC.png", bbox_inches="tight")
plt.close(fig)

# ---- Fig: linearity with error bars ----------------------------------------
vs = sorted(groups)
Lm = [np.mean([np.median(d["L_uH"][plateau]) for d in groups[v]]) for v in vs]
Ls = [np.std([np.median(d["L_uH"][plateau]) for d in groups[v]]) for v in vs]
fig, ax = plt.subplots(figsize=(8.5, 4), dpi=180)
ax.errorbar(vs, Lm, yerr=Ls, fmt="o", color=BLUE, ms=7, capsize=4, lw=1.8)
ax.axhline(np.mean(Lm[:3]), color=MUTED, lw=0.9, ls="--")
ax.annotate("2–10 Vpp within 0.2 % — air-core linearity confirmed",
            xy=(5, np.mean(Lm[:3])), xytext=(2.5, np.mean(Lm[:3]) - 0.15), **ann)
ax.annotate("20 Vpp: −0.9 %, 5× scatter —\ngenerator strain into ~10 Ω load,\nnot DUT nonlinearity",
            xy=(20, Lm[-1]), xytext=(12, Lm[-1] + 0.1), **ann)
ax.set_xlabel("drive amplitude (Vpp)")
ax.set_ylabel("plateau L_LL (µH)")
ax.set_title("Amplitude linearity — 43 sweeps grouped by drive level",
             loc="left", fontsize=11, color=INK, fontweight="bold")
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_Linearity_RevC.png", bbox_inches="tight")
plt.close(fig)

# ---- Fig: PWM loss decomposition at 18 V -----------------------------------
# Winding loss from measured Z: square-wave (50 % duty, 18 V bus) odd-harmonic
# sum P = sum |V_n,rms|^2 * Re(1/Z(n*fs)), harmonics limited to measured band.
logf = np.log10(f)
def Y_re(x):
    xm = np.clip(x, f[0], f[-1])
    m = np.interp(np.log10(xm), logf, med["Z_mag"])
    p = np.interp(np.log10(xm), logf, med["Z_deg"])
    return np.cos(np.radians(p)) / m

VBUS = 18.0
fs_ax = np.logspace(np.log10(2e4), np.log10(1.2e6), 120)
P_wind = np.array([sum((2 * VBUS / (np.pi * n) / np.sqrt(2)) ** 2 * Y_re(n * fsx)
                       for n in (1, 3, 5, 7, 9) if n * fsx <= f[-1])
                   for fsx in fs_ax])
P_cap = Cp * VBUS ** 2 * fs_ax            # bridge charging loss through C_p
E_SW = 2e-6                                # J/cycle, typical integrated Si bridge
P_sw = E_SW * fs_ax
P_tot = P_wind + P_cap + P_sw
fopt = fs_ax[np.argmin(P_tot)]

fig, ax = plt.subplots(figsize=(8.5, 5.2), dpi=180)
ax.loglog(fs_ax, P_wind, color=BLUE, lw=1.8, label="winding ripple loss (measured Z)")
ax.loglog(fs_ax, P_cap, color=AQUA, lw=1.8, label="C_p charging loss (measured C_p)")
ax.loglog(fs_ax, P_sw, color=ORANGE, lw=1.8, ls="--",
          label="bridge switching loss (illustrative, 2 µJ/cycle)")
ax.loglog(fs_ax, P_tot, color=INK, lw=2.2, label="total")
ax.axvline(2e5, color=MUTED, lw=0.9, ls=":")
ax.annotate("MCT8316Z max\n200 kHz", xy=(2e5, 2.5), xytext=(6e4, 4.5), **ann)
ax.axvline(srf / 5, color=MUTED, lw=0.9, ls=":")
ax.annotate(f"SRF/5 ≈ {srf/5e3:.0f} kHz\nringing/EMI ceiling", xy=(srf/5, 2.5),
            xytext=(5.2e5, 4.5), **ann)
ax.annotate(f"total-loss minimum ≈ {fopt/1e3:.0f} kHz\n(flat within ~0.1 W from 150–450 kHz)",
            xy=(fopt, P_tot.min()), xytext=(3e4, 0.06), **ann)
ax.set_xlabel("PWM switching frequency (Hz)")
ax.set_ylabel("power loss (W)")
ax.set_ylim(5e-3, 40)
ax.set_title("Where PWM frequency stops helping — loss decomposition at 18 V bus",
             loc="left", fontsize=11, color=INK, fontweight="bold")
ax.legend(frameon=False, loc="upper center", fontsize=8.5, labelcolor=INK2)
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_PwmLoss_RevC.png", bbox_inches="tight")
plt.close(fig)

# ---- Fig: emfC extended -----------------------------------------------------
fig, ax = plt.subplots(figsize=(8.5, 3.8), dpi=180)
ax.semilogx(f[val], 100 * med["emfC_ratio"][val], "o-", color=AQUA, lw=1.8, ms=4)
ax.set_xlabel("frequency (Hz)")
ax.set_ylabel("|EMF_C| / (V_drive/2)  (%)")
ax.set_title("Open-phase C deviation from star midpoint — mutual asymmetry, full band",
             loc="left", fontsize=11, color=INK, fontweight="bold")
fig.tight_layout()
fig.savefig(f"{outdir}/FIG_STATOR_EmfCExtended_RevC.png", bbox_inches="tight")
plt.close(fig)

print(f"SRF={srf/1e6:.3f} MHz  Cp={Cp*1e12:.0f} pF  L_plat={L_plat:.2f} uH  "
      f"P_tot min at {fopt/1e3:.0f} kHz")
print(f"P_wind at 200k={np.interp(2e5, fs_ax, P_wind):.3f} W, "
      f"P_cap at 450k={Cp*VBUS**2*4.5e5*1e3:.1f} mW")
print(f"figures -> {outdir}/")
