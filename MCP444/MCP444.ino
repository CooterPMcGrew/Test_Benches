// MCP444x / MCP44XX quad I2C digipot bench checkout + bus diagnostics
// Board: ESP32 DevKit V1  |  GPIO21 SDA / GPIO22 SCL
//
// Each cycle: scan at 100 kHz, rescan at 10 kHz (catches weak pull-up /
// slow-edge failures), then a DC wiggle phase driving SDA and SCL to
// opposite static levels so chip pins 5/6 can be meter-probed for swaps
// or breaks. Once a digipot ACKs at either speed: verify wipers, then
// staircase for multimeter checks.

#include <Wire.h>

// RESET and WP are strapped to 3V3 on the board (no GPIO): chip POR/BOR
// covers reset, and NV writes stay enabled.
const int PIN_SDA = 21;
const int PIN_SCL = 22;

// MCP44XX volatile register addresses (TCON/STATUS shared across family)
const uint8_t REG_WIPER[4] = { 0x00, 0x01, 0x06, 0x07 };
const uint8_t REG_TCON0    = 0x04;
const uint8_t REG_STATUS   = 0x05;
const uint8_t REG_TCON1    = 0x0A;

// Command bits, OR'd into the high nibble register address
const uint8_t CMD_WRITE = 0x00;
const uint8_t CMD_READ  = 0x0C;

uint8_t  pot_addr  = 0;      // 0 = not discovered yet
uint16_t fullscale = 0x80;   // 0x80 = 7-bit part, 0x100 = 8-bit part

bool potWrite(uint8_t reg, uint16_t value) {
  Wire.beginTransmission(pot_addr);
  Wire.write((uint8_t)((reg << 4) | CMD_WRITE | ((value >> 8) & 0x03)));
  Wire.write((uint8_t)(value & 0xFF));
  return Wire.endTransmission() == 0;
}

bool potRead(uint8_t reg, uint16_t *out) {
  Wire.beginTransmission(pot_addr);
  Wire.write((uint8_t)((reg << 4) | CMD_READ));
  if (Wire.endTransmission(false) != 0) return false;  // repeated start
  if (Wire.requestFrom((int)pot_addr, 2) != 2) return false;
  uint8_t hi = Wire.read();
  uint8_t lo = Wire.read();
  *out = ((uint16_t)(hi & 0x03) << 8) | lo;
  return true;
}

// Re-init the bus on the given pins/speed and scan; returns first ACK in
// 0x28-0x2F, else 0. Trying both pin orders catches swapped SDA/SCL wiring.
uint8_t scanCfg(int sda, int scl, uint32_t hz, const char *tag) {
  Wire.end();
  // NOTE: never pinMode() SDA/SCL after Wire.begin() on esp32 core 3.x -
  // it detaches the pin from the I2C peripheral. External 4.7k pull-ups
  // carry the bus.
  Wire.begin(sda, scl, hz);
  uint8_t found = 0;
  Serial.printf("scan %s @%lukHz:", tag, hz / 1000);
  for (uint8_t a = 0x08; a < 0x78; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", a);
      if (a >= 0x28 && a <= 0x2F && found == 0) found = a;
    }
  }
  Serial.println(found ? "" : "  (no ACKs)");
  return found;
}

// ---- Bit-banged I2C: bypasses the ESP32 I2C peripheral and pin matrix ----
// Open-drain emulated by mode-switching: INPUT_PULLUP = released (external
// 4.7k pulls high), OUTPUT-low = driven. ~2.5 kHz with TQ = 100 us.

const uint32_t SOFT_TQ_US = 100;  // quarter-bit time

void softRelease(int pin) { pinMode(pin, INPUT_PULLUP); }
void softDrive(int pin)   { pinMode(pin, OUTPUT); digitalWrite(pin, LOW); }

// Returns false if the bus is stuck (either line low while released).
bool softStart() {
  softRelease(PIN_SDA);
  softRelease(PIN_SCL);
  delayMicroseconds(SOFT_TQ_US);
  if (!digitalRead(PIN_SDA) || !digitalRead(PIN_SCL)) return false;
  softDrive(PIN_SDA);
  delayMicroseconds(SOFT_TQ_US);
  softDrive(PIN_SCL);
  delayMicroseconds(SOFT_TQ_US);
  return true;
}

void softStop() {
  softDrive(PIN_SDA);
  delayMicroseconds(SOFT_TQ_US);
  softRelease(PIN_SCL);
  delayMicroseconds(SOFT_TQ_US);
  softRelease(PIN_SDA);
  delayMicroseconds(SOFT_TQ_US);
}

// Clock one bit out; returns SDA level sampled while SCL is high.
bool softBit(bool b) {
  if (b) softRelease(PIN_SDA); else softDrive(PIN_SDA);
  delayMicroseconds(SOFT_TQ_US);
  softRelease(PIN_SCL);
  delayMicroseconds(SOFT_TQ_US);
  bool sampled = digitalRead(PIN_SDA);
  delayMicroseconds(SOFT_TQ_US);
  softDrive(PIN_SCL);
  delayMicroseconds(SOFT_TQ_US);
  return sampled;
}

// Address a device write-mode; true if it pulled SDA low for ACK.
bool softProbe(uint8_t addr) {
  if (!softStart()) return false;
  uint8_t frame = (uint8_t)(addr << 1);
  for (int i = 7; i >= 0; i--) softBit((frame >> i) & 1);
  bool ack = !softBit(true);  // release SDA; slave drives low to ACK
  softStop();
  return ack;
}

uint8_t softScan() {
  uint8_t found = 0;
  Serial.print("scan BITBANG @2.5kHz:");
  for (uint8_t a = 0x08; a < 0x78; a++) {
    if (softProbe(a)) {
      Serial.printf(" 0x%02X", a);
      if (a >= 0x28 && a <= 0x2F && found == 0) found = a;
    }
  }
  Serial.println(found ? "" : "  (no ACKs)");
  return found;
}

// Drive SDA/SCL as plain GPIO to opposite DC levels so the far end of each
// line can be verified with a meter at the chip: pin 6 = SDA, pin 5 = SCL.
void wigglePhase() {
  Wire.end();
  pinMode(PIN_SDA, OUTPUT);
  pinMode(PIN_SCL, OUTPUT);

  // Asymmetric on purpose: SDA low 8 s, SCL low 2 s. A meter on chip pin 6
  // (SDA) should read LOW most of the cycle; chip pin 5 (SCL) only blips
  // low. Reversed duty = swapped lines; constant level = open circuit.
  digitalWrite(PIN_SDA, LOW);
  digitalWrite(PIN_SCL, HIGH);
  Serial.println("[wiggle] SDA=0V SCL=3.3V for 8 s");
  delay(8000);

  digitalWrite(PIN_SDA, HIGH);
  digitalWrite(PIN_SCL, LOW);
  Serial.println("[wiggle] SDA=3.3V SCL=0V for 2 s");
  delay(2000);

  // Back to hardware I2C; Wire.begin() re-attaches the pins itself
  Wire.begin(PIN_SDA, PIN_SCL, 100000);
}

// Characterize the discovered part and write/read-verify all four wipers.
// No software resolution detect: the wiper register stores out-of-range
// codes unclamped, so write/readback can't distinguish 7- from 8-bit.
// Part on this bench is marked MCP4441-502: 7-bit, 129 taps, 5k.
bool verifyPot() {
  uint16_t rb = 0;
  Serial.printf("Full-scale code: 0x%X (MCP4441: 7-bit, 129 taps)\n", fullscale);

  uint16_t status, tcon0, tcon1;
  if (potRead(REG_STATUS, &status)) Serial.printf("STATUS = 0x%03X\n", status);
  if (potRead(REG_TCON0, &tcon0))   Serial.printf("TCON0  = 0x%03X (0x1FF = all terminals connected)\n", tcon0);
  if (potRead(REG_TCON1, &tcon1))   Serial.printf("TCON1  = 0x%03X\n", tcon1);

  bool all_ok = true;
  for (int w = 0; w < 4; w++) {
    uint16_t test_val = (uint16_t)(fullscale / 4 * (w + 1) - 1);
    bool ok = potWrite(REG_WIPER[w], test_val) &&
              potRead(REG_WIPER[w], &rb) && rb == test_val;
    all_ok &= ok;
    Serial.printf("Wiper %d: wrote 0x%03X read 0x%03X  %s\n",
                  w, test_val, rb, ok ? "PASS" : "FAIL");
  }
  Serial.println(all_ok ? ">>> CONTROL VERIFIED - staircase starting <<<"
                        : ">>> READBACK MISMATCH - back to scanning <<<");
  return all_ok;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== MCP444x bench checkout + bus diag ===");

  Wire.begin(PIN_SDA, PIN_SCL, 100000);
}

void loop() {
  if (pot_addr == 0) {
    // Hardware I2C first, then bit-banged GPIO I2C. A part that only
    // answers the bit-banged scan implicates the ESP32 I2C peripheral,
    // not the chip or wiring.
    uint8_t found = scanCfg(PIN_SDA, PIN_SCL, 100000, "hw");
    if (found == 0) found = scanCfg(PIN_SDA, PIN_SCL, 10000, "hw");
    if (found == 0) {
      Wire.end();
      uint8_t soft = softScan();
      Wire.begin(PIN_SDA, PIN_SCL, 100000);
      if (soft != 0) {
        Serial.println("!!! Chip ACKs BIT-BANGED I2C but not hardware I2C.");
        Serial.println("!!! ESP32 I2C peripheral/pin-matrix is the fault, not the MCP.");
      }
    }
    if (found == 0) {
      Serial.printf("Idle levels: SDA=%d SCL=%d (want 1/1)\n",
                    digitalRead(PIN_SDA), digitalRead(PIN_SCL));
      wigglePhase();
      return;
    }
    pot_addr = found;
    Serial.printf("Digipot at 0x%02X\n", pot_addr);
    if (!verifyPot()) { pot_addr = 0; return; }
  }

  // 5-step staircase, 15 s per step, all four wipers together. Each step
  // reads the wiper register back and prints the expected W<->B resistance
  // computed FROM THE READBACK, for side-by-side meter verification.
  // R_WB(code) = R_AB * code/128 + R_W. Nominal R_AB = 5k, R_W = 75R;
  // this part's R_AB measured ~4% low, so meter should track ~4% under.
  const uint32_t R_AB_NOM = 5000;
  const uint32_t R_W_NOM  = 75;
  static const uint8_t PCT[5] = { 0, 25, 50, 75, 100 };
  for (int s = 0; s < 5; s++) {
    uint16_t code = (uint16_t)(((uint32_t)fullscale * PCT[s]) / 100);
    bool ok = true;
    uint16_t rb[4] = { 0, 0, 0, 0 };
    for (int w = 0; w < 4; w++) {
      ok &= potWrite(REG_WIPER[w], code);
      ok &= potRead(REG_WIPER[w], &rb[w]);
    }
    uint32_t expect = (R_AB_NOM * rb[0]) / fullscale + R_W_NOM;
    Serial.printf("[staircase] %3u%%  wrote 0x%03X  readback %03X/%03X/%03X/%03X"
                  "  expect ~%lu ohms W<->B  %s\n",
                  PCT[s], code, rb[0], rb[1], rb[2], rb[3],
                  expect, ok ? "" : "(I2C ERROR)");
    if (!ok) { pot_addr = 0; return; }  // device vanished - back to scanning
    delay(15000);
  }
}
