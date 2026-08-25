# ISS Router — bench

Annular PCB router board (Rev 2): 6 coil drivers, 2× MCP4461-502 quad
digipots (I2C), ADS1258 16-ch ADC (SPI, own 32.768 kHz crystal). Two boards
— **LOWER** and **UPPER** — are checked out together on one ESP32-S3.

Ground truth for the board itself: `TN_ISSROUTER_BringupPinout_RevA.md`.
Running history: `LOG_ISSROUTER_ChangeLog_RevA.md`.

## Checkout rig — `ISS_Router_Checkout/`

Plug-and-read go/no-go panel. Power the S3, connect the DSUB harnesses,
read the 20×4 LCD. No buttons: the panel rescans every ~3 s and updates
live, so plugging a board in (or fixing a wire) shows up on its own.

### ESP32-S3 DevKitC-1 pin map (harness-authoritative)

LOWER board on the **left** header, UPPER on the **right**. Pins avoid
strap (0/3/45/46), USB (19/20), UART0 (43/44), flash/PSRAM (26–37) and the
RGB LED (48).

| Function | LOWER board | UPPER board | Notes |
|---|---|---|---|
| I2C SDA | GPIO8 | GPIO8 (shared) | all four digipots + LCD backpack |
| I2C SCL | GPIO9 | GPIO9 (shared) | |
| ADS1258 SCLK | GPIO12 | GPIO42 | separate SPI hosts (FSPI / HSPI) |
| ADS1258 DOUT → MISO | GPIO13 | GPIO41 | |
| ADS1258 DIN ← MOSI | GPIO11 | GPIO40 | |
| ADS1258 CS̄ | GPIO10 *(or strap to DGND)* | — strapped low on-board | CS̄ is on DSUB **J2.13**; two always-selected chips can't share DOUT, hence two buses |
| ADS1258 START | GPIO4 | GPIO39 | |
| ADS1258 RESET̄ | GPIO5 | GPIO38 | held high; pulsed low per probe |
| ADS1258 DRDȲ | GPIO6 | GPIO21 | edge-counted |
| IR1 / IR2 / IR3 / IR4 | — | GPIO15 / 16 / 17 / 18 | S3 ADC2, **≤ 3.1 V** — divider anything that can exceed 3.3 V |
| 3V3 / GND | both boards' digital domains + LCD | | |

Power: P3V3 external on J1; ±2.5 V analog rails are generated on-board and
need the external **−5 V** input. The ADS1258 does not answer SPI at all
until both supplies are up — "no comms" with −5 V absent is expected.

LCD: HD44780 20×4 behind a PCF8574 backpack, auto-detected at 0x20–0x27 /
0x38–0x3F (found at 0x27). Blank-but-backlit = contrast trimpot.

### What each cycle does

- **I2C scan + classification** — pots at 0x28–0x2F, LCD elsewhere.
- **Digipots** — every pot found: all 4 volatile wipers × 2 patterns
  write/readback, STATUS read; as-found wiper codes restored; NV never
  written. Only **4/4 in range** is a PASS: two chips on one address both
  ACK and both take writes, so a collision is invisible except as a
  missing device. Lower pair is 0x2C/0x2D; the upper pair must be strapped
  elsewhere in the range.
- **ADS1258 ×2** — hardware reset, register dump vs power-on defaults
  (ID = 0x8B), write/readback at 1 MHz with 100 kHz fallback. On failure,
  an SPI-leg isolator runs: MISO driven-vs-float under CS, and a blind
  CONFIG1 DRATE write with the DRDY rate as the readback channel — names
  the bad wire without a meter.
- **DRDY/START** — free-running rate and whether START gates it
  (informational; proves clock, rails, both GPIO wires).
- **IR1–4** — float check (pull-up vs pull-down), then 16-sample averaged
  millivolts against `IR_MIN_MV`/`IR_MAX_MV` (placeholders 0.2–3.0 V until
  the sensor's quiescent output is characterized).

### LCD readouts

```
POT 2C 2D 2E 2F   OK      row 0: addresses found + OK / FAIL / n/4
ADC-L 8B PASS gateOK      row 1: lower ADC (gateOK gateNO idleHI idleLO FLOAT)
ADC-U 8B PASS gateOK      row 2: upper ADC — or on failure:
                                 ADC-x check CS/MISO | chk SCLK/MOSI |
                                 cmdsOK MISObad | 8B WR FAIL
IR1.2 1.0 2.1 1.5 OK      row 3: volts per channel (flt = floating), OK / !!
```

Nothing connected at all → `ROUTER CHECKOUT / Please connect / lower +
upper boards / auto-scan #N`.

### Build, flash, capture

```sh
cd router_basic/ISS_Router_Checkout
arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc .
arduino-cli upload  --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc -p /dev/ttyACM0 .

# after any USB replug (tethys is not in dialout):
sudo setfacl -m u:$USER:rw /dev/ttyACM0

cd .. && uv run --with pyserial python TOOL_ISSROUTER_CheckoutMonitor_RevA.py 20
```

`CDCOnBoot=cdc` is required — the S3 talks over native USB (`/dev/ttyACM0`).

## Other files

| File | Purpose |
|---|---|
| `TN_ISSROUTER_BringupPinout_RevA.md` | board pinout, nets, J1/J2, digipot + ADC details |
| `ISS_Router_Bringup/` | original bring-up firmware (TN §5 pin plan — differs from the checkout harness above) |
| `TOOL_ISSROUTER_*_RevA.py` | autotest, bench dash, dual-scope, spin campaign, stream plot, capture, checkout monitor |
| `LOG_ISSROUTER_*` | first-boot log, spin-hunt session, change log |
| `ISS_ROUTER_Rev2.pdf` | schematic export (coil child sheets have invisible graphics) — kept local, not in git |
| `data/` | scope/autotest captures — **not in git** (≈0.9 GB), synced out-of-band |
