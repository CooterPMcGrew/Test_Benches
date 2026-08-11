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

#include <Arduino.h>
#include <Wire.h>

constexpr uint8_t PIN_SDA = 8, PIN_SCL = 9;
constexpr uint8_t PIN_NFAULT = 4, PIN_SPEED = 5, PIN_DIR = 6, PIN_DRVOFF = 7,
                  PIN_FG = 10;
constexpr uint8_t MCF_ADDR = 0x01;  // 7-bit default target ID
constexpr uint32_t I2C_HZ = 100000;

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
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      line[n] = 0;
      n = 0;
      if (!line[0]) continue;
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
        Serial.printf("DRVOFF=%lu\n", (unsigned long)a);
      } else if (!strcmp(cmd, "dir") && nf >= 2) {
        digitalWrite(PIN_DIR, a ? HIGH : LOW);
        Serial.printf("DIR=%lu\n", (unsigned long)a);
      } else if (!strcmp(cmd, "speed") && nf >= 2) {
        if (a > 1023) a = 1023;
        ledcWrite(0, a);
        Serial.printf("SPEED duty=%lu/1023\n", (unsigned long)a);
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
