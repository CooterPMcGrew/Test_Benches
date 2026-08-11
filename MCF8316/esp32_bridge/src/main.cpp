// ESP32-S3 USB-serial <-> I2C bridge for MCF8316C1-Q1 bring-up.
//
// Implements the MCx83xx 24-bit control-word I2C protocol (TI SLLA662):
//   CW[23]    OP_R/W (1 = read)
//   CW[22]    CRC_EN (0 here)
//   CW[21:20] DLEN (01 = 32-bit, the only length used)
//   CW[19:16] MEM_SEC, CW[15:12] MEM_PAGE, CW[11:0] MEM_ADDR
// Register addresses are passed on the CLI as the combined 20-bit
// SEC/PAGE/ADDR value (as printed in the datasheet register map).
//
// Serial CLI (115200, newline-terminated):
//   r <hexaddr>            read 32-bit register
//   w <hexaddr> <hexval>   write 32-bit register
//   scan                   I2C bus scan
//   fault                  read nFAULT pin level
//   drvoff <0|1>           drive DRVOFF pin (1 = outputs disabled)
//   dir <0|1>              direction pin
//   speed <0..1023>        20 kHz PWM duty on SPEED pin (0 = off/brake-idle)
//   pins                   report control pin states
//
// DEAD-MAN SWITCH: whenever SPEED != 0 or DRVOFF is released, the host must
// send any command at least every DEADMAN_MS or the bridge autonomously
// asserts DRVOFF and zeroes SPEED. A hung host can never leave the motor
// energized.

#include <Arduino.h>
#include <Wire.h>

constexpr uint8_t PIN_SDA = 8, PIN_SCL = 9;
constexpr uint8_t PIN_NFAULT = 4, PIN_SPEED = 5, PIN_DIR = 6, PIN_DRVOFF = 7,
                  PIN_FG = 10;
constexpr uint8_t MCF_ADDR = 0x01;  // 7-bit default target ID
constexpr uint32_t I2C_HZ = 100000;
constexpr uint32_t DEADMAN_MS = 3000;

static uint32_t g_lastCmdMs = 0;
static bool g_energized = false;   // DRVOFF released or speed nonzero
static bool g_deadmanTripped = false;

// FG pulse counting for hardware speed telemetry
static volatile uint32_t g_fgCount = 0;
static uint32_t g_fgLastCount = 0;
static uint32_t g_fgLastMicros = 0;
static void IRAM_ATTR fgIsr() { g_fgCount++; }

static bool mcfWrite32(uint32_t reg, uint32_t val, uint8_t addr = MCF_ADDR) {
  uint32_t cw = (0u << 23) | (0u << 22) | (1u << 20) | (reg & 0xFFFFF);
  Wire.beginTransmission(addr);
  Wire.write((cw >> 16) & 0xFF);
  Wire.write((cw >> 8) & 0xFF);
  Wire.write(cw & 0xFF);
  // data LSB-first per SLLA662
  for (int i = 0; i < 4; i++) Wire.write((val >> (8 * i)) & 0xFF);
  return Wire.endTransmission() == 0;
}

static bool mcfRead32(uint32_t reg, uint32_t &val, uint8_t addr = MCF_ADDR) {
  uint32_t cw = (1u << 23) | (0u << 22) | (1u << 20) | (reg & 0xFFFFF);
  Wire.beginTransmission(addr);
  Wire.write((cw >> 16) & 0xFF);
  Wire.write((cw >> 8) & 0xFF);
  Wire.write(cw & 0xFF);
  if (Wire.endTransmission(false) != 0) return false;  // repeated start
  if (Wire.requestFrom((int)addr, 4) != 4) return false;
  val = 0;
  for (int i = 0; i < 4; i++) val |= ((uint32_t)Wire.read()) << (8 * i);
  return true;
}

static void cmdScan() {
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("found 0x%02X\n", a);
      found++;
    }
  }
  Serial.printf("scan done, %d device(s)\n", found);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_NFAULT, INPUT);   // board provides pull-up to AVDD
  pinMode(PIN_FG, INPUT);       // board provides pull-up to AVDD
  attachInterrupt(PIN_FG, fgIsr, RISING);
  g_fgLastMicros = micros();
  pinMode(PIN_DIR, OUTPUT);
  digitalWrite(PIN_DIR, LOW);
  pinMode(PIN_DRVOFF, OUTPUT);
  digitalWrite(PIN_DRVOFF, HIGH);  // start with outputs DISABLED
  ledcSetup(0, 20000, 10);          // channel 0, 20 kHz, 10-bit
  ledcAttachPin(PIN_SPEED, 0);
  ledcWrite(0, 0);
  Wire.begin(PIN_SDA, PIN_SCL, I2C_HZ);
  delay(100);
  Serial.println("MCF8316C1 bridge ready (DRVOFF asserted; outputs disabled)");
}

void loop() {
  static char line[64];
  static size_t n = 0;
  if (g_energized && (millis() - g_lastCmdMs > DEADMAN_MS)) {
    digitalWrite(PIN_DRVOFF, HIGH);
    ledcWrite(0, 0);
    g_energized = false;
    g_deadmanTripped = true;
    Serial.println("DEADMAN: DRVOFF asserted, speed 0");
  }
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      line[n] = 0;
      n = 0;
      if (!line[0]) continue;
      g_lastCmdMs = millis();
      if (g_deadmanTripped) {
        Serial.println("NOTE: deadman had tripped since last command");
        g_deadmanTripped = false;
      }
      char cmd[16];
      uint32_t a = 0, v = 0;
      int nf = sscanf(line, "%15s %lx %lx", cmd, (unsigned long *)&a,
                      (unsigned long *)&v);
      if (!strcmp(cmd, "r") && nf >= 2) {
        uint32_t val;
        if (mcfRead32(a, val))
          Serial.printf("R[%05lX] = %08lX\n", (unsigned long)a,
                        (unsigned long)val);
        else
          Serial.println("ERR read nack");
      } else if (!strcmp(cmd, "w") && nf >= 3) {
        Serial.printf(mcfWrite32(a, v) ? "W[%05lX] <= %08lX ok\n"
                                       : "ERR write nack\n",
                      (unsigned long)a, (unsigned long)v);
      } else if (!strcmp(cmd, "scan")) {
        cmdScan();
      } else if (!strcmp(cmd, "fault")) {
        Serial.printf("nFAULT=%d (0 = fault active)\n", digitalRead(PIN_NFAULT));
      } else if (!strcmp(cmd, "drvoff") && nf >= 2) {
        digitalWrite(PIN_DRVOFF, a ? HIGH : LOW);
        g_energized = (a == 0);
        Serial.printf("DRVOFF=%lu\n", (unsigned long)a);
      } else if (!strcmp(cmd, "dir") && nf >= 2) {
        digitalWrite(PIN_DIR, a ? HIGH : LOW);
        Serial.printf("DIR=%lu\n", (unsigned long)a);
      } else if (!strcmp(cmd, "speed") && nf >= 2) {
        if (a > 1023) a = 1023;
        ledcWrite(0, a);
        if (a > 0) g_energized = true;
        Serial.printf("SPEED duty=%lu/1023\n", (unsigned long)a);
      } else if (!strcmp(cmd, "fg")) {
        // FG pulse frequency since the last "fg" query (hardware speed)
        uint32_t now = micros();
        uint32_t cnt = g_fgCount;
        uint32_t dc = cnt - g_fgLastCount;
        float dt = (now - g_fgLastMicros) / 1e6f;
        g_fgLastCount = cnt;
        g_fgLastMicros = now;
        Serial.printf("FG %.2f Hz (%lu edges / %.3f s)\n",
                      dt > 0 ? dc / dt : 0.0f, (unsigned long)dc, dt);
      } else if (!strcmp(cmd, "pins")) {
        Serial.printf("nFAULT=%d FG=%d\n", digitalRead(PIN_NFAULT),
                      digitalRead(PIN_FG));
      } else {
        Serial.println("cmds: r <a> | w <a> <v> | scan | fault | drvoff <0|1> "
                       "| dir <0|1> | speed <0-1023> | pins");
      }
    } else if (n < sizeof(line) - 1) {
      line[n++] = c;
    }
  }
}
