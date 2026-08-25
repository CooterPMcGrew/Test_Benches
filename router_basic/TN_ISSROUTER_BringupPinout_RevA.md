# TN_ISSROUTER_BringupPinout_RevA

ISS Router board (Rev 2, annular PCB) — bench bring-up pinout and plan.
Source: `ISS_ROUTER_Rev2.pdf` (Altium export, 2026-08-17Z). Bench host:
ESP32-S3 DevKitC-1.

**PDF caveat:** pages 1–6 (the `Coil.SchDoc` child sheets) exported with
invisible graphics — only net annotations survive. Everything below comes from
the top sheet (page 7). The coil-driver internals are NOT yet documented; see
Open Questions.

---

## 1. Architecture

Six identical coil-driver blocks (`Coil_1..6`, internals unknown: 2× 6-pin
FET-class parts, R5–R12, C3–C9 each) sit around the ring. Each block gets one
MCP4461 digipot channel (`COIL_Cx_DPOT_P/N`, rheostat W–B, terminal A
unconnected) and returns a current-sense output `ISNS_x` to the ADC. A shared
`COIL_OFF` net gates all six blocks. An ADS1258 (IC3) digitizes the six sense
lines plus ten external analog inputs. Control interface is I2C (digipots) +
SPI (ADC) on two Samtec TFM-110-01-L-D-A headers (J1 power/I2C, J2 ADC/analog).

A red-boxed, grayed-out region with two `ADC_BLK` sheet instances
(BUSY/FRSTDATA/CONVST-style parallel ADC interface) is a disabled legacy
design — confirm those footprints are unpopulated on the physical board and
ignore.

## 2. Power

| Rail | Source | Used by |
|---|---|---|
| P5V0 | **J1 (external)** | LT3045 (IC5) → P2V5; coil drivers |
| N5V0 | **J1 (external)** | LT3093 (IC4) → N2V5 |
| P3V3 | **J1 (external)** | ADS1258 DVDD, both MCP4461, I2C pull-ups (R13/R14 4.7k) |
| P2V5 | on-board LT3045 | ADS1258 AVDD + VREFP, TLV365 buffers V+ |
| N2V5 | on-board LT3093 | ADS1258 AVSS + VREFN, TLV365 buffers V− |

- **The board has no negative-rail generator. N5V0 must be supplied or the
  ADC analog section is dead** (AVSS/VREFN missing). Bench PSU −5 V,
  current-limit 50 mA, is plenty (LT3093 + ADS1258 analog + TLV365 ≈ 20 mA).
- ADC reference is **VREFP−VREFN = P2V5−N2V5 = 5.0 V differential**; analog
  input range is bipolar **±2.5 V max** (AVDD/AVSS rails). Never drive an
  `ANALOG_x` pin beyond ±2.5 V — a raw 3.3 V logic level violates abs max.
- `ADCGND` joins `GND` through ferrites L1/L2 (BLM18PG121SN1D).

## 3. J1 — power + I2C (TFM-110, 2×10)

| Pin | Net | Pin | Net |
|---|---|---|---|
| 1 | I2C_SCL | 2 | NC |
| 3 | I2C_SDA | 4 | NC |
| 5 | NC | 6 | NC |
| 7 | NC | 8 | NC |
| 9 | P5V0 | 10 | P5V0 |
| 11 | GND | 12 | GND |
| 13 | P3V3 | 14 | P3V3 |
| 15 | GND | 16 | GND |
| 19 | GND | 20 | GND |

## 4. J2 — ADC SPI + analog inputs (TFM-110, 2×10)

| Pin | Net | Pin | Net |
|---|---|---|---|
| 1 | ADC_RST | 2 | ANALOG_0 |
| 3 | GND | 4 | ANALOG_1 |
| 5 | NC | 6 | ANALOG_2 |
| 7 | GND | 8 | ANALOG_3 |
| 9 | ADC_DRDY_N | 10 | **ANALOG_5** |
| 11 | ADC_STRT | 12 | **ANALOG_4** |
| 13 | ADC_CS_N | 14 | ANALOG_6 |
| 15 | ANALOG_9 | 16 | ADC_SCLK |
| 17 | ANALOG_8 | 18 | ADC_DIN |
| 19 | ANALOG_7 | 20 | ADC_DOUT |

Note the deliberate 5-before-4 order on pins 10/12.

## 5. ESP32-S3 DevKitC-1 hookup

All ten signals land on one physical header column of the DevKitC-1
(GPIO16…14 run), plus power.

| S3 pin | Dir | Board pin | Net | Function |
|---|---|---|---|---|
| GPIO9 | out | J1.1 | I2C_SCL | I2C clock (pull-ups on board) |
| GPIO8 | bidir | J1.3 | I2C_SDA | I2C data |
| GPIO16 | out | J2.1 | ADC_RST | ADS1258 RESET̄ (also 0xC0 cmd) |
| GPIO17 | in | J2.9 | ADC_DRDY_N | data-ready, falling edge |
| GPIO14 | out | J2.11 | ADC_STRT | START — high = free-run |
| GPIO10 | out | J2.13 | ADC_CS_N | SPI CS̄ (FSPI default SS) |
| GPIO12 | out | J2.16 | ADC_SCLK | SPI SCLK (FSPI) |
| GPIO11 | out | J2.18 | ADC_DIN | SPI MOSI (FSPI) |
| GPIO13 | in | J2.20 | ADC_DOUT | SPI MISO (FSPI) |
| 3V3 | pwr | J1.13 (+14) | P3V3 | digital rail, ~20 mA |
| GND | pwr | J1.11/12/15/16/19/20, J2.3/7 | GND | at least two wires + one on J2 |
| — bench PSU +5 V | pwr | J1.9 (+10) | P5V0 | limit 100 mA for bring-up |
| — bench PSU −5 V | pwr | J1.17 (+18) | N5V0 | limit 50 mA |

Bench PSU commons tie to ESP32 GND. USB-powered S3 5 V pin can substitute for
the +5 V bench channel during phase 1 (coils off), but the −5 V channel is
unavoidable.

GPIO choices avoid all S3 strapping pins (0/3/45/46), USB-JTAG (19/20), and
UART0 (43/44); 8/9 and 10–13 are the Arduino-ESP32 S3 defaults for
Wire and SPI(FSPI).

## 6. Digipots — 2× MCP4461-502E/ST (quad, 8-bit, NV, RAB = 5 kΩ)

Wiring per channel: rheostat, **B = `..._DPOT_P`, W = `..._DPOT_N`, A
unconnected**. "Nominal 5 k" = wiper code **0x100** (full scale, R_WB ≈ RAB =
5.0 k nom, ±20 % RAB tolerance). Power-on value comes from EEPROM — factory
default is mid-scale 0x80 ≈ 2.5 k, so pots must be re-written (or NV-burned)
to hold 5 k.

### Addressing — floating A1

7-bit address = `0101 1 A1 A0`. Straps R25/R26 (IC1) and R27/R28 (IC2) are
all DNP → **A1 (pin 16) floats on both chips** (as observed). HVC/A0 is
hard-strapped and does distinguish the chips:

| Chip | A0 strap | Possible addresses | Serves (net truth) |
|---|---|---|---|
| IC1 | GND | 0x2C or 0x2E (even) | wiper0→Coil6, wiper1→Coil2, wiper2→Coil1 |
| IC2 | P3V3 | 0x2D or 0x2F (odd) | wiper0→Coil4, wiper1→Coil5, wiper2→Coil3 |

Wiper 3 unused on both. Bring-up firmware scans 0x2C–0x2F and assigns by
address parity, re-scanning on NACK. **Root-cause fix (recommended bodge):
populate R26 and R28 (10 k to GND) → deterministic 0x2C/0x2D.** A floating
CMOS address input can flap and burns idle current; the scan is a workaround,
not a fix.

Schematic "For coils 1-3 / 4-6" comments near IC1/IC2 contradict the net
names; the nets (table above) are ground truth.

WP̄ (pin 14) routing is ambiguous in the export ("Check the WP" note on
schematic). It has an internal pull-up, so volatile wiper writes always work;
only EEPROM burn could be blocked if WP̄ is actually strapped low. Verify with
a meter if the NV burn fails.

## 7. ADC — ADS1258IRTCR (IC3)

- Clock: 32.768 kHz crystal (Y1) + on-chip PLL, CLKSEL strapped low →
  fCLK ≈ 15.73 MHz. No external clock wire needed. SPI mode 0, SCLK ≤ fCLK/2
  (1 MHz used on jumpers).
- PWDN̄ / START / RESET̄ pulled to P3V3 on-board (R21–R23 25.5 k cluster), so
  the chip idles alive with the ESP32 unplugged.
- MUXOUT→ADCIN external loop through two TLV365 buffers (IC6/IC7) with
  10 k / 100 pF interstage — signal path only exists because of this loop
  (same topology as the ADS1258 bench-adapter rule: no loop, no readings).
- AINCOM = GND; all channels single-ended bipolar, 1 LSB = 5.0 V / 0x780000.

### Channel map (net truth, swaps folded in)

| AIN | Net | Meaning |
|---|---|---|
| 0 | ISNS_0 | Coil 1 sense |
| 1 | ISNS_1 | Coil 2 sense |
| 2 | ISNS_2 | Coil 3 sense |
| 3 | ISNS_3 | Coil 4 sense |
| 4 | **ISNS_5** | **Coil 6** sense — AIN4/5 flipped (schematic note) |
| 5 | **ISNS_4** | **Coil 5** sense |
| 6–9 | ANALOG_0–3 | J2.2/4/6/8 |
| 10 | **ANALOG_5** | J2.10 |
| 11 | **ANALOG_4** | J2.12 |
| 12 | ANALOG_6 | J2.14 |
| 13 | ANALOG_7 | J2.19 |
| 14 | ANALOG_8 | J2.17 |
| 15 | ANALOG_9 | J2.15 |

Auto-scan of all 16 channels at max DRATE ≈ 23.3 kSPS aggregate ≈ 1.45 kSPS
per channel (fCLK = 15.73 MHz).

## 8. Bring-up sequence

1. **Cold checks** (no power): no shorts P5V0/N5V0/P3V3→GND; GND↔ADCGND ≈ 0 Ω
   (ferrites); legacy ADC_BLK footprints unpopulated; R25–R28 absent.
2. **Power, no ESP32**: +5 V then −5 V (limits above). Verify P2V5 = +2.5 V
   (C18), N2V5 = −2.5 V (C17). Both LDOs are set by 25.5 k SET resistors.
3. **Connect S3 per §5**, flash `ISS_Router_Bringup`. Firmware then:
   a. I2C-scans, identifies IC1/IC2, writes TCON + wipers 0–2 to 0x100,
      reads back → **digipots at nominal 5 k** (volatile).
   b. ADS1258: hardware reset, register dump vs defaults (ID = 0x8B),
      write/verify, internal-monitor self-test (VCC ≈ 5.0, REF ≈ 5.0, TEMP
      sane) — proves supplies/clock/SPI with zero analog wiring.
   c. Streams all 16 channels with net-true names. ISNS_x ≈ 0 V (coils off);
      floating ANALOG_x read mux bias — normal.
4. **Stimulus check**: S3 PWM → 10 k series → 10 k∥1 µF to GND → ANALOG_x pin
   (divider keeps it ≤ 1.65 V, inside the ±2.5 V window). Confirm the channel
   map, especially the 4/5 and 10/11 swaps.
5. **NV burn (optional)**: serial command `n` persists 5 k through power
   cycles. Verify by power cycle, as with the MCF8316 keyed-burn procedure.

## 9. As-built findings (bring-up 2026-08-17Z)

- **SDA/SCL crossed on the bench harness** at J1; firmware detects and runs
  with pins swapped in the ESP32 GPIO matrix. Uncross at next rewire.
- **I2C pull-ups R13/R14 appear unstuffed** on this build (bus floats at the
  S3 with P3V3 proven live). Bus runs fine on ESP32 internal pull-ups at
  100 kHz over the bench harness; fit 4.7 k for anything faster/longer.
- **Actual VREF = 5.109 V** (REF monitor; matches 2× 25.5 k × 100 µA LDO SET
  math). Firmware `VREF_VOLTS` is 5.000 → readings scale ~2.1 % low; update
  for calibrated work. VCC monitor 5.097 V. P3V3 at J1.14 scoped 3.20 V.
- **ISNS idle levels (coils off):** +16 to +106 mV, channel-dependent;
  baseline noise ≤ 2.9 mV std on all 16 channels.
- **Digipots: 5 k set and NV-burned 6/6** (2026-08-17Z); POR now loads 5 k.
  Power-cycle verification pending. WP held high by bench jumpers.
- **Stimulus anomalies to re-check (generator-side):** JDS6600 0.5 Vpp
  setting measured 0.61 Vpp (ADC); 1.0 Vpp at 100 Hz measured 1.40 Vpp
  (scope) / 1.34 (ADC envelope) — ADC and scope agree with each other, so
  the measurement chain is consistent and the generator output is suspect.

## 10. Open questions

1. **Coil.SchDoc is unreadable** (invisible export). Needed before coil-fire
   testing: drive topology, what turns a coil on (COIL_OFF polarity? digipot
   TCON gating?), ISNS scale (V per A), safe drive duration for latching.
   → Re-export the PDF with child sheets visible, or share Coil.SchDoc.
2. What the router routes (system test definition): expected behavior per
   coil fire, and what the ANALOG_0–9 inputs carry in the real system.
3. −5 V source on your bench: PSU channel assumed; a TPS60403-class charge
   pump off 5 V is the portable alternative.

## 11. Halbach spin phase (added 2026-08-17Z)

### Dual-scope 6-coil measurement

S3 is trigger master: GPIO15 strobe splits to **CH4 of both scopes**; both
arm SINGLE on its rising edge -> hardware-aligned records.

| Scope | C1 | C2 | C3 | C4 |
|---|---|---|---|---|
| SDS1104X-E (.220) | coil 1 | coil 2 | coil 3 | SYNC (GPIO15) |
| SDS1104X-U (.221) | coil 4 | coil 5 | coil 6 | SYNC (GPIO15) |

Probe points: start on the ISNS_x nets (ground-referenced by design; also
cross-checks the ADC). Raw COIL_P/N probing waits on Coil.SchDoc — COIL_N
ground reference unknown.

Capture: `uv run --with numpy,pyserial python3 TOOL_ISSROUTER_DualScope_RevA.py
--label spin1 --n 3` (drop `--auto` once the sync wire is run). Verified
end-to-end on the X-E: full 7 Mpt/ch records, CSVs + metrics per shot.
X-E SCPI quirks found: drops rapid-fire commands (250 ms pacing needed),
STOP required before waveform readout, 4-ch memory is the 7K–7M family.

### MCT8316Z (SLLSFH3) — Halbach spin motor driver

SPI: 16-bit frame, B15=R/W (1=read), B14:9=addr, B8=even parity, B7:0=data.
SDO returns 8 STAT bits + 8 register bits. Mode 1 (idle-low, falling-edge
capture), ≤5 MHz (1 MHz used), nSCS high ≥400 ns between words. Registers
reset on power-up AND sleep. Control regs: 0x03–0x0C (0x04–0x06 nonzero at
POR — comms probe targets). Status: 0x00–0x02.

| S3 pin | MCT8316Z | | S3 pin | MCT8316Z |
|---|---|---|---|---|
| GPIO4 | nSCS | | GPIO6 | SDI |
| GPIO5 | SCLK | | GPIO7 | SDO |
| GPIO21 | SPEED (PWM 25 kHz, if PWM speed mode) | | | |

### Serial command set (current firmware)

`p` pots scan+5k · `n` NV burn · `a` ADC reprobe · `c` CSV toggle ·
`x` wire diag · `w`/`W` meter modes · `d` spoof duty · `t` scope sync pulse ·
`m` MCT8316Z probe · `+`/`-`/`0` MCT speed duty
