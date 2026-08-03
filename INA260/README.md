# INA260 Bench — ESP32 DevKit v1

Streams bus voltage / current / power from an Adafruit INA260 (PID 4226) over
USB serial as CSV, with a host-side logger that stamps each sample in UTC.

---

## 1. Safety envelope — read before energizing

The INA260 is a **low-voltage DC** part. These are hard limits from the TI
datasheet, not guidelines:

| Parameter | Limit |
|---|---|
| Bus voltage, VIN+ / VIN− | **0 to +36 V DC** |
| Continuous current | **±15 A** |
| Logic supply (Vin pin) | 3–5 V |

- **No mains AC. No rectified mains.** Neither the part nor the breadboard
  wiring is rated for it.
- **Never exceed 36 V** on VIN+/VIN−, even transiently. Inductive loads
  (motors, solenoids, relays) kick well above their supply rail on turn-off —
  clamp them with a flyback diode or you will destroy the INA260.
- **No negative voltages** on VIN+/VIN− relative to GND.

### The ground-loop hazard

The ESP32 is USB-powered from the laptop, so **laptop ground is part of your
measurement circuit.** The INA260 measures bus voltage at VIN− referenced to
its own GND pin, so the supply under test must share that ground.

That means: use a **floating bench supply or a battery** for the voltage you
are injecting. If your source has its own earth reference, tying its negative
rail to the ESP32 ground creates a path through the laptop's USB ground. At
best you get a noisy trace; at worst you inject fault current into the
laptop's USB port.

If you cannot float the source, power the ESP32 from a USB battery pack or an
isolated USB hub instead of directly from the laptop.

---

## 2. Wiring

### Logic side — ESP32 to INA260 header

| ESP32 DevKit v1 | INA260 | Wire |
|---|---|---|
| `3V3` | `Vin` | red |
| `GND` | `GND` | black |
| `GPIO21` | `SDA` | blue |
| `GPIO22` | `SCL` | yellow |

`ALE` and `3Vo` are unused — leave them unconnected.

The Adafruit breakout has **onboard I2C pull-ups**. Do not add external
pull-up resistors; doing so over-loads the bus.

### Measurement side — the circuit under test

Current flows **into VIN+ and out of VIN−**. The sensor goes *in series* with
the load's positive leg (high-side sensing):

```
   Bench supply (+)  ──────►  VIN+  ┐
                                    │  INA260  (2 mΩ internal shunt)
   Load (+)          ◄──────  VIN−  ┘

   Load (−)          ──────┬─────►  Bench supply (−)
                           │
                           └─────►  INA260 GND  ──── ESP32 GND
                                    (common ground — REQUIRED)
```

Full picture:

```
  ┌──────────────┐                    ┌────────────────────┐
  │ ESP32        │                    │ Adafruit INA260    │
  │ DevKit v1    │                    │                    │
  │              │                    │                    │
  │  3V3  ───────┼────────────────────┼─► Vin              │
  │  GND  ───────┼──────────┬─────────┼─► GND              │
  │  GPIO21 ─────┼──────────┼─────────┼─► SDA              │
  │  GPIO22 ─────┼──────────┼─────────┼─► SCL              │
  │              │          │         │                    │
  │  USB ──► PC  │          │         │  VIN+ ◄── supply + │
  └──────────────┘          │         │  VIN− ──► load  +  │
                            │         └────────────────────┘
                            │
                            └──── supply − ──── load −
```

**Wiring order.** Connect the logic side first and confirm the boot log prints
`verified INA260 at 0x44` with nothing on VIN+/VIN−. Only then wire the
measurement side, with the bench supply off. Energize last.

On this unit A1 is strapped to VS, so the address is **0x44**, not the 0x40
default. The firmware sweeps 0x40–0x4F and confirms the TI manufacturer and
die IDs, so a bare I2C ack from an unrelated part sharing the address cannot
be mistaken for the sensor.

---

## 3. Build and upload

```bash
export PATH=$HOME/.local/bin:$PATH

arduino-cli compile --fqbn esp32:esp32:esp32doit-devkit-v1 INA260_Bench

sg dialout -c 'arduino-cli upload -p /dev/ttyUSB0 \
    --fqbn esp32:esp32:esp32doit-devkit-v1 INA260_Bench'
```

If upload stalls at `Connecting........`, hold the **BOOT** button until it
starts writing.

### Why `sg dialout`

This login session predates the `dialout` group membership, so `/dev/ttyUSB0`
is not writable in the current shell. `sg dialout -c '...'` runs a command
with the group applied — no `sudo`, no system changes. A logout/login makes it
permanent.

---

## 4. Read back data

Watch the stream live *and* capture it to a result file in one command:

```bash
cd /home/maint9/test_benches/INA260
sg dialout -c './log_ina260.py --dut AMB1 --test ReverseCurrent'
```

Ctrl-C to stop, or bound it with `--duration 60`. Dependencies (`pyserial`)
resolve into a `uv` scratch venv; nothing touches system Python.

### Result file naming

Files land in `./data/` as:

```
DATA_[DUT]_[TestTitle]_YYYYMMDD_HHMM.csv
DATA_AMB1_ReverseCurrent_20260728_2014.csv
```

The timestamp is **UTC**. `--dut` and `--test` are stripped of spaces and
underscores so the `_` field separators stay parseable. A run that would
collide with an existing file aborts rather than overwriting it.

Output columns:

```
utc,micros,volts,amps,watts
2026-07-28T20:14:03.117Z,1402380,12.0031,-0.2487,-2.9856
```

- `micros` is the ESP32's own uptime — use it for sample spacing, since it
  carries no USB-scheduling jitter. `utc` correlates against other bench gear.
- `amps` is **signed**: negative means current flowing VIN− → VIN+.
- `watts` is computed `V*I` in firmware, *not* the INA260's power register.
  That register is unsigned, so it would report `|V*I|` and hide the direction
  of energy flow during current reversal.

If the board resets mid-capture the `micros` timebase restarts; the logger
detects the backward step and writes a `# WARN` line at that point rather than
leaving a silent discontinuity in the data.

---

## 5. Active magnetic bearing notes

The DUT is a bipolar AMB coil. Bidirectional current needs no extra hardware —
the INA260's current register is signed (±15 A) and the firmware preserves it.
Three constraints matter more than the sensor choice:

### Bandwidth ceiling — 1 kHz

The firmware samples at 1 kHz (verified: 1000.1 Hz, max jitter 1 µs). Nyquist
puts the honest observable ceiling at **~500 Hz**, and even that assumes the
content above it is negligible. It is not, for a switching amplifier.

This rig measures **coil current envelope and DC bias**. It cannot resolve PWM
ripple or fast control-loop transients — those alias down into the passband and
appear as low-frequency content that is not real. For loop dynamics or
switching waveforms you need an isolated wideband sensor (hall-effect or
fluxgate, e.g. LEM LA-series) into a real ADC or a scope, not an I2C part.

### Inductive kickback will destroy the INA260

An AMB coil is a large inductance. De-energizing it produces `V = L·di/dt`,
which for a bearing coil runs *hundreds of volts* against a 36 V-rated part.
**A flyback path across every coil is mandatory**, not optional. Verify the
clamp with a scope before connecting the sensor.

### Drive topology decides whether the INA260 can be used at all

The INA260 measures VIN− relative to its own GND, and its common mode must stay
within 0–36 V. Whether that holds depends on how the coil is driven:

| Drive | Sensor placement | Works? |
|---|---|---|
| H-bridge, single +V supply | In the DC link | Yes — but reads supply current, not signed coil current. The bridge hides direction. |
| H-bridge, single +V supply | In a bridge output leg | Signed coil current, but common mode chops 0↔Vbus at PWM rate. Only if Vbus ≤ 36 V, and readings will alias badly. |
| True bipolar ±V supply | In series with the coil | **No.** The coil leg swings below GND; the INA260 cannot go negative. Use an INA282 (−14 to +80 V CM) instead. |

Settle this before trusting any capture — a reading taken outside the
common-mode window is wrong, not merely noisy.

## 6. Sweeping input voltages

Start the logger, then step the bench supply and hold each point for a few
seconds so the 64-sample average settles:

```bash
sg dialout -c './log_ina260.py --out sweep.csv'
```

Suggested points for a first characterization: 3.3, 5.0, 9.0, 12.0, 24.0 V.
Keep a real load on the output — with an open circuit you will read the bus
voltage but ~0 A, which tells you nothing about the current path.

**Accuracy note.** The INA260's current resolution is 1.25 mA/LSB. Below
roughly 10 mA the reading is dominated by quantization and offset, so treat
sub-10 mA measurements as indicative only. If your loads are that small, a
sense resistor plus an INA219 is the better instrument.

---

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| I2C scan finds nothing | Vin/GND not connected, or SDA/SCL swapped |
| Device at `0x44` instead of `0x40` | Normal on this unit — A1 is strapped to VS. Firmware auto-detects 0x40–0x4F and verifies mfg/die ID. |
| Volts reads ~0 with supply on | VIN−/VIN+ reversed, or grounds not common |
| Amps reads ~0 with supply on | Sensor not in series — likely wired across the load instead of in-line |
| Readings wander with no load | Floating input; expected with an open circuit |
| `cannot open /dev/ttyUSB0` | Not under `sg dialout`, or a monitor already holds the port |
