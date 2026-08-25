# LOG_ISSROUTER_ChangeLog_RevA

| | |
|---|---|
| **Document** | Change Log — ISS Router board |
| **Scope** | Board bringup, checkout rig, and findings |
| **Bench** | `~/test_benches/router_basic`, host `tethys` |
| **Convention** | Chronological, Zulu dates. Newest entries at the bottom. |

---

## Prior history (summary — see the named docs for detail)

- **2026-08-17Z** — First boot (`LOG_ISSROUTER_FirstBoot.txt`), full pinout
  reverse-engineered into `TN_ISSROUTER_BringupPinout_RevA.md` (PDF
  schematic graphics invisible; net annotations only). Bringup firmware
  `ISS_Router_Bringup/`. Digipots set to nominal and **NV-burned 6/6**;
  spin-hunt session logged (`LOG_ISSROUTER_SpinHuntSession_RevA.md`).
  Board power: P3V3 external via J1; ±2.5 V analog generated on-board
  (LT3045/LT3093) — needs the external −5 V input.

## 2026-08-24Z — Checkout rig built and exercised (this session)

### Rig
- User-built DSUB harness mates the router board to an **ESP32-S3
  DevKitC-1** (/dev/ttyACM0, native USB CDC) and a **20×4 I2C LCD**
  (PCF8574 backpack at 0x27). Board carries 2× **MCP4461-502** quad
  digipots (8-bit, 257 taps, 5 kΩ) + **ADS1258** ADC, own crystal.
- New firmware `ISS_Router_Checkout/ISS_Router_Checkout.ino`: auto-running
  go/no-go panel — I2C scan/classify, both digipots control/readback
  (4 wipers × 2 patterns, as-found codes restored, NV never written),
  ADS1258 aliveness (hardware reset, register dump vs defaults, ID=0x8B,
  write/readback), DRDY/START observation, SPI path fault isolator.
  Status on the LCD, full detail on serial (115200).
- Harness pin map (authoritative for this rig; differs from the TN §5
  bringup plan): SCK GPIO12, MISO GPIO13, MOSI GPIO11, CS GPIO10,
  START GPIO4, ADCRST GPIO5, DRDY GPIO6, SDA GPIO8, SCL GPIO9.
- Build: `arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc`.
  Capture: `TOOL_ISSROUTER_CheckoutMonitor_RevA.py` (RTS reset + dump).

### Findings, in order
1. **LCD blank at first power** — display was ACKing at 0x27 and accepting
   data; cause was the backpack **contrast trimpot**, not comms. Firmware
   set to 20×4 and given a "Please connect / auto-scan #N" idle screen
   (continuous rescan; no reset button needed).
2. **Both digipots PASS**: addresses **0x2C** and **0x2D**, STATUS=0x182
   (no wiper locks, EEPROM writable), all eight wipers write/readback
   clean every cycle. As-found wiper codes **0x080 on all 8 channels** =
   mid-scale for this 8-bit part (~2.5 kΩ W–B + R_W).
   - ⚠ Reconcile with the TN/2026-08-17Z note "digipots at nominal 5 k,
     NV-burned 6/6" — 0x080 reads as ~2.5 kΩ W–B, not 5 kΩ. Verify which
     resistance the burn actually targeted (W–B vs W–A vs full R_AB).
3. **ADS1258 dead without the analog rails — expected, not a fault.** TI
   guidance for this family: no required supply sequence, but the device
   does not operate (including SPI) until BOTH supplies are present.
   Corrected the checkout's original assumption that register comms would
   pass on DVDD alone.
4. **−5 V applied (±2.5 V rails up)** — chip immediately alive:
   conversions free-running (DRDY ~11.67 kHz), START gates them cleanly
   (proves START + DRDY wires end-to-end). SPI still all-0x00 both ways.
5. **SPI path diagnostic** (firmware, no meter): DOUT floats regardless of
   CS state, and a blind CONFIG1 DRATE write (readback channel = DRDY
   rate) has zero effect → both SPI directions dead, single common cause:
   **CS never reaches the chip**. Root cause: `ADC_CS_N` is routed to
   **DSUB J2.13** (TN §4) and that wire is not in the 8-signal harness,
   so CS floats. Converter runs; SPI stone dead. Matches user suspicion.
6. **Renamed effort**: rig was initially misfiled under the Stator bench
   (`Stator/StatorCheckout`, FAMILY STATOR). Moved and renamed to
   `router_basic/ISS_Router_Checkout` + FAMILY ISSROUTER (this log,
   monitor tool, sketch). No Stator-bench files were affected.

### Disposition
- **CS fix: jumper CS_n to DGND directly on the board** (user decision).
  Alternative left on the table: run DSUB J2.13 to GPIO10 for a real CS.
  Firmware handles both — strapped CS is covered by its SCLK-idle resync.
- No reflash needed after the jumper; the panel probes continuously and
  the ADC row flips to `ADC ID=8B SPI PASS` when comms come up.

### Open items
- [ ] Install CS→DGND jumper; confirm `ADC ID=8B SPI PASS` on the panel.
- [ ] Reconcile digipot as-found 0x080 (mid-scale ≈2.5 kΩ) against the
      2026-08-17Z "5 k NV burn" claim.
- [ ] Full ADC analog checkout (internal monitors, channel scan of the six
      coil-sense lines) once SPI is up — deep sketch lives in `../ADS1258/`.

## 2026-08-24Z (later) — Rig extended to LOWER + UPPER boards on one S3

- Second DSUB harness for the UPPER board. Same signal set as the lower
  board, plus four IR sensor analog outputs. Its ADS1258 CS is strapped on
  the board (no CS wire) — two always-selected chips cannot share DOUT, so
  the upper ADC gets the S3's second SPI host (HSPI) on the right header.
- Firmware rewritten around an `Adc` instance per board (shared driver,
  per-board pins/ISR/SPI host); digipot logic generalized to N found, with
  the lower pair pinned at 0x2C/0x2D and a **4/4-in-range = PASS** rule
  (address collisions are invisible to a scan except as a missing device).
  Four IR channels read on S3 ADC2 (GPIO15–18) with a float check and a
  nominal window (`IR_MIN_MV`/`IR_MAX_MV`, placeholders 0.2–3.0 V).
- Pin map published in `README.md` (harness-authoritative). Compiles
  clean (345 KB); **flash pending** — the S3 was off USB at the time.
- `.gitignore` added: `data/` (≈0.9 GB of scope captures) and
  `__pycache__/` stay out of the repo.

### Open items (added)
- [ ] Flash the dual-board build; verify lower board still PASSes end-to-end
      (regression), then upper.
- [ ] Set the IR nominal window from the sensor's real quiescent output;
      confirm the sensor outputs cannot exceed 3.3 V (S3 ADC pins are not
      5 V tolerant — divider if they can).
- [ ] Confirm the upper digipots are strapped away from 0x2C/0x2D (panel
      shows `2/4` if they collide).
