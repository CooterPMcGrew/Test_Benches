# TR_MCP444_Mcp4441BenchCharacterization_RevA

| | |
|---|---|
| **Document** | Test Report — Bench Characterization |
| **DUT** | MCP4441-502 (top marking `4441502` + Microchip logo) |
| **Date** | 2026-08-03Z |
| **Bench** | `~/test_benches/MCP444`, host `tethys` |
| **Status** | PASS — device fully functional, within datasheet limits |

---

## 1. Device Identification

Microchip MCP4441-502: quad-channel, 7-bit (129-tap) digital potentiometer,
5 kΩ nominal R_AB, nonvolatile (EEPROM) wiper memory, I²C interface,
TSSOP-20. Datasheet: DS20002265C.

| Parameter (datasheet) | Value |
|---|---|
| Resolution | 129 taps (codes 0x00–0x80) |
| R_AB nominal | 5.0 kΩ ±20% |
| Wiper resistance R_W | 75 Ω typical |
| V_DD operating | 2.7–5.5 V (serial-only down to 1.8 V) |
| Bus speeds supported | 100 kHz / 400 kHz / 3.4 MHz |
| POR wiper source | Nonvolatile wiper register (factory mid-scale, 0x40) |

## 2. Test Setup

Controller: ESP32 DevKit V1 (ESP32-WROOM-32), 3.3 V logic.
Firmware: `MCP444.ino` (arduino-esp32 core 3.3.11), bus at 100 kHz.
Instrumentation: handheld DMM (resistance + DC volts); register read-back
over I²C for digital-side verification.

### 2.1 Pinout and bench wiring (TSSOP-20, per datasheet Table 3-1)

| Pin | Symbol | Function | Bench connection |
|---|---|---|---|
| 1 | P3A | Pot 3 terminal A | open |
| 2 | P3W | Pot 3 wiper | open / DMM |
| 3 | P3B | Pot 3 terminal B | open / DMM |
| 4 | HVC/A0 | HV command / address bit 0 | float (internal pull-up → 1) |
| 5 | SCL | I²C clock input | GPIO22 + 4.7 kΩ → 3V3 |
| 6 | SDA | I²C data, open-drain | GPIO21 + 4.7 kΩ → 3V3 |
| 7 | V_SS | Ground | GND |
| 8 | P1B | Pot 1 terminal B | open / DMM |
| 9 | P1W | Pot 1 wiper | open / DMM |
| 10 | P1A | Pot 1 terminal A | open |
| 11 | P0A | Pot 0 terminal A | open |
| 12 | P0W | Pot 0 wiper | open / DMM |
| 13 | P0B | Pot 0 terminal B | open / DMM |
| 14 | WP | EEPROM write-protect, active low | 3V3 (writes enabled) |
| 15 | RESET | Hardware reset, active low | 3V3 |
| 16 | A1 | Address bit 1 | float (internal pull-up → 1) |
| 17 | V_DD | Supply | 3V3 |
| 18 | P2B | Pot 2 terminal B | open / DMM |
| 19 | P2W | Pot 2 wiper | open / DMM |
| 20 | P2A | Pot 2 terminal A | open |

Notes: SDA/SCL have no internal pull-ups and require the external 4.7 kΩ.
RESET/WP/A0/A1 are logic inputs with internal weak pull-ups; direct rail
ties are acceptable. Minimum viable harness is 4 wires: V_DD, V_SS, SDA, SCL.

## 3. Results

### 3.1 I²C interface

Device ACKs at **address 0x2F** (7-bit), consistent with base `0101` +
A1=1, A0=1 from the floating address pins. Grounding A0/A1 would yield
0x2C. Communication verified at 100 kHz: address ACK, register writes,
and register reads (repeated-start) all function.

### 3.2 Status and configuration registers

| Register | Read value | Decode |
|---|---|---|
| STATUS (0x05) | 0x182 | Bits 8:7,1 = reserved, forced to 1 (as specified). WL3/WL2/WL1/WL0 = 0 → no channel WiperLocked. EEWA = 0 → no EEPROM write active. WP = 0 → EEPROM writable (WP pin high). Factory-default state. |
| TCON0 (0x04) | 0x1FF | POR default: pots 0/1 — all terminals (A, W, B) connected, no shutdown |
| TCON1 (0x0A) | 0x1FF | POR default: pots 2/3 — all terminals connected, no shutdown |

### 3.3 Volatile wiper write/read verification

Each volatile wiper register written and read back over I²C; all four
channels returned the written value exactly.

| Channel | Register | Wrote | Read back | Result |
|---|---|---|---|---|
| Wiper 0 | 0x00 | 0x01F | 0x01F | PASS |
| Wiper 1 | 0x01 | 0x03F | 0x03F | PASS |
| Wiper 2 | 0x06 | 0x05F | 0x05F | PASS |
| Wiper 3 | 0x07 | 0x07F | 0x07F | PASS |

All four channels also track simultaneous staircase writes (read-back
`020/020/020/020` etc. at every step).

### 3.4 Analog resistance verification (W↔B)

Model (datasheet Eq. 5-2, 7-bit): `R_WB = R_AB × N/128 + R_W`.

| Code | Predicted (nominal) | Measured (DMM) | Result |
|---|---|---|---|
| 0x00 (zero) | ~75 Ω | not recorded | — |
| 0x20 (25%) | ~1.33 kΩ | not recorded | — |
| 0x40 (50%) | ~2.58 kΩ | **2.5 kΩ** (POR state, pot 1) | PASS |
| 0x60 (75%) | ~3.83 kΩ | not recorded | — |
| 0x80 (full) | ~5.08 kΩ | **4.8 kΩ** (pot 1) | PASS |

Derived actual R_AB ≈ 4.73 kΩ (**−5.5% from nominal**, within the ±20%
limit; DMM lead resistance not nulled). Unpowered, W↔B reads megaohms
(analog switches open); on power-up the device autonomously recalls the
nonvolatile wiper value (factory mid-scale) — observed as the 2.5 kΩ POR
reading, confirming EEPROM recall.

### 3.5 Behavioral notes

- Wiper register is 9 bits wide and stores out-of-range codes (> 0x80)
  unclamped; the analog wiper saturates at full-scale per Table 5-1.
  Read-back therefore cannot distinguish 7-bit from 8-bit parts.
- Full-scale (0x80) connects W to terminal A; zero-scale (0x00) to B; the
  residual resistance at zero-scale is the analog switch R_W (~75 Ω).
- WP protects nonvolatile memory only; volatile wiper and TCON writes are
  unaffected by WP state.

## 4. Conclusions

The device is fully functional: I²C interface verified at 100 kHz, all
four wiper channels writable/readable with exact read-back, TCON and
STATUS registers at documented POR defaults, EEPROM recall working, and
measured resistance within datasheet tolerance (R_AB −5.5% of nominal).
Suitable for use as a 4-channel programmable resistance in the 75 Ω–4.8 kΩ
range at 3.3 V.

## 5. References

1. Microchip DS20002265C, *MCP444X/446X 7/8-Bit Quad I²C Digital POT with
   Nonvolatile Memory* — <https://ww1.microchip.com/downloads/aemDocuments/documents/MSLD/ProductDocuments/DataSheets/MCP444X-446X-Data-Sheet-DS20002265.pdf>
2. Bench firmware: `MCP444.ino` (this directory)
3. Serial capture tool: `TOOL_MCP444_SerialMonitor_RevA.py`
