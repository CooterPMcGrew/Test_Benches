# TR_STATOR_PcbStatorCharacterization_RevA

**Test report — electrical characterization of spiral annular PCB stator**

| Field | Value |
|---|---|
| Document | TR_STATOR_PcbStatorCharacterization_RevA |
| Date | 2026-08-10Z |
| Status | DRAFT — long-run loop in progress; §7 pending |
| DUT | 3-phase spiral annular PCB stator, wye-connected, 3 terminals (A/B/C) |
| DUT configuration | Bare stator board only — no rotor, magnets, or back-iron. Resting on wood (non-conductive) |
| Operator | C. McGrew; automated bench control by MSI test-bench tooling |

## 1. Objective

Full passive electrical characterization of the PCB stator: terminal-pair
impedance Z(ω), series inductance and resistance vs frequency, drive-amplitude
linearity, repeatability/drift over hours, mutual-coupling asymmetry of the
open phase, and location of the self-resonant frequency (SRF).

## 2. Instruments

| Instrument | Model | Serial | Interface |
|---|---|---|---|
| Oscilloscope | Siglent SDS1104X-U, fw 2.1.1.1.5R5 | SDSAHBAX5R1440 | SCPI raw socket, LAN 10.42.0.29 |
| Signal generator | JDS6600 (15 MHz variant) | 2366105039 | vendor serial protocol, /dev/ttyUSB0 |
| Sense resistor | metal film, **42.68 Ω** (DMM 4-wire-equivalent hand measurement) | — | — |

Scope vertical system is 8-bit; quantization is mitigated by sine excitation
and lock-in extraction (§3), not by averaging.

## 3. Method

Series V-I measurement with low-side current sense, all grounds common:

```
gen ── T ─────────────────────► terminal A     CH1 tip: terminal A (10x)
      └─ coax ► scope CH4 (1x)                 CH2 tip: B–Rsense junction (10x)
terminal B ──[ Rsense 42.68 Ω ]── gen return   CH3 tip: terminal C, open (10x)
                                               all probe grounds: Rsense gnd end
```

Per frequency point, automated (TOOL_STATOR_SweepOrchestrator_RevA.py):
generator set to sine at f; scope timebase set for ≥8 cycles; per-channel
vertical autoscale; single acquisition of all four channels; Hann-weighted
lock-in extraction of each channel's complex phasor at f₀ (f₀ refined by
maximizing the current-channel lock-in magnitude). Impedance from the phasor
ratio, in which the window scalar cancels exactly:

> Z_pair = Rs · (V₁ − V₂) / V₂

Derived per point: R = Re Z, L = Im Z / ω, and the open-phase asymmetry EMF
normalized to half the drive: emf_C = (V₃ − (V₁+V₂)/2) / ((V₁−V₂)/2). For a
wye winding with matched half-windings the star point sits at (V₁+V₂)/2, so
emf_C measures phase-C mutual/geometric asymmetry, not gross coupling.

**Method validation:** the extraction chain was verified against synthetic
Siglent-format captures of a known DUT (R = 7.000 Ω, L = 2000 µH) including
8-bit quantization and noise: recovery within 0.1 % (R) and 0.3 % (L) across
100 Hz–100 kHz (see repo history, `TOOL_STATOR_ImpedanceSweep_RevA.py`).

**Wye conversion:** a terminal pair measures L_LL = 2·(L_phase − M). Values
quoted "per phase" are L_LL/2 = L_phase − M — the commutation inductance seen
by an inverter leg. Separating L_phase from M requires the §7 multi-pair data.

## 4. DC baseline

Phase-to-phase DC resistance (DMM): **≈ 7.0 Ω** on each pair (≈ 3.5 Ω per
winding), consistent with thin-copper spiral traces. Equal pair readings are
consistent with a balanced wye; wye topology was confirmed electrically: the
open terminal C rides at the (V₁+V₂)/2 star midpoint within ~7 % (§5.3).

## 5. Results — run 1, pair A–B, 5 Vpp drive, 15 points, 1 kHz–1 MHz

Raw data: `data/run1_pairAB/` (per-point waveform CSVs + `summary.csv`).

### 5.1 Headline values

| Quantity | Value | Basis |
|---|---|---|
| L_LL (pair A–B) | **37.3 µH** | median of 20 kHz–300 kHz plateau (spread < 0.5 %) |
| L per phase (L−M, wye) | **18.7 µH** | L_LL / 2 |
| R_LL low-frequency | 7.6–8.3 Ω (1–2 kHz) | Re Z; DC value 7.0 Ω |
| R_LL mid-band | ≈ 9.1 Ω (20–100 kHz) | Re Z |
| R_LL at 1 MHz | 26 Ω | Re Z (skin/proximity/eddy) |
| Corner (∠Z = 45°) | ≈ 39 kHz observed | matches R/2πL = 34–39 kHz |
| Q at 300 kHz | ≈ 6.6 | ωL/R |
| SRF | > 1 MHz | phase still +85° at 1 MHz; located by §7 extended sweep |

![Bode](figures/FIG_STATOR_BodePairAB_RevA.png)
![L and R](figures/FIG_STATOR_LandRPairAB_RevA.png)

### 5.2 Low-frequency dispersion — eddy screening

Apparent series L falls from ≈ 156 µH (1 kHz) to the 37.3 µH plateau by
≈ 20 kHz while series R rises above its DC value. External-conductor eddy
screening is excluded: no rotor mounted, board resting on wood. Remaining
candidates, both matching the frequency signature (effect peaking at few
kHz, gone above ~20 kHz):

1. **Self-screening by the board's own copper.** Spiral traces are
   mm-scale wide; copper skin depth passes through that dimension across
   1–20 kHz (δ ≈ 2.1 mm at 1 kHz, 0.46 mm at 20 kHz). Eddy currents
   circulating within the trace widths — on all layers, including the
   open phase — progressively expel flux from the copper: L falls, and
   the reflected loss raises R (consistent with R: 7.0 Ω DC → 9.1 Ω
   mid-band). A known effect in PCB machines with wide traces; the
   mitigation in fabrication is multiple narrow paralleled traces.
2. **Probe compensation mismatch (CH1 vs CH2)** — a miscompensated 10×
   probe introduces phase errors of a few degrees peaked in the same
   band, and the extraction is phase-limited at low f where ∠Z is small
   (excess phase observed: 5.5° at 1 kHz).

Discriminating tests (post-loop, minutes): verify both probe compensations
on the scope cal output; re-measure with a known pure resistor as DUT (a
comp artifact reproduces on a resistor, real self-screening does not);
swap CH1/CH2 probes (an artifact moves/flips, physics stays). Either way
the design-relevant value for PWM-rate ripple is the **plateau L**, which
is insensitive to both mechanisms.

### 5.3 Open-phase asymmetry

![emfC](figures/FIG_STATOR_EmfCPairAB_RevA.png)

|emf_C| is 6–7 % of half-drive at low frequency, decaying above the corner.
A perfectly symmetric wye with identical mutuals M_AC = M_BC would null this
quantity; the observed asymmetry is small and stable. Single rotor position;
angle dependence not yet measured.

## 6. Uncertainty

Dominant terms, quoted for the plateau L_LL:

- Rsense value: DMM accuracy on 42.68 Ω, est. ±0.5 % → ±0.5 % on |Z| and L.
- Probe/channel gain mismatch (CH1 vs CH2, 10× probes, compensation state
  not formally verified): est. ±2 % on |Z|; largely cancels in ∠Z.
- Extraction: < 0.3 % (validated, §3).
- Quantization/noise at plateau signal levels: < 0.5 % after lock-in.

Combined (RSS): **≈ ±2.2 % on L_LL** → L_LL (A–B) = **37.3 ± 0.9 µH**.
R values below the corner carry the same scale factors; R above ∠Z ≈ 85°
(≥ 700 kHz) is ill-conditioned and indicative only.

## 7. Long-run comprehensive loop — PENDING

In progress at time of Rev A: repeated sweeps, 200 Hz–5 MHz, ~10 pts/decade
(~44 points), drive amplitude cycled 5 / 2 / 10 / 20 Vpp per iteration,
≥ 3 h wall clock, each iteration in a UTC-stamped folder under
`data/loop1_pairAB/`. Will populate: amplitude linearity, repeatability and
thermal drift statistics, R(f) fine structure, SRF location. Rev B will fold
these in, plus pairs B–C and C–A for per-phase closure.

## 8. Findings & recommendations

1. **F-1:** Pair A–B behaves as a clean series R-L with L_LL = 37.3 µH
   (plateau) and no SRF below 1 MHz. Suitable frequency range for inverter
   ripple analysis: use plateau L.
2. **F-2:** Strong low-frequency L dispersion; external conductors excluded
   (bare board on wood). Leading candidate: self-screening by the board's
   own wide copper traces; alternative: probe-compensation phase error
   (§5.2). Action: probe-comp check, known-resistor DUT, probe swap.
   Plateau values unaffected. If self-screening is confirmed it is a real
   DUT property worth noting for any low-frequency (sub-corner) use.
3. **F-3:** Wye topology confirmed electrically; phase-C asymmetry ≤ 7 %.
   Action: repeat at several locked rotor angles to map saliency.
4. **N-1 (process):** Incident during initial manual capture (USB file
   SDS00004.csv): both scope probes were on generator outputs (4 kHz and
   10 kHz on the JDS6600's two channels), yielding no DUT response.
   Detected by frequency fingerprint and −70 dB cross-lock-in. Mitigation
   now automated: every sweep point verifies the sense-channel frequency
   equals the drive frequency by construction (lock-in at f₀ from CH2).

## 9. Data & tooling index

| Path | Content |
|---|---|
| `TOOL_STATOR_SweepOrchestrator_RevA.py` | single sweep: gen + scope + lock-in |
| `TOOL_STATOR_SweepLoop_RevA.py` | multi-hour loop, amplitude cycle |
| `TOOL_STATOR_ImpedanceSweep_RevA.py` | offline analysis of saved scope CSVs |
| `TOOL_STATOR_ReportFigures_RevA.py` | figures in `figures/` from summary.csv |
| `data/run1_pairAB/summary.csv` | run-1 per-point table (committed) |
| `data/loop1_pairAB/` | loop output (waveform CSVs local-only, summaries committed) |
