// ISS Router checkout: LOWER + UPPER boards on one ESP32-S3, simultaneously.
// Per board: MCP4461 quad digipots (control, readback, address report) and
// an ADS1258 (digital-side aliveness). Upper board adds four IR sensor
// outputs checked for a nominal analog level. Status on the harness 20x4
// I2C LCD; full detail on serial. Plug in, power, read the screen.
// Board reference: ../TN_ISSROUTER_BringupPinout_RevA.md. This harness's
// pin map (below), not the TN section 5 plan, is what is wired.
//
// ESP32-S3 DevKitC-1. LOWER board = left header, UPPER = right header.
//   shared I2C   GPIO8 SDA / GPIO9 SCL  <-> all four digipots + LCD backpack
//   LOWER ADS1258  SCK 12  MISO 13  MOSI 11  CS 10 (also strapped on-board)
//                  START 4  ADCRST 5  DRDY 6
//   UPPER ADS1258  SCK 42  MISO 41  MOSI 40  (no CS: strapped low on-board,
//                  which is WHY it needs its own SPI host -- two always-
//                  selected chips cannot share DOUT)
//                  START 39  ADCRST 38  DRDY 21
//   UPPER IR1..4   GPIO15 16 17 18  (S3 ADC2; max 3.1 V -- divider anything
//                  that can exceed 3.3 V. ADC2 is unusable if Wi-Fi is ever
//                  enabled; this rig has no radio.)
//   3V3 + GND to both boards' digital domains and the LCD.
//
// Both ADS1258s need BOTH supplies (DVDD and the +/-2.5 V analog rails)
// before the SPI interface answers at all -- "no comms" with the -5 V
// input absent is expected, not a fault. DRDY/START behavior is reported
// as informational (a converter free-running with START gating it proves
// clock, rails and both GPIO wires).
//
// Digipot address plan: MCP4461 = 0101 + A2 A1 A0 -> 0x28-0x2F. The lower
// pair is strapped 0x2C/0x2D; the upper pair must be strapped elsewhere in
// the range. Two chips on ONE address both ACK and both accept writes, so
// a collision is invisible to a scan except as a missing device: 4/4 in
// range is the only PASS, and "2/4 with the upper board plugged in" means
// the upper pair collides with the lower pair.
//
// Digipots are tested in place: wiper codes found on entry are restored
// after the pattern test, and NV/EEPROM registers are never written.
// Deep diagnostics (bit-bang I2C, SPI mode probe, wiggle phases) live in
// the MCP444 and ADS1258 bench sketches; this one is a go/no-go panel.

#include <SPI.h>
#include <Wire.h>

const int PIN_SDA = 8;
const int PIN_SCL = 9;

const int L_SCK = 12, L_MISO = 13, L_MOSI = 11, L_CS = 10;
const int L_START = 4, L_RST = 5, L_DRDY = 6;

const int U_SCK = 42, U_MISO = 41, U_MOSI = 40;   // CS strapped on-board
const int U_START = 39, U_RST = 38, U_DRDY = 21;

const int PIN_IR[4] = { 15, 16, 17, 18 };
// Nominal window for a healthy IR channel. Placeholders until the sensor's
// real quiescent output is characterized -- set from the datasheet/bench.
const uint32_t IR_MIN_MV = 200;
const uint32_t IR_MAX_MV = 3000;
// A floating S3 pin swings ~3 V between internal pull-up and pull-down; a
// driven sensor output barely moves. Threshold sits between the two.
const uint32_t IR_FLOAT_SWING_MV = 1500;

const uint8_t LOWER_POT_ADDR[2] = { 0x2C, 0x2D };   // measured 2026-08-24Z
const int     POTS_EXPECTED = 4;

const uint32_t I2C_HZ      = 100000;   // PCF8574 backpacks top out at 100 kHz
const uint32_t SPI_HZ_NORM = 1000000;  // <= fCLK/2 for any legal ADS1258 clock
const uint32_t SPI_HZ_SLOW = 100000;   // fallback catches marginal SCLK wiring

// ---------------- LCD: HD44780 behind a PCF8574 backpack -------------------
// Standard backpack bit map: P0=RS P1=RW P2=E P3=backlight P4-7=DB4-7.
const uint8_t LCD_COLS = 20;
const uint8_t LCD_ROWS = 4;
const uint8_t LCD_RS = 0x01, LCD_EN = 0x04, LCD_BL = 0x08;

uint8_t lcd_addr = 0;   // 0 = no LCD acquired
bool    lcd_err  = false;

bool lcdBus(uint8_t b) {
  Wire.beginTransmission(lcd_addr);
  Wire.write(b);
  bool ok = Wire.endTransmission() == 0;
  if (!ok) lcd_err = true;   // forces re-init next cycle (power blip recovery)
  return ok;
}

void lcdNib(uint8_t nib, uint8_t flags) {
  uint8_t b = (uint8_t)(nib << 4) | flags | LCD_BL;
  lcdBus(b | LCD_EN);
  delayMicroseconds(1);      // E pulse width; I2C framing already exceeds spec
  lcdBus(b & (uint8_t)~LCD_EN);
  delayMicroseconds(50);     // > 37 us instruction time
}

void lcdCmd(uint8_t c)  { lcdNib(c >> 4, 0);      lcdNib(c & 0x0F, 0); }
void lcdData(uint8_t d) { lcdNib(d >> 4, LCD_RS); lcdNib(d & 0x0F, LCD_RS); }

bool lcdInit(uint8_t addr) {
  lcd_addr = addr;
  lcd_err = false;
  if (!lcdBus(LCD_BL)) { lcd_addr = 0; return false; }
  delay(50);                              // HD44780 power-up wait
  lcdNib(0x3, 0); delay(5);               // "initialization by instruction",
  lcdNib(0x3, 0); delayMicroseconds(150); // works from any half-byte state
  lcdNib(0x3, 0);
  lcdNib(0x2, 0);                         // -> 4-bit
  lcdCmd(0x28);                           // 4-bit, 2 lines, 5x8
  lcdCmd(0x0C);                           // display on, cursor off
  lcdCmd(0x01); delay(2);                 // clear (slow command)
  lcdCmd(0x06);                           // entry: increment, no shift
  return !lcd_err;
}

void lcdLine(uint8_t row, const char *text) {
  static const uint8_t ROW_ADDR[4] = { 0x00, 0x40, 0x14, 0x54 };
  lcdCmd(0x80 | ROW_ADDR[row]);
  bool end = false;
  for (uint8_t i = 0; i < LCD_COLS; i++) {   // pad with spaces: no stale chars
    if (!end && text[i] == '\0') end = true;
    lcdData(end ? ' ' : (uint8_t)text[i]);
  }
}

// ---------------- I2C bus scan / classification -----------------------------
// MCP4461 = 0x28-0x2F. PCF8574 = 0x20-0x27, PCF8574A = 0x38-0x3F; neither
// overlaps the pot range, so one scan sorts everything.
struct Scan { uint8_t lcd; uint8_t pot[8]; int npot; };
void i2cScan(Scan *s);   // explicit prototypes: the Arduino builder's
                         // auto-prototypes land above the struct definitions

void i2cScan(Scan *s) {
  s->lcd = 0;
  s->npot = 0;
  int acks = 0;
  Serial.print("  I2C ACKs:");
  for (uint8_t a = 0x08; a < 0x78; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() != 0) continue;
    Serial.printf(" 0x%02X", a);
    acks++;
    if (a >= 0x28 && a <= 0x2F) {
      if (s->npot < 8) s->pot[s->npot] = a;
      s->npot++;
    } else if (((a >= 0x20 && a <= 0x27) || (a >= 0x38 && a <= 0x3F)) &&
               s->lcd == 0) {
      s->lcd = a;
    }
  }
  Serial.println(acks ? "" : " none (check SDA/SCL wires and pull-ups)");
}

bool isLowerPot(uint8_t addr) {
  return addr == LOWER_POT_ADDR[0] || addr == LOWER_POT_ADDR[1];
}

// ---------------- MCP4461 quad digipot --------------------------------------
const uint8_t POT_WIPER[4]  = { 0x00, 0x01, 0x06, 0x07 };  // volatile wipers
const uint8_t POT_STATUS    = 0x05;
const uint8_t POT_CMD_READ  = 0x0C;

bool potWrite(uint8_t addr, uint8_t reg, uint16_t v) {
  Wire.beginTransmission(addr);
  Wire.write((uint8_t)((reg << 4) | ((v >> 8) & 0x03)));
  Wire.write((uint8_t)(v & 0xFF));
  return Wire.endTransmission() == 0;
}

bool potRead(uint8_t addr, uint8_t reg, uint16_t *out) {
  Wire.beginTransmission(addr);
  Wire.write((uint8_t)((reg << 4) | POT_CMD_READ));
  if (Wire.endTransmission(false) != 0) return false;   // repeated start
  if (Wire.requestFrom((int)addr, 2) != 2) return false;
  uint8_t hi = Wire.read(), lo = Wire.read();
  *out = (uint16_t)((hi & 0x03) << 8) | lo;
  return true;
}

// Patterns stay <= 0x80 -- legal for 7- and 8-bit parts (MCP4461: 8-bit,
// 257 taps, full scale 0x100), and analog-safe if the rails are up.
const uint16_t POT_PATTERNS[2] = { 0x25, 0x5A };

// Control + readback on all four volatile wipers, both patterns, then
// restore the codes found on entry -- leave the board exactly as found.
bool potVerify(uint8_t addr) {
  const char *side = isLowerPot(addr) ? "lower" : "upper";
  uint16_t initial[4];
  for (int w = 0; w < 4; w++) {
    if (!potRead(addr, POT_WIPER[w], &initial[w])) {
      Serial.printf("  pot 0x%02X (%s): wiper %d initial read FAIL\n", addr, side, w);
      return false;
    }
  }
  uint16_t status = 0xFFFF;
  potRead(addr, POT_STATUS, &status);
  Serial.printf("  pot 0x%02X (%s): STATUS=0x%03X wipers-as-found %03X/%03X/%03X/%03X\n",
                addr, side, status, initial[0], initial[1], initial[2], initial[3]);

  bool ok = true;
  for (int w = 0; w < 4; w++) {
    for (int p = 0; p < 2; p++) {
      uint16_t rb = 0xFFFF;
      bool step = potWrite(addr, POT_WIPER[w], POT_PATTERNS[p]) &&
                  potRead(addr, POT_WIPER[w], &rb) && rb == POT_PATTERNS[p];
      if (!step)
        Serial.printf("  pot 0x%02X wiper %d: wrote 0x%03X read 0x%03X FAIL\n",
                      addr, w, POT_PATTERNS[p], rb);
      ok &= step;
    }
    if (!potWrite(addr, POT_WIPER[w], initial[w])) {
      Serial.printf("  pot 0x%02X wiper %d: RESTORE FAILED\n", addr, w);
      ok = false;
    }
  }
  Serial.printf("  pot 0x%02X (%s): control+readback %s\n", addr, side,
                ok ? "PASS" : "FAIL");
  return ok;
}

// ---------------- ADS1258, one instance per board -----------------------------
const uint8_t CMD_RREG    = 0x40;
const uint8_t CMD_WREG    = 0x60;
const uint8_t REG_CONFIG1 = 0x01;
const uint8_t REG_MUXSG0  = 0x04;
const uint8_t REG_ID      = 0x09;
const uint8_t REG_DEFAULT[10] = { 0x0A, 0x83, 0x00, 0x00, 0xFF,
                                  0xFF, 0x00, 0xFF, 0x00, 0x8B };

struct Adc {
  const char *tag;            // "L" / "U"
  SPIClass   *spi;
  int sck, miso, mosi, cs;    // cs = -1: strapped low on the board
  int start, rst, drdy;
  volatile uint32_t edges;    // DRDY CHANGE count, ISR-shared
  uint32_t hz;                // SPI clock that last answered
  bool alive, wr_ok;
  uint8_t id;
  int match;
  char note[8];               // DRDY summary for the LCD row
};
// Explicit prototypes: the Arduino builder's auto-prototypes land above
// the struct definition and fail for every Adc* parameter.
void adcSelect(Adc *a, bool sel);
uint8_t adcRegRead(Adc *a, uint8_t r);
void adcRegWrite(Adc *a, uint8_t r, uint8_t v);
bool adcRegVerify(Adc *a, uint8_t r, uint8_t v);
void adcHwReset(Adc *a);
bool adcProbe(Adc *a);
bool adcTest(Adc *a);
void adcPathDiag(Adc *a, char *line, size_t n);
void drdyObserve(Adc *a);
uint32_t drdyEdgesIn(Adc *a, uint32_t ms);
void adcRow(Adc *a, char *line, size_t n);
void adcPins(Adc *a);

SPIClass spiUpper(HSPI);   // S3 third SPI host, matrixed to the right header
Adc adcL = { "L", &SPI,      L_SCK, L_MISO, L_MOSI, L_CS,
             L_START, L_RST, L_DRDY, 0, SPI_HZ_NORM, false, false, 0, 0, "" };
Adc adcU = { "U", &spiUpper, U_SCK, U_MISO, U_MOSI, -1,
             U_START, U_RST, U_DRDY, 0, SPI_HZ_NORM, false, false, 0, 0, "" };

void IRAM_ATTR drdyIsr(void *arg) { ((Adc *)arg)->edges++; }

void adcSelect(Adc *a, bool sel) {
  if (a->cs >= 0) digitalWrite(a->cs, sel ? LOW : HIGH);
}

uint8_t adcRegRead(Adc *a, uint8_t r) {
  a->spi->beginTransaction(SPISettings(a->hz, MSBFIRST, SPI_MODE0));
  adcSelect(a, true);
  a->spi->transfer(CMD_RREG | r);
  uint8_t v = a->spi->transfer(0x00);
  adcSelect(a, false);
  a->spi->endTransaction();
  return v;
}

void adcRegWrite(Adc *a, uint8_t r, uint8_t v) {
  a->spi->beginTransaction(SPISettings(a->hz, MSBFIRST, SPI_MODE0));
  adcSelect(a, true);
  a->spi->transfer(CMD_WREG | r);
  a->spi->transfer(v);
  adcSelect(a, false);
  a->spi->endTransaction();
}

bool adcRegVerify(Adc *a, uint8_t r, uint8_t v) {
  adcRegWrite(a, r, v);
  uint8_t rb = adcRegRead(a, r);
  if (rb != v)
    Serial.printf("  ADC-%s reg 0x%02X: wrote 0x%02X read 0x%02X FAIL\n",
                  a->tag, r, v, rb);
  return rb == v;
}

// Hardware reset via the ADCRST wire needs no working SPI and no command
// clocking -- recovery of first resort. Spec minimum low time is 2 fCLK;
// 100 us covers a clock as slow as the 0.1 MHz spec floor.
void adcHwReset(Adc *a) {
  digitalWrite(a->rst, LOW);
  delayMicroseconds(100);
  digitalWrite(a->rst, HIGH);
  delay(10);
}

// With CS strapped low, power-up can leave the interface mid-frame; SCLK
// idle > 4096 fCLK resyncs it (SBAS297D). Covers CS-wired boards too.
void spiResync() { delay(50); }

bool adcProbe(Adc *a) {
  spiResync();
  adcHwReset(a);
  a->match = 0;
  Serial.printf("  ADC-%s regs @%lukHz:", a->tag, (unsigned long)(a->hz / 1000));
  for (uint8_t r = 0; r < 10; r++) {
    uint8_t v = adcRegRead(a, r);
    if (r == REG_ID) a->id = v;
    if (v == REG_DEFAULT[r]) a->match++;
    Serial.printf(" %02X", v);
  }
  // "Responding" = ID exact, or most defaults match (all-00/FF = dead bus)
  Serial.printf("  (%d/10 default, ID=0x%02X want 0x8B)\n", a->match, a->id);
  return a->id == 0x8B || a->match >= 6;
}

bool adcTest(Adc *a) {
  const uint32_t speeds[2] = { SPI_HZ_NORM, SPI_HZ_SLOW };
  a->alive = false;
  a->wr_ok = false;
  a->id = 0;
  for (int s = 0; s < 2 && !a->alive; s++) {
    a->hz = speeds[s];
    a->alive = adcProbe(a);
  }
  if (!a->alive) return false;
  if (a->hz != SPI_HZ_NORM)
    Serial.printf("  !! ADC-%s answers only at 100 kHz -- check SCLK integrity\n", a->tag);
  // Write/readback both bit polarities, then hardware-reset so the chip is
  // left at power-on defaults rather than holding a test pattern.
  a->wr_ok = adcRegVerify(a, REG_MUXSG0, 0x55) && adcRegVerify(a, REG_MUXSG0, 0xAA);
  adcHwReset(a);
  return a->wr_ok;
}

// Weak-pull discrimination: a driven pin reads the same under pull-up and
// pull-down; a floating pin follows the pull. Leaves the pin as plain INPUT.
const char *pinDrivenState(int pin) {
  pinMode(pin, INPUT_PULLDOWN);
  delayMicroseconds(50);
  bool down = digitalRead(pin);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(50);
  bool up = digitalRead(pin);
  pinMode(pin, INPUT);
  return (down != up) ? "FLOAT" : (down ? "HI" : "LO");
}

uint32_t drdyEdgesIn(Adc *a, uint32_t ms) {
  a->edges = 0;
  delay(ms);
  return a->edges;
}

// Register probe failed but which SPI leg is bad? Isolate it using only
// side channels -- no readback needed:
//   MISO drive test: DOUT must drive while the chip is selected and
//   tri-state when deselected. FLOAT = CS never selects the chip (open CS
//   or missing strap) or MISO wire open.
//   Blind write test: CONFIG1 DRATE 11->00 slows the conversion rate ~13x;
//   DRDY moving proves CS+SCLK+MOSI land commands with readback dead.
void adcPathDiag(Adc *a, char *line, size_t n) {
  a->spi->end();   // pinMode() silently detaches matrix pins; reattached below
  const char *sel, *desel;
  if (a->cs >= 0) {
    adcSelect(a, true);
    delayMicroseconds(20);
    sel = pinDrivenState(a->miso);
    adcSelect(a, false);
    delayMicroseconds(20);
    desel = pinDrivenState(a->miso);
  } else {
    sel = pinDrivenState(a->miso);   // strapped: always "selected"
    desel = "n/a";
  }
  a->spi->begin(a->sck, a->miso, a->mosi, -1);
  Serial.printf("  [diag %s] MISO/DOUT drive: selected %s, deselected %s\n",
                a->tag, sel, desel);

  bool landed = false;
  uint32_t before = drdyEdgesIn(a, 200);
  if (before) {
    adcRegWrite(a, REG_CONFIG1, 0x80);   // DRATE 11 -> 00
    delay(5);
    uint32_t after = drdyEdgesIn(a, 200);
    adcHwReset(a);                       // defaults restored either way
    landed = after * 4 < before;         // expect ~13x drop; 4x = decisive
    Serial.printf("  [diag %s] blind DRATE write: DRDY %lu -> %lu edges/200ms %s\n",
                  a->tag, (unsigned long)before, (unsigned long)after,
                  landed ? "-- commands LAND: CS+SCLK+MOSI good"
                         : "-- no effect: command path dead");
  } else {
    Serial.printf("  [diag %s] DRDY quiet: blind-write test unavailable\n", a->tag);
  }

  if (landed)
    snprintf(line, n, "ADC-%s cmdsOK MISObad", a->tag);
  else if (strcmp(sel, "FLOAT") == 0)
    snprintf(line, n, "ADC-%s check CS/MISO", a->tag);
  else
    snprintf(line, n, "ADC-%s chk SCLK/MOSI", a->tag);
  Serial.printf("  [diag %s] verdict: %s\n", a->tag, line);
}

// Informational: a converter free-running with START gating it proves
// clock, rails and both GPIO wires. Fills a->note for the LCD row.
void drdyObserve(Adc *a) {
  const uint32_t WIN_MS = 200;
  digitalWrite(a->start, HIGH);
  delay(5);
  uint32_t run = drdyEdgesIn(a, WIN_MS);
  if (run == 0) {
    const char *st = pinDrivenState(a->drdy);
    snprintf(a->note, sizeof a->note, strcmp(st, "FLOAT") == 0 ? "FLOAT" : "idle%s", st);
    Serial.printf("  DRDY-%s: no edges in %lu ms, line %s\n",
                  a->tag, (unsigned long)WIN_MS, st);
    return;
  }
  digitalWrite(a->start, LOW);
  delay(5);
  uint32_t halted = drdyEdgesIn(a, WIN_MS);
  digitalWrite(a->start, HIGH);
  uint32_t hz = run / 2 * 1000 / WIN_MS;   // CHANGE counts both edges
  snprintf(a->note, sizeof a->note, halted == 0 ? "gateOK" : "gateNO");
  Serial.printf("  DRDY-%s: ~%lu Hz with START high; START low -> %lu edges %s\n",
                a->tag, (unsigned long)hz, (unsigned long)halted,
                halted == 0 ? "(gates: START+DRDY wires good)" : "(does NOT gate)");
}

void adcRow(Adc *a, char *line, size_t n) {
  if (a->alive && a->wr_ok)
    snprintf(line, n, "ADC-%s %02X PASS %s", a->tag, a->id, a->note);
  else if (a->alive)
    snprintf(line, n, "ADC-%s %02X WR FAIL", a->tag, a->id);
  else
    adcPathDiag(a, line, n);   // refine "no comms" to a wire
}

// ---------------- IR sensor outputs (S3 ADC) ---------------------------------
// Float check first (pull-up vs pull-down reading), then a 16-sample
// calibrated average with pulls off. If the core strips the pulls when it
// re-attaches the ADC, both readings agree and the float check simply
// never fires -- a graceful degradation, not a false FAIL.
bool irRead(int pin, uint32_t *mv, bool *floating) {
  pinMode(pin, INPUT_PULLUP);
  delay(2);
  uint32_t up = analogReadMilliVolts(pin);
  pinMode(pin, INPUT_PULLDOWN);
  delay(2);
  uint32_t down = analogReadMilliVolts(pin);
  pinMode(pin, INPUT);
  delay(2);
  uint32_t acc = 0;
  for (int i = 0; i < 16; i++) acc += analogReadMilliVolts(pin);
  *mv = acc / 16;
  uint32_t swing = up > down ? up - down : down - up;
  *floating = swing > IR_FLOAT_SWING_MV;
  return !*floating && *mv >= IR_MIN_MV && *mv <= IR_MAX_MV;
}

// ---------------- top level --------------------------------------------------
void adcPins(Adc *a) {
  if (a->cs >= 0) { pinMode(a->cs, OUTPUT); digitalWrite(a->cs, HIGH); }
  pinMode(a->rst, OUTPUT);   digitalWrite(a->rst, HIGH);
  pinMode(a->start, OUTPUT); digitalWrite(a->start, HIGH);
  pinMode(a->drdy, INPUT);
  attachInterruptArg(digitalPinToInterrupt(a->drdy), drdyIsr, a, CHANGE);
  a->spi->begin(a->sck, a->miso, a->mosi, -1);   // CS is manual GPIO / strap
}

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2000) {}   // native USB CDC enumeration
  Serial.println("\n=== ISS Router checkout: LOWER + UPPER (4x MCP4461, 2x ADS1258, 4x IR) ===");

  adcPins(&adcL);
  adcPins(&adcU);
  analogReadResolution(12);
  Wire.begin(PIN_SDA, PIN_SCL, I2C_HZ);
}

void loop() {
  static uint32_t cycle = 0;
  static char lines[4][LCD_COLS + 1];
  Serial.printf("\n--- cycle %lu ---\n", (unsigned long)++cycle);

  Scan s;
  i2cScan(&s);

  // (Re)acquire the LCD when its address changes or a bus write failed --
  // survives hot-plug and display power blips.
  if (s.lcd != lcd_addr || (s.lcd && lcd_err)) {
    if (s.lcd && lcdInit(s.lcd)) {
      Serial.printf("  LCD acquired at 0x%02X\n", s.lcd);
      lcdLine(0, "ROUTER CHECKOUT");   // instant sign of life at power-up
    } else {
      lcd_addr = 0;
    }
  } else if (!s.lcd) {
    lcd_addr = 0;
  }

  // ---- digipots: verify every one found (first four), then the count
  //      verdict. Only 4/4 is a PASS -- see the address-collision note above.
  bool pots_ok = true;
  int nverify = s.npot < POTS_EXPECTED ? s.npot : POTS_EXPECTED;
  int nlower = 0;
  for (int p = 0; p < nverify; p++) {
    pots_ok &= potVerify(s.pot[p]);
    if (isLowerPot(s.pot[p])) nlower++;
  }
  if (s.npot < POTS_EXPECTED)
    Serial.printf("  pots: %d/%d found (%d lower, %d other). If the UPPER board "
                  "is plugged in, its pair collides with the lower pair's "
                  "addresses -- restrap its A0/A1.\n",
                  s.npot, POTS_EXPECTED, nlower, nverify - nlower);
  else if (s.npot > POTS_EXPECTED)
    Serial.printf("  !! %d devices ACK in the pot range (expected %d)\n",
                  s.npot, POTS_EXPECTED);
  char hex[4][3];
  const char *slot[4];
  for (int i = 0; i < 4; i++) {
    if (i < s.npot) { snprintf(hex[i], sizeof hex[i], "%02X", s.pot[i]); slot[i] = hex[i]; }
    else slot[i] = "--";
  }
  char pstat[6];
  if (s.npot != POTS_EXPECTED) snprintf(pstat, sizeof pstat, "%d/%d", s.npot, POTS_EXPECTED);
  else snprintf(pstat, sizeof pstat, pots_ok ? "OK" : "FAIL");
  snprintf(lines[0], sizeof lines[0], "POT %s %s %s %s %4s",
           slot[0], slot[1], slot[2], slot[3], pstat);

  // ---- ADCs
  adcTest(&adcL);
  drdyObserve(&adcL);
  adcRow(&adcL, lines[1], sizeof lines[1]);
  adcTest(&adcU);
  drdyObserve(&adcU);
  adcRow(&adcU, lines[2], sizeof lines[2]);

  // ---- IR channels (upper board)
  bool ir_ok = true;
  char irv[4][4];
  for (int i = 0; i < 4; i++) {
    uint32_t mv;
    bool flt;
    bool ok = irRead(PIN_IR[i], &mv, &flt);
    ir_ok &= ok;
    if (flt) snprintf(irv[i], sizeof irv[i], "flt");
    else     snprintf(irv[i], sizeof irv[i], "%.1f", mv / 1000.0f);
    Serial.printf("  IR%d (GPIO%d): %lu mV %s\n", i + 1, PIN_IR[i], (unsigned long)mv,
                  flt ? "FLOATING (no sensor?)"
                      : ok ? "PASS" : "FAIL (outside nominal window)");
  }
  snprintf(lines[3], sizeof lines[3], "IR%s %s %s %s %s",
           irv[0], irv[1], irv[2], irv[3], ir_ok ? "OK" : "!!");

  Serial.printf("  >>> %s | %s | %s | %s <<<\n",
                lines[0], lines[1], lines[2], lines[3]);

  // Nothing at all on the far end -> a prompt instead of FAIL rows. The
  // scan re-runs forever, so plugging in is enough (no reset needed); the
  // incrementing counter proves the panel is alive and retrying.
  if (s.npot == 0 && !adcL.alive && !adcU.alive) {
    snprintf(lines[0], sizeof lines[0], "ROUTER CHECKOUT");
    snprintf(lines[1], sizeof lines[1], "Please connect");
    snprintf(lines[2], sizeof lines[2], "lower + upper boards");
    snprintf(lines[3], sizeof lines[3], "auto-scan #%lu", (unsigned long)cycle);
  }

  if (lcd_addr) {
    for (int base = 0; base < 4; base += LCD_ROWS) {
      for (int r = 0; r < LCD_ROWS && base + r < 4; r++)
        lcdLine((uint8_t)r, lines[base + r]);
      delay(2500);
      if (lcd_err) break;   // display vanished mid-write; rescan sooner
    }
  } else {
    delay(2500);
  }
}
