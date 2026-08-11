# TN_MCF8316_NextSessionPlan_RevA

**Technical note — session handoff and next-session plan (written 2026-08-11Z,
for the following bench session)**

## Where things stand (verified facts)

1. **EEPROM is truly burned and power-cycle-verified.** All nine config
   registers boot correctly from EEPROM (keyed write `0x8A500000` → 0xEA,
   750 ms, self-clear confirmed; then verified across a real power cycle).
   The board wakes up tuned: measured R/L/Ke, 60 kHz PWM, winner ramps
   (0.5 Hz/s CL, 2.5 Hz/s OL, 1.5 A OL), brake stop, PWM speed mode,
   operator-approved detector state.
2. **Grand sweep winner** (hall-scored): 254 RPM @ 60 s fault-free.
   Later, the max-speed climb hit a **record 385 RPM at t≈61 s, then
   collapsed to ~22 RPM** without faulting (mid-run loss of sync — same
   family as the 5 Hz/s OL failures; mechanism unconfirmed).
3. **Hall feedback is the only rotor truth.** 60 counts/rev (RPM = counts
   per second at 10 pp). Estimator/FG/scope-commutation all follow the
   estimator, which can "run" with a stationary rotor.
4. **Hot stator does not start** — observed twice (post-campaign direction
   runs, end-of-session burned-config demo): 0 RPM, no faults. Copper
   tempco (+0.4 %/K) cutting align/OL torque is the working hypothesis;
   unproven until a cold-vs-hot controlled comparison.
5. **Ke = 12.5 mV/elHz (code 0x3C)** from the original coast test remains
   the standing value; the upgraded hall-cross-referenced verification was
   interrupted by the 385-RPM collapse and remains pending.
6. **Board quirks:** DRVOFF pin path broken (do not rely on it; dead-man
   uses speed-0 + brake stop); FG output wire dead (halls replace it);
   supply must be ≥8 A capable (2 A limit caused historic bus collapse).

## Tomorrow's plan, in order

1. **Cold-start discriminator** (5 min, most informative single test):
   power on cold → verify EEPROM boot regs → single start attempt, no
   config writes, hall-logged. Starts cleanly = thermal hypothesis
   confirmed and the burned config is fully validated end-to-end.
   Fails cold = something else (investigate state trace + align current).
2. **Direction validation** (5 min): winner config both DIR values,
   hall-verified sign reversal. (Never yet achieved on a cold stator.)
3. **Ke verification v2** (10 min): rerun the climb with a cooling-aware
   profile — release to coast at ~250-300 RPM BEFORE any collapse
   (release on plateau OR at fixed 90 s, whichever first), 5-window BEMF
   cascade with hall cross-reference. Also yields the BEMF-vs-hall
   frequency ratio (free pole-count confirmation).
4. **Drag audit** (free, from #3's coast): fit the hall-speed decay →
   friction torque vs speed; compare against the 385-RPM collapse point
   to test whether drag explains the speed ceiling.
5. **Collapse investigation** (if time): repeat climb with per-2s hall +
   state logging around 350-400 RPM to catch the loss-of-sync signature;
   candidate mitigations: lower target, slower final approach, higher
   ILIMIT (thermal budget permitting).
6. **Torque-mode design note**: user has DRV8313-class boards (exact part
   number TBC — if DRV8316: integrated current sense, ideal). Draft
   DN_MCF8316_TorqueModeStage: ESP32-S3 + hall-FOC (SimpleFOC or custom)
   + driver board + shunts; reuse measured R/L/Ke/pole data; target the
   stator's optimal ~200 kHz PWM. This is the reaction-wheel path — the
   MCF8316C1 cannot do torque mode or zero-crossing (TR RevD §13 + chat).

## Operating cheat sheet

- Fault clear: ALWAYS `w ea 30000000` (CLR_FLT + CLR_FLT_RETRY_COUNT —
  bit 29 alone latches out after repeated lock faults).
- EEPROM write: config in shadow → `w ea 8a500000` → wait 750 ms →
  `r ea` must be 0. Only when motor idle. True verification = power cycle.
- Position mode: `w ec 4400` (FORCE_ALIGN_EN + register angle source),
  angle to ALGO_CTRL1[19:11] (`w ea <angle<<11>`), release with `w ec 0`.
- Bridge CLI: r/w/scan/fault/dir/speed/pins/fg/hall/hallzero; dead-man
  3 s (feed with any command, e.g. `pins`, during long host-side waits).
- Stall-heavy tests heat the stator ~20 W; insert cooling pauses and
  log a winding-resistance spot check between blocks (R rise = temp).
- Instruments: scope 10.42.0.29:5025 (single client!); LA fx2lafw
  (512-sample cap — replace for long captures); ESP32 /dev/ttyACM0.

## Data index for this session

`data/grandtune_*` (12-trial sweep + winner + first burn attempt),
`data/keclimb_*` (385-RPM climb + collapse trace + interrupted coast),
`data/burned_config_first_start.json` (hot no-start evidence),
TR_MCF8316_BringupAndTuning_RevD/E, all pushed.
