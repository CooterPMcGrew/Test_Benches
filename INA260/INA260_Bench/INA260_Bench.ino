/*
 * INA260_Bench - ESP32 DevKit v1 + Adafruit INA260 (PID 4226)
 *
 * Streams bus voltage / current / power as CSV over USB serial so a host
 * logger can capture it directly. Emits a startup banner and an I2C scan
 * because most bench bring-up failures are wiring, not code -- knowing the
 * bus is silent vs. answering at the wrong address separates "SDA/SCL
 * swapped" from "address jumper soldered".
 *
 * Platform assumptions:
 *   - ESP32 DevKit v1 (30-pin), Arduino-ESP32 core. 3.3V logic.
 *   - Default I2C pins: SDA = GPIO21, SCL = GPIO22.
 *   - Adafruit INA260 breakout carries its own bus pull-ups and regulator;
 *     no external pull-ups are needed or wanted.
 *   - Adafruit_INA260 returns milli-units (mV / mA / mW), not base units.
 *     Every read is converted once, here, so the CSV is in volts/amps/watts.
 */

#include <Adafruit_INA260.h>
#include <Wire.h>

// ---- Bench configuration -------------------------------------------------

// 921600 rather than 115200: at ~35 bytes per CSV line, 115200 caps the
// stream near 300 samples/s and the UART, not the sensor, becomes the limit.
static const uint32_t SERIAL_BAUD_BPS      = 921600;
static const uint8_t  I2C_SDA_PIN          = 21;
static const uint8_t  I2C_SCL_PIN          = 22;

// 400 kHz: three 16-bit register reads per sample at 100 kHz costs ~1.8 ms,
// which alone would cap the loop near 550 Hz. Keep jumper leads short.
static const uint32_t I2C_CLOCK_HZ         = 400000;

// The INA260 occupies 0x40..0x4F depending on how A0/A1 are strapped, so the
// address is discovered rather than assumed -- a bridged jumper otherwise
// looks identical to a dead sensor.
static const uint8_t  INA260_ADDR_FIRST    = 0x40;
static const uint8_t  INA260_ADDR_LAST     = 0x4F;

// 0x44 is shared with several unrelated sensor families (SHT3x, ISL29125),
// so a bare I2C ack is not proof. Confirm against the TI ID registers.
static const uint16_t INA260_MFG_UID       = 0x5449;  // 'TI'
static const uint16_t INA260_DIE_UID       = 0x2270;  // die 0x227, rev 0

// ---- Acquisition mode ----------------------------------------------------
//
// DYNAMIC: 1 kHz, 4-sample averaging. Tracks the control-loop envelope and
//          current reversals. Legitimate for CURRENT despite PWM drive --
//          the coil's own inductance is the anti-alias filter, holding ripple
//          near 1% of bias current. Not legitimate for VOLTS (see below).
//
// MEAN:    ~28 Hz, 128-sample averaging. Each reading integrates hundreds of
//          PWM cycles, so it returns a true mean immune to switching aliasing.
//          Use for precise bias-current characterization.
//
// Both modes leave the volts column meaningless when the sensor sits in an
// H-bridge output leg -- that node is an unfiltered square wave sampled at
// arbitrary phase. Trust amps; ignore volts and watts in that placement.
#define ACQ_MODE_DYNAMIC 1
#define ACQ_MODE_MEAN    2
#define ACQ_MODE         ACQ_MODE_DYNAMIC

#if ACQ_MODE == ACQ_MODE_DYNAMIC
  static const uint32_t SAMPLE_PERIOD_US = 1000;
  #define ACQ_AVERAGING  INA260_COUNT_4
  #define ACQ_LABEL      "dynamic  averaging=4  conv=140us  target=1kHz"
#elif ACQ_MODE == ACQ_MODE_MEAN
  // 128 samples x 140 us x 2 channels = ~35.8 ms per V+I pair.
  static const uint32_t SAMPLE_PERIOD_US = 35800;
  #define ACQ_AVERAGING  INA260_COUNT_128
  #define ACQ_LABEL      "mean  averaging=128  conv=140us  target=28Hz"
#else
  #error "ACQ_MODE must be ACQ_MODE_DYNAMIC or ACQ_MODE_MEAN"
#endif
static const uint8_t  INIT_MAX_ATTEMPTS    = 10;
static const uint32_t INIT_RETRY_DELAY_MS  = 500;

// INA260 absolute maxima (TI datasheet). Readings beyond these mean the part
// is being abused or the wiring is wrong -- flag rather than log silently.
static const float    BUS_VOLTAGE_MAX_V    = 36.0f;
static const float    CURRENT_MAX_A        = 15.0f;

Adafruit_INA260 ina260;

// Address discovered at boot; 0 means "no verified INA260 on the bus".
static uint8_t ina260_addr = 0;

// ---- I2C diagnostics -----------------------------------------------------

// Walks the 7-bit address space and reports every responder. Run once at
// boot: an empty bus points at power/SDA/SCL wiring, whereas a device at an
// unexpected address points at the A0/A1 address jumpers.
static void scanI2CBus(void) {
  Serial.println(F("# I2C scan:"));
  uint8_t found = 0;

  for (uint8_t addr = 0x08; addr < 0x78; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("#   device at 0x%02X\n", addr);
      found++;
    }
  }

  if (found == 0) {
    Serial.println(F("#   NONE. Check: 3V3->VIN, GND->GND, "
                     "GPIO21->SDA, GPIO22->SCL."));
  }
}

// Reads one 16-bit big-endian register. Returns false on any bus fault so a
// non-INA260 device that acks its address cannot be mistaken for a good read.
static bool readReg16(uint8_t addr, uint8_t reg, uint16_t *out) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {   // repeated start, no STOP
    return false;
  }
  if (Wire.requestFrom(addr, (uint8_t)2) != 2) {
    return false;
  }
  *out = ((uint16_t)Wire.read() << 8) | Wire.read();
  return true;
}

// Sweeps the INA260's strappable address range and returns the first address
// whose manufacturer and die IDs both match. Identity, not just an ack --
// otherwise an unrelated sensor sharing the address would be configured as if
// it were a power monitor and silently produce garbage readings.
static uint8_t findINA260(void) {
  for (uint8_t addr = INA260_ADDR_FIRST; addr <= INA260_ADDR_LAST; addr++) {
    uint16_t mfg = 0;
    uint16_t die = 0;

    if (!readReg16(addr, INA260_REG_MFG_UID, &mfg)) {
      continue;
    }
    if (!readReg16(addr, INA260_REG_DIE_UID, &die)) {
      continue;
    }

    if (mfg == INA260_MFG_UID && die == INA260_DIE_UID) {
      Serial.printf("# verified INA260 at 0x%02X (mfg=0x%04X die=0x%04X)\n",
                    addr, mfg, die);
      return addr;
    }

    Serial.printf("# 0x%02X acked but is not an INA260 "
                  "(mfg=0x%04X die=0x%04X)\n", addr, mfg, die);
  }
  return 0;
}

// ---- Sensor bring-up -----------------------------------------------------

// Retries a bounded number of times so a sensor powered up slightly after the
// ESP32 still gets picked up, then gives up loudly instead of spinning
// forever with no output.
static bool initSensor(void) {
  for (uint8_t attempt = 1; attempt <= INIT_MAX_ATTEMPTS; attempt++) {
    ina260_addr = findINA260();
    if (ina260_addr != 0 && ina260.begin(ina260_addr, &Wire)) {
      Serial.printf("# INA260 online at 0x%02X (attempt %u)\n",
                    ina260_addr, attempt);
      return true;
    }
    Serial.printf("# no verified INA260 yet, attempt %u/%u\n",
                  attempt, INIT_MAX_ATTEMPTS);
    delay(INIT_RETRY_DELAY_MS);
  }
  return false;
}

void setup(void) {
  Serial.begin(SERIAL_BAUD_BPS);
  while (!Serial && millis() < 3000) {
    // USB-CDC boards need a moment; hard cap so a headless run still proceeds.
  }

  Serial.println();
  Serial.println(F("# ---- INA260 bench ----"));

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN, I2C_CLOCK_HZ);
  scanI2CBus();

  if (!initSensor()) {
    Serial.println(F("# FATAL: INA260 never answered. Halting."));
    // Park instead of rebooting: a boot loop scrolls the wiring diagnostics
    // off the operator's screen, which is the one thing they need to read.
    while (true) {
      delay(1000);
    }
  }

  ina260.setAveragingCount(ACQ_AVERAGING);
  ina260.setVoltageConversionTime(INA260_TIME_140_us);
  ina260.setCurrentConversionTime(INA260_TIME_140_us);
  ina260.setMode(INA260_MODE_CONTINUOUS);

  Serial.println(F("# mode: " ACQ_LABEL));
  Serial.println(F("# watts is computed V*I (signed), NOT the chip's "
                   "unsigned power register"));
  Serial.println(F("# columns below are CSV; '#' lines are comments"));
  Serial.println(F("micros,volts,amps,watts"));
}

void loop(void) {
  static uint32_t next_sample_us = 0;

  const uint32_t now_us = micros();
  // Signed difference so this stays correct across micros() rollover
  // (~71.6 min) instead of stalling for the rest of the run.
  if ((int32_t)(now_us - next_sample_us) < 0) {
    return;
  }
  next_sample_us = now_us + SAMPLE_PERIOD_US;

  // Library returns milli-units; convert once so downstream is SI base units.
  const float volts = ina260.readBusVoltage() / 1000.0f;
  const float amps  = ina260.readCurrent()    / 1000.0f;

  // The INA260's power register is UNSIGNED -- it reports |V*I|, so during
  // reverse coil current it would read positive and hide the direction of
  // energy flow. Computing it here keeps the sign, which is what tells you
  // whether the bearing is driving or regenerating.
  const float watts = volts * amps;

  Serial.printf("%lu,%.4f,%.4f,%.4f\n",
                (unsigned long)now_us, volts, amps, watts);

  // Out-of-range readings usually mean the DUT exceeded the part's rating or
  // the sense leads are wrong. Warn on the comment channel so the CSV column
  // structure stays intact for the host parser.
  if (volts > BUS_VOLTAGE_MAX_V) {
    Serial.printf("# WARN bus voltage %.2f V exceeds %.1f V rating\n",
                  volts, BUS_VOLTAGE_MAX_V);
  }
  if (fabsf(amps) > CURRENT_MAX_A) {
    Serial.printf("# WARN current %.2f A exceeds +/-%.1f A rating\n",
                  amps, CURRENT_MAX_A);
  }
}
