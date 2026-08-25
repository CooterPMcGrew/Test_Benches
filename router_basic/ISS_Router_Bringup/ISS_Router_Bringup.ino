// ISS Router Rev2 bench bring-up: MCP4461 digipots to nominal 5k + ADS1258
// checkout + 16-channel stream with net-true channel names.
// Board: ESP32-S3 DevKitC-1. Wiring: TN_ISSROUTER_BringupPinout_RevA.md §5.
//
// Derived from the proven ADS1258 bench sketch (test_benches/ADS1258). Deltas
// from that bench: CS/RESET/DRDY are real wires here (no straps), the clock
// is the on-board 32.768k crystal + PLL (no CLKIO wire), and VREF is the
// bipolar P2V5-N2V5 pair (still 5.0 V differential, so all monitor
// thresholds carry over). Analog inputs are hard-limited to +/-2.5 V by the
// supplies -- never present raw 3V3 logic to an ANALOG_x pin.
//
// Digipot addressing: A1 (pin 16) floats on BOTH chips (straps R25-R28 all
// DNP), so each chip answers at one of two addresses and may even move
// between power-ups. HVC/A0 is hard-strapped (IC1=GND, IC2=P3V3), so parity
// identifies the chip: even = IC1 (coils 6,2,1), odd = IC2 (coils 4,5,3).
// The scan is a workaround; populating R26/R28 pins A1 low is the fix.
//
// Serial commands: 'p' = re-scan/re-set pots, 'n' = burn NV wipers (persist
// 5k through power cycles), 'c' = toggle CSV stream for the plot tool,
// 'a' = re-probe the ADC from scratch, 'd' = cycle spoof PWM duty.

#include <SPI.h>
#include <Wire.h>

// --- pins (DevKitC-1, one header column + power) ---
const int PIN_SDA   = 8;    // J1.3  I2C_SDA (4.7k pull-ups on board)
const int PIN_SCL   = 9;    // J1.1  I2C_SCL
const int PIN_CS    = 10;   // J2.13 ADC_CS_N
const int PIN_MOSI  = 11;   // J2.18 ADC_DIN
const int PIN_SCLK  = 12;   // J2.16 ADC_SCLK
const int PIN_MISO  = 13;   // J2.20 ADC_DOUT
const int PIN_START = 14;   // J2.11 ADC_STRT (board also pulls high)
const int PIN_RST   = 16;   // J2.1  ADC_RST  (board also pulls high)
const int PIN_DRDY  = 17;   // J2.9  ADC_DRDY_N (used as a wiring check only;
                            // data is polled via the NEW status bit)
const int PIN_SPOOF = 18;   // PWM -> 10k series -> 10k||1uF to GND -> any
                            // ANALOG_x pin. Divider halves 3V3 so the level
                            // stays inside the +/-2.5 V input window.
const int PIN_TRIG  = 15;   // sync strobe -> CH4 of BOTH scopes (split lead).
                            // 't' fires one 10 ms pulse; scopes armed SINGLE
                            // on its rising edge capture time-aligned.

// 5 kHz / 12-bit like the old bench; duty cycles 25/50/75% via 'd'.
const uint16_t SPOOF_DUTIES[3] = { 1024, 2048, 3072 };
uint8_t spoof_ix = 1;

// =====================================================================
// MCP4461-502E/ST x2 -- quad 8-bit NV digipot, RAB = 5k nominal
// =====================================================================
// Address = 0101 1 A1 A0. A1 floats -> two candidates per chip.
const uint8_t POT_SCAN[4]  = { 0x2C, 0x2E, 0x2D, 0x2F }; // even=IC1, odd=IC2

// Register map: volatile wipers 0-3, NV wipers, terminal-connect, status.
const uint8_t PREG_VW[4]  = { 0x00, 0x01, 0x06, 0x07 };
const uint8_t PREG_NVW[4] = { 0x02, 0x03, 0x08, 0x09 };
const uint8_t PREG_TCON0  = 0x04;   // pots 0,1 -- 0xFF = all terminals on
const uint8_t PREG_TCON1  = 0x0A;   // pots 2,3

// Full scale: R_WB ~= RAB = 5.0k nominal (RAB tol +/-20%). POR loads the NV
// wiper (factory mid-scale 0x80 ~= 2.5k), hence this explicit set every boot.
const uint16_t WIPER_5K = 0x100;

// Wipers 0-2 are wired (B=DPOT_P, W=DPOT_N, A floating); wiper 3 is spare.
// Net truth -- the schematic "For coils 1-3/4-6" comments are wrong:
const char *POT_COIL[2][3] = { { "coil6", "coil2", "coil1" },    // IC1 (even)
                               { "coil4", "coil5", "coil3" } };  // IC2 (odd)

uint8_t pot_addr[2] = { 0, 0 };     // discovered addresses, 0 = not found

// MCP44XX frame: [AAAA CC DD] + data. C=00 write, 11 read; D = data bits 9:8.
bool potWrite(uint8_t a7, uint8_t reg, uint16_t val) {
  Wire.beginTransmission(a7);
  Wire.write((reg << 4) | ((val >> 8) & 0x03));
  Wire.write(val & 0xFF);
  return Wire.endTransmission() == 0;
}

bool potRead(uint8_t a7, uint8_t reg, uint16_t *val) {
  Wire.beginTransmission(a7);
  Wire.write((reg << 4) | 0x0C);
  if (Wire.endTransmission(false) != 0) return false;   // repeated start
  if (Wire.requestFrom((int)a7, 2) != 2) return false;
  *val = ((uint16_t)(Wire.read() & 0x01) << 8) | Wire.read();
  return true;
}

// Scan candidates, keep first even-parity hit as IC1 and odd as IC2. A
// readable volatile wiper (<=0x100) distinguishes a real pot from a stray ACK.
void potScan() {
  pot_addr[0] = pot_addr[1] = 0;
  for (int i = 0; i < 4; i++) {
    uint16_t v;
    if (!potRead(POT_SCAN[i], PREG_VW[0], &v) || v > 0x100) continue;
    int chip = POT_SCAN[i] & 1;         // odd address bit -> IC2
    if (!pot_addr[chip]) {
      pot_addr[chip] = POT_SCAN[i];
      Serial.printf("  IC%d at 0x%02X (A1 floated %s), VW0=0x%03X\n", chip + 1,
                    POT_SCAN[i], (POT_SCAN[i] & 2) ? "HIGH" : "LOW", v);
    }
  }
  if (!pot_addr[0]) Serial.println("  !! IC1 (even addr 0x2C/0x2E) not found");
  if (!pot_addr[1]) Serial.println("  !! IC2 (odd addr 0x2D/0x2F) not found");
  if (!pot_addr[0] && !pot_addr[1]) {
    // Nothing at the MCP4461 addresses -- sweep the whole bus in case the
    // fitted parts differ from the schematic (different digipot family or
    // address base). Prints every ACKing address.
    Serial.print("  full I2C sweep 0x08-0x77:");
    int hits = 0;
    for (uint8_t a = 0x08; a <= 0x77; a++) {
      Wire.beginTransmission(a);
      if (Wire.endTransmission() == 0) { Serial.printf(" 0x%02X", a); hits++; }
    }
    Serial.println(hits ? "" : " (no devices ACK -- bus level or wiring)");
    if (!hits) {
      // Crossed-wire self-test: re-init with SDA/SCL swapped and retry the
      // four MCP4461 addresses. If they appear, keep running swapped.
      Wire.end();
      Wire.begin(PIN_SCL, PIN_SDA, 100000);
      int sw = 0;
      for (int i = 0; i < 4; i++) {
        uint16_t v;
        if (potRead(POT_SCAN[i], PREG_VW[0], &v) && v <= 0x100) {
          int chip = POT_SCAN[i] & 1;
          if (!pot_addr[chip]) pot_addr[chip] = POT_SCAN[i];
          sw++;
        }
      }
      if (sw) {
        Serial.println("  !!! SDA/SCL WIRES ARE CROSSED -- pots found with pins "
                       "swapped. Running swapped; uncross at next rewire.");
      } else {
        Wire.end();
        Wire.begin(PIN_SDA, PIN_SCL, 100000);
      }
    }
  }
}

// TCON explicit (POR default is already all-connected, but a floating-A1
// part that browned out deserves determinism), then wipers 0-2 to 5k.
bool potSetChip5k(int chip, bool verbose) {
  uint8_t a = pot_addr[chip];
  bool ok = potWrite(a, PREG_TCON0, 0x1FF) && potWrite(a, PREG_TCON1, 0x1FF);
  for (int w = 0; w < 3; w++) {
    uint16_t rb = 0xFFFF;
    ok &= potWrite(a, PREG_VW[w], WIPER_5K) &&
          potRead(a, PREG_VW[w], &rb) && rb == WIPER_5K;
    if (verbose)
      Serial.printf("  IC%d wiper%d (%s): set 0x%03X read 0x%03X %s\n",
                    chip + 1, w, POT_COIL[chip][w], WIPER_5K, rb,
                    rb == WIPER_5K ? "OK" : "FAIL");
  }
  return ok;
}

bool potSetAll5k() {
  bool all_ok = true;
  for (int chip = 0; chip < 2; chip++) {
    if (!pot_addr[chip]) { all_ok = false; continue; }
    all_ok &= potSetChip5k(chip, true);
  }
  return all_ok;
}

// SAFETY: coil op-amps overheat below 5k -- verify wipers every 5 s, force
// back on any deviation, and quietly reacquire a chip lost to brown-out.
// NV wipers are burned to 5k so POR is safe; this covers runtime drift.
void potGuard() {
  static uint32_t next_ms = 0;
  if (millis() < next_ms) return;
  next_ms = millis() + 5000;
  for (int chip = 0; chip < 2; chip++) {
    if (!pot_addr[chip]) {
      for (int i = 0; i < 4; i++) {
        uint16_t v;
        if ((POT_SCAN[i] & 1) == chip &&
            potRead(POT_SCAN[i], PREG_VW[0], &v) && v <= 0x100) {
          pot_addr[chip] = POT_SCAN[i];
          Serial.printf("[pot guard] IC%d reacquired at 0x%02X -> forcing 5k\n",
                        chip + 1, POT_SCAN[i]);
          potSetChip5k(chip, false);
          break;
        }
      }
      continue;
    }
    for (int w = 0; w < 3; w++) {
      uint16_t v;
      if (!potRead(pot_addr[chip], PREG_VW[w], &v)) {
        Serial.printf("[pot guard] IC%d lost (no ACK) -- reacquiring\n", chip + 1);
        pot_addr[chip] = 0;
        break;
      }
      if (v != WIPER_5K) {
        potWrite(pot_addr[chip], PREG_VW[w], WIPER_5K);
        Serial.printf("[pot guard] !! IC%d wiper%d was 0x%03X -> forced 0x100 (5k)\n",
                      chip + 1, w, v);
      }
    }
  }
}

// Persist 5k: copy current volatile setting into the NV wipers. EEPROM write
// cycle is ~5 ms/reg; blocked only if WP-bar is really strapped low
// ("Check the WP" note on the schematic -- meter it if this fails).
void potBurnNV() {
  for (int chip = 0; chip < 2; chip++) {
    if (!pot_addr[chip]) continue;
    for (int w = 0; w < 3; w++) {
      uint16_t vw = 0, nv = 0xFFFF;
      potRead(pot_addr[chip], PREG_VW[w], &vw);
      potWrite(pot_addr[chip], PREG_NVW[w], vw);
      delay(20);
      potRead(pot_addr[chip], PREG_NVW[w], &nv);
      Serial.printf("  IC%d NV wiper%d: 0x%03X %s\n", chip + 1, w, nv,
                    nv == vw ? "BURNED" : "FAIL (check WP strap)");
    }
  }
  Serial.println("power-cycle to verify POR loads 5k");
}

// =====================================================================
// MCT8316Z -- SPI BLDC driver for the Halbach spin motor (datasheet
// SLLSFH3: 16-bit frame B15=R/W(1=read) B14:9=addr B8=even-parity B7:0=data;
// SDO = 8 status bits then register data; capture on SCLK falling edge with
// idle-low clock -> SPI MODE1, <=5 MHz, nSCS high >=400 ns between words.
// Registers reset on power-up AND sleep. Control regs 0x04-0x06 have
// non-zero POR defaults -> used as the comms probe.)
// =====================================================================
#include <SPI.h>
const int PIN_MCT_CS   = 4;   // nSCS
const int PIN_MCT_SCLK = 5;
const int PIN_MCT_SDI  = 6;   // S3 MOSI -> MCT SDI
const int PIN_MCT_SDO  = 7;   // S3 MISO <- MCT SDO
const int PIN_MCT_SPD  = 21;  // PWM -> SPEED input (if module wired for PWM
                              // speed mode); 25 kHz, duty = speed command
const int PIN_MCT_NSLP = 42;  // nSLEEP: HIGH = awake. NOTE registers reset
                              // on sleep -- reconfigure after any wake
const int PIN_MCT_NFLT = 41;  // nFAULT, open-drain -> internal pull-up
const int PIN_MCT_FG   = 40;  // FGOUT, open-drain -> internal pull-up;
                              // edge-counted for electrical speed feedback

volatile uint32_t fg_edges = 0;
void IRAM_ATTR fgIsr() { fg_edges++; }

// FG frequency over the interval since the previous computation (Hz).
// Cached at 500 ms so the human line and the CSV column can both call it
// without corrupting each other's measurement window.
float fgHz() {
  static float val = 0.0f;
  static uint32_t next_ms = 0, last_ms = 0, last_edges = 0;
  uint32_t now = millis();
  if (now >= next_ms) {
    uint32_t edges = fg_edges;
    if (last_ms && now > last_ms)
      val = 1000.0f * (float)(edges - last_edges) / (float)(now - last_ms);
    last_ms = now;
    last_edges = edges;
    next_ms = now + 500;
  }
  return val;
}

// Latched fault reporter: on nFAULT falling, dump the three status registers.
void mctFaultWatch() {
  static bool was_ok = true;
  bool ok = digitalRead(PIN_MCT_NFLT);
  if (was_ok && !ok) {
    Serial.println("!! MCT8316Z nFAULT asserted -- status registers:");
    for (uint8_t a = 0; a <= 0x02; a++) {
      uint16_t r = mctXfer(true, a, 0x00);
      Serial.printf("   reg 0x%02X: stat 0x%02X data 0x%02X\n", a, r >> 8, r & 0xFF);
    }
  }
  if (!was_ok && ok) Serial.println("[mct] nFAULT cleared");
  was_ok = ok;
}

SPIClass mctspi(HSPI);
bool mct_begun = false;
uint16_t mct_duty = 0;        // 0..1023 at 10-bit LEDC

uint16_t mctFrame(bool rd, uint8_t addr, uint8_t data) {
  uint16_t w = ((uint16_t)rd << 15) | ((uint16_t)(addr & 0x3F) << 9) | data;
  uint16_t p = w ^ (w >> 8);
  p ^= p >> 4; p ^= p >> 2; p ^= p >> 1;
  return w | ((p & 1) << 8);          // set B8 so total ones are even
}

// Returns full 16-bit response: [15:8] STAT snapshot, [7:0] register data.
uint16_t mctXfer(bool rd, uint8_t addr, uint8_t data) {
  if (!mct_begun) {
    pinMode(PIN_MCT_CS, OUTPUT);
    digitalWrite(PIN_MCT_CS, HIGH);
    mctspi.begin(PIN_MCT_SCLK, PIN_MCT_SDO, PIN_MCT_SDI, -1);
    mct_begun = true;
  }
  mctspi.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE1));
  digitalWrite(PIN_MCT_CS, LOW);
  uint16_t r = mctspi.transfer16(mctFrame(rd, addr, data));
  digitalWrite(PIN_MCT_CS, HIGH);
  mctspi.endTransaction();
  delayMicroseconds(1);               // >=400 ns nSCS high between words
  return r;
}

void mctProbe() {
  Serial.println("--- MCT8316Z probe (regs 0x00-0x0C; 0x04-0x06 nonzero at POR) ---");
  bool any = false;
  for (uint8_t a = 0; a <= 0x0C; a++) {
    uint16_t r = mctXfer(true, a, 0x00);
    Serial.printf("  reg 0x%02X: stat 0x%02X data 0x%02X\n", a, r >> 8, r & 0xFF);
    if ((r & 0xFF) != 0x00 && (r & 0xFF) != 0xFF) any = true;
  }
  Serial.println(any ? "  MCT8316Z RESPONDING"
                     : "  no life -- check wiring (CS=4 SCLK=5 SDI=6 SDO=7) and VM power");
}

void mctSpeed(uint16_t duty) {       // 0..1023; PWM speed mode only
  static bool pwm_up = false;
  if (!pwm_up) { ledcAttach(PIN_MCT_SPD, 25000, 10); pwm_up = true; }
  mct_duty = duty > 1023 ? 1023 : duty;
  ledcWrite(PIN_MCT_SPD, mct_duty);
  Serial.printf("[mct] SPEED duty %u/1023\n", mct_duty);
}

// =====================================================================
// ADS1258 -- carried over from the proven bench sketch, CS-framed
// =====================================================================
const uint32_t SPI_HZ = 1000000;  // crystal+PLL fCLK 15.73M; limit is fCLK/2

// VREFP-VREFN = P2V5 - N2V5 = 5.0 V differential
const float VREF_VOLTS       = 5.0f;
const float CODE_FULLSCALE   = 7864320.0f;   // 0x780000
const float MON_COUNTS_PER_V = 786432.0f;
const float TEMP_UV_25C      = 168000.0f;
const float TEMP_UV_PER_C    = 394.0f;

const uint8_t CMD_CHDATA = 0x30, CMD_RREG = 0x40, CMD_WREG = 0x60,
              CMD_RESET  = 0xC0;
const uint8_t REG_MUXDIF = 0x03, REG_MUXSG0 = 0x04, REG_MUXSG1 = 0x05,
              REG_SYSRED = 0x06, REG_ID = 0x09;
const uint8_t REG_DEFAULT[10] = { 0x0A, 0x83, 0x00, 0x00, 0xFF,
                                  0xFF, 0x00, 0xFF, 0x00, 0x8B };

const uint8_t STAT_NEW = 0x80, STAT_OVF = 0x40, STAT_SUPPLY = 0x20;
const uint8_t CHID_AIN0 = 0x08, CHID_OFFSET = 0x18, CHID_VCC = 0x1A,
              CHID_TEMP = 0x1B, CHID_GAIN = 0x1C, CHID_REF = 0x1D;
const uint32_t WANT_MONITORS = (1ul << CHID_OFFSET) | (1ul << CHID_VCC) |
                               (1ul << CHID_TEMP) | (1ul << CHID_GAIN) |
                               (1ul << CHID_REF);
const uint32_t WANT_AIN = 0xFFFFul << CHID_AIN0;
const uint32_t POLL_CAP = 40000;

// AIN index -> net. Bakes in both schematic swaps (AIN4/5 "flipped" note,
// ANALOG_5/4 crossover at AIN10/11) so the display never lies.
const char *CH_NAME[16] = {
  "ISNS_0/c1", "ISNS_1/c2", "ISNS_2/c3", "ISNS_3/c4",
  "ISNS_5/c6", "ISNS_4/c5",
  "ANALOG_0", "ANALOG_1", "ANALOG_2", "ANALOG_3",
  "ANALOG_5", "ANALOG_4", "ANALOG_6", "ANALOG_7", "ANALOG_8", "ANALOG_9" };

bool adc_ok = false;
bool csv_mode = false;

uint8_t regRead(uint8_t addr) {
  SPI.beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(CMD_RREG | addr);
  uint8_t v = SPI.transfer(0x00);
  digitalWrite(PIN_CS, HIGH);   // CS edge also re-frames a desynced interface
  SPI.endTransaction();
  return v;
}

void regWrite(uint8_t addr, uint8_t val) {
  SPI.beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(CMD_WREG | addr);
  SPI.transfer(val);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

bool regWriteVerify(uint8_t addr, uint8_t val) {
  regWrite(addr, val);
  uint8_t rb = regRead(addr);
  if (rb != val)
    Serial.printf("  reg 0x%02X: wrote 0x%02X read 0x%02X FAIL\n", addr, val, rb);
  return rb == val;
}

uint8_t chanRead(int32_t *code) {
  SPI.beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(CMD_CHDATA);
  uint8_t st = SPI.transfer(0x00);
  int32_t v = (int32_t)(int8_t)SPI.transfer(0x00) << 16;
  v |= (int32_t)SPI.transfer(0x00) << 8;
  v |= SPI.transfer(0x00);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  *code = v;
  return st;
}

float codeToVolts(int32_t code) { return (float)code * VREF_VOLTS / CODE_FULLSCALE; }

void adcHardReset() {
  digitalWrite(PIN_RST, LOW);
  delay(1);
  digitalWrite(PIN_RST, HIGH);
  delay(10);
  SPI.beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_CS, LOW);
  SPI.transfer(CMD_RESET);      // belt and suspenders
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  delay(10);
}

uint32_t collect(uint32_t want, int32_t vals[32]) {
  uint32_t got = 0, ovf = 0, supply = 0;
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
  if (ovf)    Serial.printf("  !! OVF on %lu readings (|VIN| > 1.06*VREF)\n", ovf);
  if (supply) Serial.printf("  !! SUPPLY flag %lu times (AVDD-AVSS < ~4.3V -- is -5V present?)\n", supply);
  return got & want;
}

bool probeRegisters() {
  adcHardReset();
  int match = 0;
  uint8_t id = 0;
  Serial.print("  regs:");
  for (uint8_t a = 0; a < 10; a++) {
    uint8_t v = regRead(a);
    if (a == REG_ID) id = v;
    if (v == REG_DEFAULT[a]) match++;
    Serial.printf(" %02X", v);
  }
  Serial.printf("  (%d/10 default, ID=0x%02X want 0x8B)\n", match, id);
  return id == 0x8B || match >= 6;
}

// DRDY is a new wire on this board relative to the old bench -- prove it
// moves. Free-running conversions must produce edges within ~100 ms.
void drdyCheck() {
  uint32_t t0 = millis();
  bool level = digitalRead(PIN_DRDY), toggled = false;
  while (millis() - t0 < 100 && !toggled)
    toggled = digitalRead(PIN_DRDY) != level;
  Serial.printf("  DRDY wire: %s\n",
                toggled ? "toggling OK" : "STUCK (J2.9 open? START low?)");
}

bool selfTest() {
  Serial.println("--- ADS1258 self-test: internal monitors ---");
  if (!(regWriteVerify(REG_MUXDIF, 0x00) && regWriteVerify(REG_MUXSG0, 0x00) &&
        regWriteVerify(REG_MUXSG1, 0x00) && regWriteVerify(REG_SYSRED, 0x3D)))
    return false;
  int32_t v[32] = { 0 };
  if (collect(WANT_MONITORS, v) != WANT_MONITORS) {
    Serial.println("  monitors never reported -- START wire (J2.11)?");
    return false;
  }
  float off_v  = codeToVolts(v[CHID_OFFSET]);
  float vcc_v  = (float)v[CHID_VCC] / MON_COUNTS_PER_V;
  float temp_c = ((float)v[CHID_TEMP] * VREF_VOLTS / CODE_FULLSCALE * 1e6f
                  - TEMP_UV_25C) / TEMP_UV_PER_C + 25.0f;
  float gain   = (float)v[CHID_GAIN] / CODE_FULLSCALE;
  float ref_v  = (float)v[CHID_REF] / MON_COUNTS_PER_V;
  Serial.printf("  OFFSET %+0.4f V  %s\n", off_v, fabsf(off_v) < 0.005f ? "PASS" : "FAIL");
  Serial.printf("  VCC    %6.3f V  %s (AVDD-AVSS, want ~5.0)\n", vcc_v,
                (vcc_v > 4.3f && vcc_v < 5.5f) ? "PASS" : "FAIL -- check -5V rail");
  Serial.printf("  TEMP   %6.1f C  %s\n", temp_c,
                (temp_c > 0.0f && temp_c < 70.0f) ? "PASS" : "FAIL");
  Serial.printf("  GAIN   %6.4f    %s\n", gain,
                (gain > 0.95f && gain < 1.05f) ? "PASS" : "FAIL");
  Serial.printf("  REF    %6.3f V  %s (P2V5-N2V5, want ~5.0)\n", ref_v,
                fabsf(ref_v - VREF_VOLTS) < 0.5f ? "PASS" : "FAIL -- LDO rails");
  return true;
}

// Detect whether a line is externally driven: a driven pin reads the same
// with pull-up and pull-down; a floating pin follows the pull. The board's
// 4.7k I2C pull-ups to P3V3 beat the S3's ~45k internal pulls, so a healthy
// bus reads "driven HIGH" and a dead P3V3 or open wire reads FLOATING.
const char *lineState(int pin) {
  pinMode(pin, INPUT_PULLDOWN);
  delayMicroseconds(80);
  bool dn = digitalRead(pin);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(80);
  bool up = digitalRead(pin);
  pinMode(pin, INPUT);
  if (dn != up) return "FLOATING (open wire, or rail dead)";
  return dn ? "driven HIGH" : "driven LOW";
}

void wireDiag() {
  SPI.end();  // pinMode() would silently detach pins from the peripherals
  Wire.end();
  Serial.println("--- wire diag (driven = wire + power good; FLOATING = fix that line) ---");
  Serial.printf("  SDA  J1.3  : %s (expect driven HIGH via board 4.7k)\n", lineState(PIN_SDA));
  Serial.printf("  SCL  J1.1  : %s (expect driven HIGH via board 4.7k)\n", lineState(PIN_SCL));
  Serial.printf("  DRDY J2.9  : %s (powered ADS1258 actively drives)\n", lineState(PIN_DRDY));
  pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, LOW);   // DOUT only drives while selected
  delayMicroseconds(10);
  Serial.printf("  DOUT J2.20 : %s (with CS low: powered ADS1258 drives)\n", lineState(PIN_MISO));
  digitalWrite(PIN_CS, HIGH);
  Serial.println("  all four FLOATING -> P3V3 jumper (S3 3V3 -> J1.13) or harness seating/orientation");
  Wire.begin(PIN_SDA, PIN_SCL, 100000);
  SPI.begin(PIN_SCLK, PIN_MISO, PIN_MOSI, -1);
}

void setup() {
  Serial.begin(115200);
  // On a CDC build the port re-enumerates after reset; hold for the host so
  // the pot-scan lines aren't lost. Falls through on the UART bridge.
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 4000) delay(10);
  delay(300);
  Serial.println("\n=== ISS Router Rev2 bring-up (TN_ISSROUTER_BringupPinout_RevA) ===");

  if (ledcAttach(PIN_SPOOF, 5000, 12))
    ledcWrite(PIN_SPOOF, SPOOF_DUTIES[spoof_ix]);
  Serial.printf("spoof PWM on GPIO%d at 50%% -> ~0.83 V after the 10k/10k+1uF "
                "divider ('d' cycles 25/50/75)\n", PIN_SPOOF);

  pinMode(PIN_CS, OUTPUT);    digitalWrite(PIN_CS, HIGH);
  pinMode(PIN_RST, OUTPUT);   digitalWrite(PIN_RST, HIGH);
  pinMode(PIN_START, OUTPUT); digitalWrite(PIN_START, HIGH);  // free-run
  pinMode(PIN_DRDY, INPUT);
  pinMode(PIN_TRIG, OUTPUT);  digitalWrite(PIN_TRIG, LOW);
  pinMode(PIN_MCT_NSLP, OUTPUT); digitalWrite(PIN_MCT_NSLP, HIGH);  // awake
  pinMode(PIN_MCT_NFLT, INPUT_PULLUP);
  pinMode(PIN_MCT_FG, INPUT_PULLUP);
  attachInterrupt(PIN_MCT_FG, fgIsr, RISING);

  // --- step 1: digipots to nominal 5k ---
  Wire.begin(PIN_SDA, PIN_SCL, 100000);
  Serial.println("--- MCP4461 scan (A1 floats: even addr = IC1, odd = IC2) ---");
  potScan();
  potSetAll5k();

  // --- step 2: ADC comms ---
  SPI.begin(PIN_SCLK, PIN_MISO, PIN_MOSI, -1);  // CS is manual
  adc_ok = probeRegisters();
  if (adc_ok) {
    drdyCheck();
    selfTest();
    adc_ok = regWriteVerify(REG_MUXSG0, 0xFF) && regWriteVerify(REG_MUXSG1, 0xFF) &&
             regWriteVerify(REG_SYSRED, 0x3D);
    Serial.println("--- streaming 16 ch; 'p' pots, 'n' NV burn, 'c' CSV ---");
  } else {
    Serial.println("!! ADS1258 not responding -- check J2 SPI wires and -5V rail");
  }
  if (!adc_ok && !pot_addr[0] && !pot_addr[1]) wireDiag();  // both buses dead
}

void loop() {
  potGuard();        // safety: 5k enforced even while ADC is down
  mctFaultWatch();
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'p') { potScan(); potSetAll5k(); }
    if (c == 'n') potBurnNV();
    if (c == 'c') csv_mode = !csv_mode;
    if (c == 'a') { adc_ok = false; Serial.println("re-probing ADC"); }
    if (c == 'x') wireDiag();
    if (c == 't') {
      digitalWrite(PIN_TRIG, HIGH);
      delay(10);
      digitalWrite(PIN_TRIG, LOW);
      Serial.println("[trig] pulse fired on GPIO15");
    }
    if (c == 'm') mctProbe();
    if (c == 'R') {   // unlock regs, then clear fault: CTRL2A (0x04) bit0 W1C
      mctXfer(false, 0x03, 0x03);       // REG_LOCK = 011b: unlock writes
      uint16_t cur = mctXfer(true, 0x04, 0x00);
      mctXfer(false, 0x04, (cur & 0xFF) | 0x01);
      delay(100);
      uint16_t after = mctXfer(true, 0x00, 0x00);
      Serial.printf("[mct] unlock+CLR_FLT: IC_STAT 0x%02X, nFAULT %s\n",
                    after & 0xFF,
                    digitalRead(PIN_MCT_NFLT) ? "clear" : "STILL ASSERTED");
    }
    if (c == '+') mctSpeed(mct_duty + 64);
    if (c == '-') mctSpeed(mct_duty > 64 ? mct_duty - 64 : 0);
    if (c == '0') mctSpeed(0);
    if (c == 'w') {
      // Meter mode: sink SDA/SCL low against the board's 4.7k pull-ups.
      // Probe along the run: 0 V = intact to that point, 3.3 V = past the
      // break. Any key releases the lines and restores I2C.
      Wire.end();
      pinMode(PIN_SDA, OUTPUT); digitalWrite(PIN_SDA, LOW);
      pinMode(PIN_SCL, OUTPUT); digitalWrite(PIN_SCL, LOW);
      Serial.println("[meter mode] SDA+SCL driven LOW. Probe S3 pins 8/9, then"
                     " J1.3/J1.1: 0V=wire good, 3.3V=break. Any key to exit.");
      while (!Serial.available()) delay(50);
      Serial.read();
      Wire.begin(PIN_SDA, PIN_SCL, 100000);
      Serial.println("[meter mode] released, I2C restored");
    }
    if (c == 'W') {
      // Drive-HIGH variant: proves SDA/SCL continuity with no dependence on
      // the board's P3V3 rail. J1.3/J1.1 read 3.3 V iff the runs are intact.
      Wire.end();
      pinMode(PIN_SDA, OUTPUT); digitalWrite(PIN_SDA, HIGH);
      pinMode(PIN_SCL, OUTPUT); digitalWrite(PIN_SCL, HIGH);
      Serial.println("[meter mode HIGH] SDA+SCL driven 3.3V. J1.3/J1.1: 3.3V="
                     "wire good, 0V=break. Also read P3V3 across C4. Any key exits.");
      while (!Serial.available()) delay(50);
      Serial.read();
      Wire.begin(PIN_SDA, PIN_SCL, 100000);
      Serial.println("[meter mode] released, I2C restored");
    }
    if (c == 'd') {
      spoof_ix = (spoof_ix + 1) % 3;
      ledcWrite(PIN_SPOOF, SPOOF_DUTIES[spoof_ix]);
      Serial.printf("spoof duty %u%% -> ~%0.2f V after divider\n",
                    25 * (spoof_ix + 1), 3.3f * 0.5f * (spoof_ix + 1) * 0.25f);
    }
  }
  if (!adc_ok) {
    delay(1000);
    adc_ok = probeRegisters();
    if (adc_ok) {   // full re-init, or monitors read zero after recovery
      drdyCheck();
      selfTest();
      adc_ok = regWriteVerify(REG_MUXSG0, 0xFF) &&
               regWriteVerify(REG_MUXSG1, 0xFF) &&
               regWriteVerify(REG_SYSRED, 0x3D);
    }
    return;
  }

  int32_t v[32] = { 0 };
  uint32_t got = collect(WANT_AIN | WANT_MONITORS, v);
  if (!got) { Serial.println("no data -- reprobing"); adc_ok = false; return; }

  if (csv_mode) {           // one line per scan: millis, 16 ch, FG Hz
    Serial.printf("%lu", millis());
    for (int ch = 0; ch < 16; ch++)
      Serial.printf(",%0.4f", codeToVolts(v[CHID_AIN0 + ch]));
    Serial.printf(",%0.1f\n", fgHz());
    delay(50);
    return;
  }
  for (int ch = 0; ch < 16; ch++) {
    bool have = got & (1ul << (CHID_AIN0 + ch));
    Serial.printf("%-10s %+7.3f%c", CH_NAME[ch],
                  have ? codeToVolts(v[CHID_AIN0 + ch]) : 0.0f, have ? ' ' : '?');
    Serial.print((ch % 4 == 3) ? "\n" : " ");
  }
  Serial.printf("VCC %5.3f V  TEMP %4.1f C  FG %6.1f Hz  nFLT %s  spd %u/1023\n\n",
                (float)v[CHID_VCC] / MON_COUNTS_PER_V,
                ((float)v[CHID_TEMP] * VREF_VOLTS / CODE_FULLSCALE * 1e6f
                 - TEMP_UV_25C) / TEMP_UV_PER_C + 25.0f,
                fgHz(), digitalRead(PIN_MCT_NFLT) ? "ok" : "FAULT", mct_duty);
  delay(2000);
}
