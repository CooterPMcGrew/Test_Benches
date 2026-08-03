// ADS1258 16-ch 24-bit delta-sigma ADC bench checkout + SPI diagnostics
// Board: ESP32 DevKit V1  |  GPIO18 SCLK / GPIO19 DOUT / GPIO23 DIN /
//                            GPIO17 CLKIO (10 MHz out; see clock options)
//
// Minimum-pin bench: 3 signal wires (+1 clock wire if no crystal).
// Everything else is strapped -- see README.md for the full table:
//   CS->DGND      always selected; SPI resyncs via SCLK-idle timeout
//   START->DVDD   free-running auto-scan
//   RESET->DVDD   RESET command (0xC0) covers it
//   PWDN->DVDD, AINCOM->AGND, VREFP->AVDD(5V), VREFN->AGND
//   DVDD MUST be 3V3: DOUT swings 0.8*DVDD into the ESP32.
//
// DRDY is not wired. Data is polled with the Channel Data Read Command
// (register-format read): unlike a direct read it cannot be corrupted by
// a DRDY update, and the STATUS byte NEW bit flags fresh data (SBAS297D).
//
// Each cycle: RESET + register dump vs power-on defaults (ID = 0x8B),
// write/readback test, internal-monitor self-test (OFFSET/VCC/TEMP/GAIN/
// REF -- proves supplies, clock, SPI and converter with ZERO analog
// wiring), then continuous 16-channel scan. On no response: SPI mode and
// speed probe, then a DC wiggle phase for meter-probing pins at the chip.

#include <SPI.h>

const int PIN_SCLK  = 18;
const int PIN_MISO  = 19;   // ADS1258 DOUT (chip pin 24)
const int PIN_MOSI  = 23;   // ADS1258 DIN  (chip pin 23)
const int PIN_CLKIO = 17;   // -> CLKIO (pin 13) with CLKSEL->DVDD. With a
                            // 32.768k crystal: CLKSEL->DGND, leave unwired.

// 10 MHz = exact LEDC divide of the 80 MHz APB clock (no fractional-divider
// jitter); CLKIO input spec is 0.1-16 MHz, 40-60% duty.
const uint32_t F_CLKIO_HZ = 10000000;

// SCLK must be <= fCLK/2. 1 MHz is safe for both clock options; 100 kHz
// fallback catches breadboard signal-integrity problems.
const uint32_t SPI_HZ_NORM = 1000000;
const uint32_t SPI_HZ_SLOW = 100000;

// VREFP is strapped to AVDD on this bench, so codes scale by the actual
// analog supply. Change if a real reference (e.g. 4.096 V) is fitted.
const float VREF_VOLTS = 5.0f;

// Datasheet scaling: 1 LSB = VREF/0x780000; VCC and REF monitor readings
// are code/786432 volts, GAIN is code/7864320 V/V (SBAS297D eq. 7-10).
const float CODE_FULLSCALE = 7864320.0f;   // 0x780000
const float MON_COUNTS_PER_V = 786432.0f;
const float TEMP_UV_25C   = 168000.0f;     // sensor voltage at +25 C
const float TEMP_UV_PER_C = 394.0f;

const uint8_t CMD_CHDATA = 0x30;  // channel data read, register format (MUL set)
const uint8_t CMD_RREG   = 0x40;
const uint8_t CMD_WREG   = 0x60;
const uint8_t CMD_RESET  = 0xC0;

const uint8_t REG_CONFIG0 = 0x00;
const uint8_t REG_MUXDIF  = 0x03;
const uint8_t REG_MUXSG0  = 0x04;
const uint8_t REG_MUXSG1  = 0x05;
const uint8_t REG_SYSRED  = 0x06;
const uint8_t REG_ID      = 0x09;

// Power-on defaults CONFIG0..ID; the checkout leaves config registers at
// default (CLKENB=1 keeps fCLK echoed on CLKIO in crystal mode -- scope it)
// and only ever writes the MUX/SYSRED selects.
const uint8_t REG_DEFAULT[10] = { 0x0A, 0x83, 0x00, 0x00, 0xFF,
                                  0xFF, 0x00, 0xFF, 0x00, 0x8B };

const uint8_t STAT_NEW = 0x80, STAT_OVF = 0x40, STAT_SUPPLY = 0x20;
const uint8_t CHID_AIN0 = 0x08;   // AIN0-15 = 0x08-0x17
const uint8_t CHID_OFFSET = 0x18, CHID_VCC = 0x1A, CHID_TEMP = 0x1B,
              CHID_GAIN = 0x1C, CHID_REF = 0x1D;
const uint32_t WANT_MONITORS = (1ul << CHID_OFFSET) | (1ul << CHID_VCC) |
                               (1ul << CHID_TEMP) | (1ul << CHID_GAIN) |
                               (1ul << CHID_REF);
const uint32_t WANT_AIN = 0xFFFFul << CHID_AIN0;

// With CS grounded the only resync handle is the SPI-interface timeout:
// 4096 fCLK of idle SCLK (SPIRST=0). 50 ms covers fCLK down to ~0.1 MHz.
const uint32_t RESYNC_MS = 50;

const uint32_t POLL_CAP = 40000;  // hard bound on NEW-bit poll loops (~2.5 s)

uint8_t  spi_mode = SPI_MODE0;    // datasheet: DIN latched on rising SCLK,
uint32_t spi_hz   = SPI_HZ_NORM;  // DOUT updated on falling -> mode 0
bool     chip_ok  = false;

uint8_t xfer1(uint8_t b) {
  SPI.beginTransaction(SPISettings(spi_hz, MSBFIRST, spi_mode));
  uint8_t r = SPI.transfer(b);
  SPI.endTransaction();
  return r;
}

void spiResync() { delay(RESYNC_MS); }

void cmdReset() { xfer1(CMD_RESET); delay(10); }

uint8_t regRead(uint8_t addr) {
  SPI.beginTransaction(SPISettings(spi_hz, MSBFIRST, spi_mode));
  SPI.transfer(CMD_RREG | addr);
  uint8_t v = SPI.transfer(0x00);
  SPI.endTransaction();
  return v;
}

void regWrite(uint8_t addr, uint8_t val) {
  SPI.beginTransaction(SPISettings(spi_hz, MSBFIRST, spi_mode));
  SPI.transfer(CMD_WREG | addr);
  SPI.transfer(val);
  SPI.endTransaction();
}

bool regWriteVerify(uint8_t addr, uint8_t val) {
  regWrite(addr, val);
  uint8_t rb = regRead(addr);
  if (rb != val)
    Serial.printf("  reg 0x%02X: wrote 0x%02X read 0x%02X FAIL\n", addr, val, rb);
  return rb == val;
}

// One 5-byte channel-data frame. Returns the STATUS byte; *code gets the
// sign-extended 24-bit result. Safe to call back-to-back: after a completed
// command frame the interface accepts a new command with no CS toggle.
uint8_t chanRead(int32_t *code) {
  SPI.beginTransaction(SPISettings(spi_hz, MSBFIRST, spi_mode));
  SPI.transfer(CMD_CHDATA);
  uint8_t st = SPI.transfer(0x00);
  int32_t v = (int32_t)(int8_t)SPI.transfer(0x00) << 16;
  v |= (int32_t)SPI.transfer(0x00) << 8;
  v |= SPI.transfer(0x00);
  SPI.endTransaction();
  *code = v;
  return st;
}

float codeToVolts(int32_t code) { return (float)code * VREF_VOLTS / CODE_FULLSCALE; }

// Poll until every CHID in `want` has been captured once or the cap hits.
// Returns the mask of CHIDs actually seen. vals[] is indexed by CHID.
uint32_t collect(uint32_t want, int32_t vals[32]) {
  uint32_t got = 0;
  uint32_t ovf = 0, supply = 0;
  for (uint32_t i = 0; i < POLL_CAP && (got & want) != want; i++) {
    int32_t code;
    uint8_t st = chanRead(&code);
    if (!(st & STAT_NEW)) continue;
    if (st & STAT_OVF) ovf++;
    if (st & STAT_SUPPLY) supply++;
    uint8_t ch = st & 0x1F;
    vals[ch] = code;
    got |= 1ul << ch;
  }
  if (ovf)    Serial.printf("  !! OVF set on %lu readings (|VIN| > 1.06*VREF)\n", ovf);
  if (supply) Serial.printf("  !! SUPPLY flag set %lu times (AVDD-AVSS below ~4.3V)\n", supply);
  return got & want;
}

// Register dump vs power-on defaults. "Responding" = ID reads 0x8B, or most
// defaults match (an all-0x00 / all-0xFF bus reads as dead wiring).
bool probeRegisters() {
  spiResync();
  cmdReset();
  int match = 0;
  uint8_t id = 0;
  Serial.print("regs:");
  for (uint8_t a = 0; a < 10; a++) {
    uint8_t v = regRead(a);
    if (a == REG_ID) id = v;
    if (v == REG_DEFAULT[a]) match++;
    Serial.printf(" %02X", v);
  }
  Serial.printf("  (%d/10 default, ID=0x%02X want 0x8B)\n", match, id);
  return id == 0x8B || match >= 6;
}

// Datasheet timing is SPI mode 0 at up to fCLK/2. If only some other
// mode/speed answers, the wiring or clocking is marginal -- adopt it so
// the bench keeps working, but say so loudly.
bool modeProbe() {
  const uint32_t hzs[2] = { SPI_HZ_NORM, SPI_HZ_SLOW };
  const uint8_t modes[4] = { SPI_MODE0, SPI_MODE1, SPI_MODE2, SPI_MODE3 };
  for (int h = 0; h < 2; h++) {
    for (int m = 0; m < 4; m++) {
      spi_hz = hzs[h];
      spi_mode = modes[m];
      Serial.printf("probe mode%d @%lukHz: ", m, spi_hz / 1000);
      if (probeRegisters()) {
        if (m != 0 || h != 0)
          Serial.println("!!! Chip answers off-datasheet SPI config -- check "
                         "SCLK/DIN wiring, clock source, and ground.");
        return true;
      }
    }
  }
  spi_mode = SPI_MODE0;
  spi_hz = SPI_HZ_NORM;
  return false;
}

// Detect whether a line is actively driven: a driven pin reads the same
// with pull-up and pull-down; a floating pin follows the pull.
const char *lineState(int pin) {
  pinMode(pin, INPUT_PULLDOWN);
  delayMicroseconds(50);
  bool down = digitalRead(pin);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(50);
  bool up = digitalRead(pin);
  if (down != up) return "FLOATING (open wire or chip unpowered?)";
  return down ? "driven HIGH" : "driven LOW";
}

// Static DC levels on SCLK/DIN so chip pins 22/23 can be meter-probed for
// swaps or breaks. Asymmetric on purpose: SCLK low 8 s, DIN low 2 s --
// reversed duty at the chip = swapped lines. CLKIO keeps running (a DMM on
// chip pin 13 reads ~1.65 V average if the clock wire is good).
void wigglePhase() {
  SPI.end();  // pinMode() would silently detach the pins from the SPI matrix
  pinMode(PIN_SCLK, OUTPUT);
  pinMode(PIN_MOSI, OUTPUT);

  digitalWrite(PIN_SCLK, LOW);
  digitalWrite(PIN_MOSI, HIGH);
  Serial.printf("[wiggle] SCLK=0V DIN=3.3V for 8 s | DOUT: %s\n", lineState(PIN_MISO));
  delay(8000);

  digitalWrite(PIN_SCLK, HIGH);
  digitalWrite(PIN_MOSI, LOW);
  Serial.println("[wiggle] SCLK=3.3V DIN=0V for 2 s");
  delay(2000);

  SPI.begin(PIN_SCLK, PIN_MISO, PIN_MOSI, -1);  // reattaches the pins
  spiResync();  // the wiggle clocked garbage into a selected chip
}

// Internal monitors prove the converter end-to-end with no analog wiring.
// CHOP=0 (default) is required for VCC/TEMP/GAIN/REF readings.
bool selfTest() {
  Serial.println("--- self-test: internal monitors only ---");
  bool cfg = regWriteVerify(REG_MUXDIF, 0x00) &&
             regWriteVerify(REG_MUXSG0, 0x00) &&
             regWriteVerify(REG_MUXSG1, 0x00) &&
             regWriteVerify(REG_SYSRED, 0x3D);  // REF|GAIN|TEMP|VCC|OFFSET
  if (!cfg) return false;

  int32_t v[32] = { 0 };
  if (collect(WANT_MONITORS, v) != WANT_MONITORS) {
    Serial.println("  monitors never reported -- is START strapped high?");
    return false;
  }

  float off_v  = codeToVolts(v[CHID_OFFSET]);
  float vcc_v  = (float)v[CHID_VCC] / MON_COUNTS_PER_V;
  float temp_c = ((float)v[CHID_TEMP] * VREF_VOLTS / CODE_FULLSCALE * 1e6f
                  - TEMP_UV_25C) / TEMP_UV_PER_C + 25.0f;
  float gain   = (float)v[CHID_GAIN] / CODE_FULLSCALE;
  float ref_v  = (float)v[CHID_REF] / MON_COUNTS_PER_V;

  bool ok = true;
  ok &= fabsf(off_v) < 0.005f;
  Serial.printf("  OFFSET %+0.4f V           %s\n", off_v,
                fabsf(off_v) < 0.005f ? "PASS" : "FAIL (want ~0)");
  ok &= vcc_v > 4.3f && vcc_v < 5.5f;
  Serial.printf("  VCC    %6.3f V            %s\n", vcc_v,
                (vcc_v > 4.3f && vcc_v < 5.5f) ? "PASS" : "FAIL (want 4.75-5.25)");
  ok &= temp_c > 0.0f && temp_c < 70.0f;
  Serial.printf("  TEMP   %6.1f C            %s\n", temp_c,
                (temp_c > 0.0f && temp_c < 70.0f) ? "PASS" : "FAIL (die temp)");
  ok &= gain > 0.95f && gain < 1.05f;
  Serial.printf("  GAIN   %6.4f V/V          %s\n", gain,
                (gain > 0.95f && gain < 1.05f) ? "PASS" : "FAIL (want ~1.000)");
  ok &= fabsf(ref_v - VREF_VOLTS) < 0.1f * VREF_VOLTS;
  Serial.printf("  REF    %6.3f V            %s (VREF_VOLTS=%0.3f)\n", ref_v,
                fabsf(ref_v - VREF_VOLTS) < 0.1f * VREF_VOLTS
                    ? "PASS" : "FAIL (floating VREF reads ~0)",
                VREF_VOLTS);
  Serial.println(ok ? ">>> SELF-TEST PASS <<<" : ">>> SELF-TEST FAIL (SPI is fine; check analog side) <<<");
  return true;  // SPI worked; analog FAILs are reported, not fatal
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== ADS1258 bench checkout + SPI diag ===");

  if (!ledcAttach(PIN_CLKIO, F_CLKIO_HZ, 3))
    Serial.println("!! LEDC clock start failed -- crystal-clocked boards unaffected");
  ledcWrite(PIN_CLKIO, 4);  // 4/8 = 50% duty

  SPI.begin(PIN_SCLK, PIN_MISO, PIN_MOSI, -1);  // no CS: chip is strapped selected

  // Power-up with CS grounded can leave the interface mid-"command"; the
  // documented no-CS remedy is SCLK idle for 2^18+4096 fCLK. 3 s covers
  // an external clock as slow as the 0.1 MHz spec minimum.
  Serial.println("interface settle wait (3 s)...");
  delay(3000);
}

void loop() {
  if (!chip_ok) {
    Serial.printf("probe mode0 @%lukHz: ", spi_hz / 1000);
    if (!probeRegisters() && !modeProbe()) {
      wigglePhase();
      return;
    }
    chip_ok = true;
    selfTest();
    // Continuous scan: all 16 single-ended channels + monitors
    if (!(regWriteVerify(REG_MUXSG0, 0xFF) &&
          regWriteVerify(REG_MUXSG1, 0xFF) &&
          regWriteVerify(REG_SYSRED, 0x3D))) {
      chip_ok = false;
      return;
    }
    Serial.println("--- scanning AIN0-15 (vs AINCOM) every 5 s; floating pins read mux bias ---");
  }

  int32_t v[32] = { 0 };
  uint32_t got = collect(WANT_AIN | WANT_MONITORS, v);
  if (got == 0) {  // device vanished -- back to probing
    Serial.println("no data -- reprobing");
    chip_ok = false;
    return;
  }
  for (int ch = 0; ch < 16; ch++) {
    bool have = got & (1ul << (CHID_AIN0 + ch));
    Serial.printf("AIN%02d %+7.3f%s", ch,
                  have ? codeToVolts(v[CHID_AIN0 + ch]) : 0.0f,
                  have ? " " : "?");
    Serial.print((ch % 4 == 3) ? "\n" : "  ");
  }
  Serial.printf("VCC %5.3f V  TEMP %4.1f C\n",
                (float)v[CHID_VCC] / MON_COUNTS_PER_V,
                ((float)v[CHID_TEMP] * VREF_VOLTS / CODE_FULLSCALE * 1e6f
                 - TEMP_UV_25C) / TEMP_UV_PER_C + 25.0f);
  delay(5000);
}
