# Stator — spiral annular PCB stator impedance bench

DUT: axial-flux style PCB stator, spiral annular windings, wye-connected.
Measured phase-to-phase DC resistance: ~7 Ω (≈3.5 Ω per winding).
Air-core → expect per-phase inductance in the **µH range**, so the inductive
region of Z(ω) only opens up above roughly 50–100 kHz. Sweep high.

## Wiring (low-side current sense, all grounds common)

```
gen hot ──────────────── terminal A
terminal B ── Rsense ─── gen return / scope gnd     Rsense ≈ 10 Ω, DMM-measured
CH1: terminal A          (V across DUT + Rsense)
CH2: top of Rsense       (I = V / Rsense)
CH3: terminal C          (optional — third-phase voltage, rotor-angle sensitivity)
```

## Sweep protocol

- Points (1-2-5 decades): 1 k, 2 k, 5 k, 10 k, 20 k, 50 k, 100 k, 200 k,
  500 k, 1 MHz. Add points where ∠Z crosses ~45° — that corner
  (f = R/2πL) is where L is best conditioned.
- Sine, no DC offset. Amplitude: max the gen allows; expect heavy sag into
  the ~7 Ω load (50 Ω source) — the tool measures at the winding, so sag is
  harmless, but more current = better SNR.
- Timebase: ~10 cycles per capture. Both channels scaled to fill the screen.
  Acquire → Average (16) — the scope is 8-bit; averaging buys the phase
  accuracy the L extraction needs below the corner.
- Rotor: if magnets/back-iron are anywhere near the board, they load the
  measurement (eddy currents) and make L rotor-position-dependent. Measure
  bare-board first, then with rotor at a few locked angles.
- Save one CSV per frequency (any names — drive frequency is auto-detected
  from the current channel).

## Analysis

```
uv run --with numpy,matplotlib python TOOL_STATOR_ImpedanceSweep_RevA.py \
    --rsense <measured value> [--sense CH3] --out sweep SDS000*.csv
```

Outputs per-point table (f, |Z|, ∠Z, R, L), `sweep_table.csv`, Bode plot,
and L(f) plot. Validated against synthetic captures (R = 7 Ω, L = 2 mH,
8-bit quantization): recovers R to 0.1 %, L to 0.3 % across the band.

## Running the bench yourself

One-time / after replug:

```sh
# serial access to the JDS6600 (lost when the USB is replugged):
sudo setfacl -m u:$USER:rw /dev/ttyUSB0
# permanent alternative (takes effect next login): sudo usermod -aG dialout $USER
```

The scope link needs no action: the "Wired connection 3" NetworkManager
profile is saved in shared mode, so plugging the scope's Ethernet into the
USB-C NIC brings up 10.42.0.1 automatically; the scope sits at 10.42.0.29
(SCPI raw socket, port 5025).

Single sweep (one terminal pair, wired per the diagram above):

```sh
cd Stator
uv run --with numpy,pyserial python TOOL_STATOR_SweepOrchestrator_RevA.py \
    --rsense 42.68 --out data/myrun --freqs 1e3,2e3,5e3,1e4,2e4,5e4,1e5,2e5,5e5,1e6
```

Add `--smoke` for a single-point wiring check (prints per-channel pk-pk).

Multi-hour characterization loop (dense grid 200 Hz–5 MHz, amplitude
cycled 5/2/10/20 Vpp, UTC-stamped subfolders, survives terminal close):

```sh
cd Stator
setsid nohup uv run --with numpy,pyserial python TOOL_STATOR_SweepLoop_RevA.py \
    --rsense 42.68 --hours 3 --base data/myloop > data/myloop_console.txt 2>&1 &
tail -f data/myloop/loop_log.txt     # watch progress; Ctrl-C stops the tail only
```

Figures from any run's summary:

```sh
uv run --with numpy,matplotlib python TOOL_STATOR_ReportFigures_RevA.py \
    data/myrun/summary.csv figures
```

Rules of thumb: never run two things against the scope at once; check
`data/*/loop_log.txt` for a live loop before starting anything; re-measure
Rsense with the DMM if you swap it, and pass the measured value.

## Interpretation caveats

- Terminal-pair inductance of a wye pair is L_LL = 2(L_phase − M). The
  printed "per-phase if wye" value is L_LL/2 = L_phase − M, i.e. the
  commutation inductance an inverter leg actually sees. Separating L and M
  needs the CH3 third-terminal measurement or a neutral-point tap.
- Expect R(f) to rise at high frequency (skin/proximity effect in PCB
  traces) — real, not an artifact. The R column is ill-conditioned above
  ∠Z ≈ 85°; trust it only below the corner.
- Watch for the self-resonance (∠Z rolling back toward 0° then negative,
  |Z| peaking) — likely single-digit MHz for a planar spiral. L values
  within a decade below SRF are inflated; quote L from the flat region.
