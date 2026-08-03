# ADS1258 Bench — Minimum-Pin Checkout

TI ADS1258 (16-ch / 8-diff, 24-bit delta-sigma, QFN-48) driven by an ESP32
DevKit V1 with **3 MCU signal pins** — plus a 4th only if the board has no
32.768 kHz crystal. Everything else is strapped. Datasheet: SBAS297D.

## MCU pins

| ESP32 | ADS1258 pin | Signal | Notes |
|---|---|---|---|
| GPIO18 | 22 | SCLK | SPI mode 0, 1 MHz (must stay ≤ fCLK/2) |
| GPIO23 | 23 | DIN | |
| GPIO19 | 24 | DOUT | Swings 0.8 × DVDD — DVDD must be 3V3 |
| GPIO17 | 13 | CLKIO | Only for clock option B below |

## Straps (no GPIO)

| ADS1258 pin | Name | Strap to | Why |
|---|---|---|---|
| 27 | /CS | DGND | Always selected; datasheet allows CS tied low. SPI resyncs itself after 4096 fCLK of idle SCLK |
| 26 | START | DVDD | Free-running auto-scan |
| 11 | /RESET | DVDD | RESET command (0xC0) covers device reset |
| 10 | /PWDN | DVDD | |
| 25 | /DRDY | — (unwired) | Polled via STATUS byte NEW bit instead — see below |
| 32 | AINCOM | AGND | Single-ended channels measure AINx − AINCOM |
| 31 | VREFP | AVDD (5 V) | Supply-as-reference; codes scale with actual AVDD. Fit a real 4.096 V ref for accuracy and update `VREF_VOLTS` |
| 30 | VREFN | AGND | |
| 28 | DVDD | 3V3 (DevKit 3V3 pin) | **Not 5 V** — logic must match the ESP32 |
| 6 / 5 | AVDD / AVSS | 5 V (DevKit VIN on USB) / GND | Unipolar: AVDD−AVSS must be 4.75–5.25 V |

## Clock — the ADS1258 has NO internal oscillator

Pick one:

- **A. Crystal:** 32.768 kHz crystal on XTAL1/XTAL2 (pins 8/9), 22 nF from
  PLLCAP (pin 7) to AVSS, **CLKSEL (pin 12) → DGND**. Internal PLL makes
  fCLK = 15.729 MHz, echoed on CLKIO (CLKENB default) — scope pin 13 to
  verify the PLL runs. GPIO17 stays unconnected (its clock output is
  harmless).
- **B. No crystal:** **CLKSEL → DVDD**, GPIO17 → CLKIO. The sketch outputs
  10 MHz via LEDC (exact divide of the 80 MHz APB clock, 50 % duty; CLKIO
  spec is 0.1–16 MHz, 40–60 %).

## Why no /DRDY pin

Data is read with the Channel Data Read *Command* (0x30, register-format,
5-byte frame). Unlike the direct read, it cannot be corrupted by a /DRDY
update mid-frame, and the STATUS byte's NEW bit flags fresh data — the
datasheet recommends exactly this for polled, CS-tied-low operation. The
STATUS CHID field identifies which channel each result belongs to.

## Firmware phases (`ADS1258.ino`)

1. **Probe** — RESET command, dump all 10 registers vs power-on defaults
   (ID must read 0x8B). All-0x00/0xFF dump = dead wiring.
2. **SPI diag** (only if probe fails) — retries every SPI mode at 1 MHz and
   100 kHz (off-datasheet hits are flagged, not trusted silently), then a
   DC wiggle phase: SCLK = 0 V / DIN = 3.3 V for 8 s, reversed for 2 s, so
   chip pins 22/23 can be meter-probed for swaps or opens. Reversed duty at
   the chip = swapped lines. DOUT is checked for driven-vs-floating.
3. **Self-test** — internal monitors only (SYSRED = 0x3D), zero analog
   wiring needed: OFFSET ≈ 0, VCC 4.75–5.25 V, die TEMP (394 µV/°C,
   168 mV @ 25 °C), GAIN ≈ 1.000, REF ≈ VREF. A floating reference reads
   REF ≈ 0. This separates SPI faults from analog-side faults.
4. **Scan** — all 16 single-ended channels + monitors every 5 s, in volts
   (1 LSB = VREF/0x780000; clips at |VIN| ≥ 1.06 VREF). Floating inputs sit
   at mux bias; jumper an AINx to AGND or 5 V and watch it move.

## Serial

DevKit auto-reset holds the chip in reset if DTR/RTS are asserted by a
naive monitor (`cat`, `arduino-cli monitor`). Use:

```
uv run --with pyserial python TOOL_ADS1258_SerialMonitor_RevA.py 20
```

If `/dev/ttyUSB0` is denied and the `dialout` group hasn't taken effect
yet: `sudo chmod 666 /dev/ttyUSB0` after each replug.
