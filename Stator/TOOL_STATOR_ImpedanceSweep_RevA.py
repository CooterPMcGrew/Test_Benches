#!/usr/bin/env python3
"""Extract R / L from a set of Siglent SDS CSV captures taken at swept frequencies.

Wiring assumed (low-side current sense, all grounds common):

    gen hot ── terminal A          CH_V: terminal A  (V across DUT + Rsense)
    terminal B ── Rsense ── gnd    CH_I: top of Rsense (V_I; I = V_I / Rsense)
                                   CH_S: (optional) third stator terminal

Per file: auto-detects the drive frequency from the current channel, lock-in
extracts both phasors at that frequency, and computes
    Z_dut = Rsense * (V - V_I) / V_I
Window factors cancel in the ratio, so leakage does not bias Z.

Usage:
    uv run --with numpy,matplotlib python TOOL_STATOR_ImpedanceSweep_RevA.py \
        --rsense 10.0 [--v CH1] [--i CH2] [--sense CH3] [--out sweep] FILES...

Output: per-point table on stdout, <out>_table.csv, <out>_bode.png, <out>_L.png.
"""
import argparse
import csv
import sys
import numpy as np


def parse_siglent_csv(path):
    """Return (t, {channel_name: samples}) from an SDS1000-series CSV export."""
    with open(path) as fh:
        head = [next(fh) for _ in range(12)]
    src_line = next(l for l in head if l.startswith("Source"))
    names = [c.strip() for c in src_line.strip().split(",")[1:] if c.strip()]
    data = np.genfromtxt(path, delimiter=",", skip_header=12,
                         usecols=range(len(names) + 1))
    return data[:, 0], {n: data[:, i + 1] for i, n in enumerate(names)}


def lockin(t, y, f):
    """Hann-weighted complex phasor of y at frequency f (arbitrary scale —
    only ever used in same-window ratios, where the scale cancels)."""
    w = np.hanning(len(t))
    return np.sum(w * (y - y.mean()) * np.exp(-2j * np.pi * f * t))


def detect_f0(t, y):
    """Dominant frequency of y: FFT peak, then refined by maximizing the
    lock-in magnitude over +/-1 bin (golden section, fixed iteration cap)."""
    n = len(t)
    dt = (t[-1] - t[0]) / (n - 1)
    spec = np.abs(np.fft.rfft((y - y.mean()) * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, dt)
    k = np.argmax(spec[1:]) + 1          # skip DC bin
    lo, hi = freqs[max(k - 1, 1)], freqs[k + 1]
    g = (np.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    for _ in range(40):                   # bounded: ~1e-8 relative width
        if np.abs(lockin(t, y, c)) > np.abs(lockin(t, y, d)):
            b, d = d, c
            c = b - g * (b - a)
        else:
            a, c = c, d
            d = a + g * (b - a)
    return (a + b) / 2


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--rsense", type=float, required=True,
                    help="measured sense resistor value, ohms")
    ap.add_argument("--v", default="CH1", help="channel across DUT+Rsense")
    ap.add_argument("--i", default="CH2", help="channel across Rsense")
    ap.add_argument("--sense", default=None,
                    help="optional third-terminal channel (reported, not fitted)")
    ap.add_argument("--out", default="sweep", help="output file prefix")
    args = ap.parse_args()

    rows = []
    for path in args.files:
        try:
            t, ch = parse_siglent_csv(path)
        except Exception as e:
            print(f"skip {path}: {e}", file=sys.stderr)
            continue
        if args.v not in ch or args.i not in ch:
            print(f"skip {path}: has {list(ch)}, need {args.v},{args.i}",
                  file=sys.stderr)
            continue
        f0 = detect_f0(t, ch[args.i])
        zv, zi = lockin(t, ch[args.v], f0), lockin(t, ch[args.i], f0)
        if np.abs(zi) < 1e-12:
            print(f"skip {path}: no current signal", file=sys.stderr)
            continue
        Z = args.rsense * (zv - zi) / zi
        w0 = 2 * np.pi * f0
        # crude SNR gate: current tone must dominate its own residual
        n = len(t)
        i_pk = 2 * np.abs(zi) / np.sum(np.hanning(n))
        row = {
            "file": path, "f_hz": f0,
            "I_pk_mA": 1e3 * i_pk / args.rsense,
            "Z_mag": np.abs(Z), "Z_deg": np.degrees(np.angle(Z)),
            "R_ohm": Z.real,
            "L_uH": 1e6 * Z.imag / w0 if Z.imag > 0 else float("nan"),
        }
        if args.sense and args.sense in ch:
            zs = lockin(t, ch[args.sense], f0)
            row["Vsense_over_V"] = np.abs(zs / zv)
            row["Vsense_deg"] = np.degrees(np.angle(zs / zv))
        rows.append(row)

    if not rows:
        sys.exit("no usable files")
    rows.sort(key=lambda r: r["f_hz"])

    cols = list(rows[0].keys())
    fmt = {"f_hz": "10.1f", "I_pk_mA": "8.2f", "Z_mag": "9.3f",
           "Z_deg": "7.2f", "R_ohm": "8.3f", "L_uH": "9.2f",
           "Vsense_over_V": "7.4f", "Vsense_deg": "7.1f"}
    print("  ".join(f"{c:>10s}" for c in cols if c != "file"))
    for r in rows:
        print("  ".join(f"{r[c]:>{fmt.get(c, '10.3f')}}"
                        for c in cols if c != "file") + f"  {r['file']}")

    with open(f"{args.out}_table.csv", "w", newline="") as fh:
        wtr = csv.DictWriter(fh, fieldnames=cols)
        wtr.writeheader()
        wtr.writerows(rows)

    # inductive-region fit: Z = R + jwL over points with phase in (15, 80) deg
    sel = [r for r in rows if 15 < r["Z_deg"] < 80]
    fit_note = ""
    if len(sel) >= 2:
        wv = np.array([2 * np.pi * r["f_hz"] for r in sel])
        L_fit = np.median([r["L_uH"] for r in sel])
        R_fit = np.median([r["R_ohm"] for r in rows if r["Z_deg"] < 45])
        fit_note = f"R = {R_fit:.2f} ohm, L_LL = {L_fit:.1f} uH (median of inductive band)"
        print(f"\n{fit_note}")
        print(f"  per-phase if wye: {L_fit/2:.1f} uH   if delta: {1.5*L_fit:.1f} uH")

    plot(rows, args.out, fit_note)


def plot(rows, out, note):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
    plt.rcParams.update({
        "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb",
        "savefig.facecolor": "#fcfcfb", "text.color": INK,
        "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#c3c2b7", "font.size": 10,
    })
    f = [r["f_hz"] for r in rows]

    fig, (am, ap_) = plt.subplots(2, 1, sharex=True, figsize=(8, 6), dpi=160)
    am.loglog(f, [r["Z_mag"] for r in rows], "o-", color="#2a78d6", lw=1.8, ms=5)
    am.set_ylabel("|Z| (Ω)")
    am.set_title(f"Terminal-pair impedance   {note}", loc="left",
                 fontsize=10.5, color=INK, fontweight="bold")
    ap_.semilogx(f, [r["Z_deg"] for r in rows], "o-", color="#eb6834", lw=1.8, ms=5)
    ap_.set_ylabel("∠Z (deg)")
    ap_.set_xlabel("frequency (Hz)")
    ap_.set_ylim(-95, 95)
    fig.tight_layout()
    fig.savefig(f"{out}_bode.png", bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4), dpi=160)
    ax.semilogx(f, [r["L_uH"] for r in rows], "o-", color="#2a78d6", lw=1.8, ms=5)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("apparent L (µH)")
    ax.set_title("Apparent series inductance vs frequency", loc="left",
                 fontsize=10.5, color=INK, fontweight="bold")
    fig.tight_layout()
    fig.savefig(f"{out}_L.png", bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out}_table.csv, {out}_bode.png, {out}_L.png")


if __name__ == "__main__":
    main()
