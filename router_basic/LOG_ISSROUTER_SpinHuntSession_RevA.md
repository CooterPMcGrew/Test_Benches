# LOG_ISSROUTER_SpinHuntSession_RevA

Halbach spin-attempt session, 2026-08-17 ~20:30–21:45Z. Companion to
`TN_ISSROUTER_BringupPinout_RevA.md` (wiring/config ground truth).

## Timeline of findings

| Time (Z) | Event |
|---|---|
| 20:56 | First spin attempt (duty 128): MTR_LOCK ~1 s after start. IC_STAT 0x81. Campaign safed motor. |
| ~21:00 | Hunter v1 (FG criterion): 12 rounds, duties 128–384. Coil bursts to 860 mV rms prove phases drive and rotor twitches; FG never ticks. |
| ~21:10 | Hunter v2 (coil-activity criterion, progressive ramp): 16 ladders. Same signature every attempt. |
| ~21:2x | Hand-spin bursts: coil peaks 2.39 V with FG = 0.0 throughout → hall path definitively dead at driver. |
| ~21:3x | **FG ticked 3.8 Hz during one burst** — hall/FG path is INTERMITTENT, not severed. Contact was live at whatever was being touched at that moment. |
| ~21:3x | Coil 5 sense (ISNS_4/AIN5) went permanently quiet (~1 mV) — died during evening bench work. Coil 4 (ISNS_3/AIN3) dead all day. |
| 21:4x | Hunter stopped, motor parked (duty 0), CLR_FLT sent. nFAULT LED relights: IC_STAT 0x81 (MTR_LOCK) relatching; STAT1 0x00 (no OCP/thermal), STAT2 0x80 (benign, present when healthy). |

## Fault-register captures

Healthy idle (motor power on, pre-attempt): 0x00=0x00, 0x01=0x00, 0x02=0x80,
control 0x04=0x60 0x05=0x46 0x06=0x10 (non-zero POR defaults — SPI proven).
Faulted: 0x00=0x81 (FAULT+MTR_LOCK), 0x01=0x00, 0x02=0x80.

## Open hardware items (ranked)

1. **Hall path intermittent** — contact briefly live at ~21:3x (FG 3.8 Hz).
   Fix at the spot being handled at that moment; verify by hand-spin with FG
   steady in the stream header. Motor cannot run until then (MCT8316Z is
   sensored-only). Fallback: MCF8316 board (sensorless) spins this motor
   with halls disconnected.
2. **Persistent nFAULT LED** — MTR_LOCK relatches with no known speed
   command; board has no speed pot (ILIM pot only). Suspect SPEED input
   floating (GPIO21 wire not on the actual SPEED pin, or broken). Check:
   scope module SPEED pin; must show 25 kHz PWM when duty commanded.
3. **Coil 4 sense dead** (all day) and **coil 5 sense dead** (since evening
   work) — ohm the coils / reseat taps at those sectors.

## Data index (correct titles)

| Path | Content |
|---|---|
| `data/AUTOTEST_20260817T190438Z/` | Phase 1–7 bring-up campaign: baseline noise, sine fidelity, linearity, 100 Hz scope cross-check, NV burn. `REPORT.txt` inside. |
| `data/STREAM_20260817T205330Z.csv` | 2,970-frame (154 s) 16-ch stream during rotor handling — source of coil-coupling envelopes and the coil-4 dead finding. |
| `data/STREAM_20260817T211201Z.csv` | Evening stream incl. 2.39 V hand-spin bursts, the FG 3.8 Hz tick, and coil-5 death. FG column present. |
| `data/SPIN_20260817T205617Z_firstspin/` | First spin campaign: stream, firmware log with first MTR_LOCK register dump. |
| `data/SPINHUNT_20260817T2130Z/` | Hunter v1/v2 event logs (all rounds/ladders with per-step coil activity). |
| `data/COILSCOPE_20260817T200143Z_pipelinecheck4/` | 7 Mpt × 4 ch scope record (coils 1–3 + C4), 50 MSa/s — capture-pipeline validation shot. |
| Artifact | Characterization report (boss-facing): https://claude.ai/code/artifact/01477929-cd80-44ed-9ff2-701d17ca8a00 |
| Artifact | Wiring/bring-up reference: https://claude.ai/code/artifact/d8e162a2-665a-40ac-bbe6-78341dbd1a9c |

## Stiffness estimate status

Order-of-magnitude only until a known-ω spin: Ke ≈ 0.1–0.2 V·s/rad per coil
(from 2.4 V pk at assumed 2–4 rev/s hand-spin). At 5 kΩ digipot load the ring
is a near-transparent damper (~2×10⁻⁵ N·m·s/rad); at minimum pot load ~50×
stronger. Passive stiffness at practical frequencies ≈ 0 (resistive load →
damping, not stiffness). Hard numbers require: known-speed spin (Ke), rotor
inertia (k from ringdown), coast-down pair at two pot settings (c directly).

## Late-session root cause: MCT8316Z register write-lock (21:5xZ)

The persistent nFAULT LED was NOT an uncleerable fault: the MCT8316Z powers
up with registers WRITE-LOCKED (REG_LOCK, CTRL1/0x03). SPI reads work while
locked; writes are silently ignored — so every CLR_FLT all session bounced.
Fix: write 0x03=0x03 (unlock code 011b) before any register write. After
unlock+CLR_FLT: IC_STAT 0x81 -> 0x08 (NPOR flag only, benign), nFAULT
released, LED dark. Firmware 'R' now always unlocks first.

Implication: all evening spin attempts ran against a mostly-latched driver
(clears never landed). Retry campaign is worth rerunning with working
clears. PWM wire verified separately: 25.000 kHz at SPEED pin (scope).

## GOLD campaign (dumb driver, 22:06:57–22:07:50Z) — CAPTURED

Motor spun continuously ~90 s on a known-good external driver. Data:
- `data/COILSCOPE_20260817T220657Z_gold_early/` + `..._220731Z_gold_late/`:
  700 k-sample × 8-ch records (1.4 s @ 500 kSa/s), the primary dataset.
- `data/COILSCOPE_20260817T220345Z_SPINNING/` + `..._SPINSLOW/`: first-spin
  captures at a different (higher) speed: coils 1/2/3/6 ≥4.4 V pkpk on the
  ADC (clipping ±2.5 V), fundamental ≈7.1 Hz electrical.
- `data/STREAM_20260817T220113Z_spin.csv`: envelope stream across the whole
  spin session (heavy frame loss from port contention, ~2 fps effective —
  usable for envelope only; scope records carry the waveforms).

Findings locked in: coils 4+5 WINDINGS ALIVE (0.24–1.7 V at terminals on
scope) while their ADC sense taps read ~6 mV — break is in the sense path.
Four live sense channels match within a few percent. ADC clips at speed:
future stiffness runs need lower speed or scope-only amplitude.

Next session: (1) repair coil 4+5 sense taps; (2) pole count or hall→GPIO40
for mech speed; (3) coast-down pair (5k vs TCON-open) for damping/Ke;
(4) MCT8316Z path: verify motor connector (phases read open at board?),
remember REG_LOCK unlock before any register write.

## Teardown correction (user physical inspection)

TWO COILS PHYSICALLY BROKEN — coils 4 and 5 themselves, not just sense
taps. Earlier "windings alive per scope" claim RETRACTED: the terminal
voltages were induced pickup on open stubs. Timeline: coil 4 dead from
first power-up (pre-existing); coil 5 died during the crash-heavy spin
window — crash-induced fatigue is the likely mechanism. Ring asymmetry
from 2/6 dead sectors also degrades any damping/drive compensation.

Rebuild order of operations: repair coils -> verify all 6 on ADC at
hand-spin -> keep operation away from f0 ≈ 3.6 Hz (or raise damping via
digipots) BEFORE long spin campaigns, so the ring stops eating itself.
