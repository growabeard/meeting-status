/*
 * meeting_status_led - show the host machine's meeting status on an RGB LED.
 *
 * Wiring (common-cathode RGB LED, the usual kind):
 *
 *   Arduino D2  --[220R]-- B leg
 *   Arduino D3  --[220R]-- G leg
 *   Arduino D4  --[220R]-- R leg
 *   Arduino GND ---------- common (longest) leg
 *
 * Leg order varies between parts. Set PIN_R/PIN_G/PIN_B below to match yours.
 *
 * For a common-anode LED, wire the common leg to 5V instead and set
 * COMMON_ANODE to 1 below.
 *
 * D2/D3/D4 are not hardware PWM pins, so this sketch generates PWM in
 * software from a Timer2 interrupt instead - see the SOFT PWM section. That
 * buys pin freedom at the cost of owning Timer2, which means tone() will not
 * work alongside it. The pins must stay within D2..D7: all three live in one
 * port register, so the interrupt can set them with a single write, and D0/D1
 * are left alone because they are the serial lines.
 *
 * On a board that is not an ATmega328P (Uno, Nano, Pro Mini) the register
 * names below will not compile. Move the LED to three hardware PWM pins and
 * revert to analogWrite in that case.
 *
 * Serial protocol, 115200 baud, newline-terminated, case insensitive:
 *
 *   host -> board          board -> host
 *   STAT FREE              OK FREE
 *   STAT UPCOMING          OK UPCOMING
 *   STAT MEETING           OK MEETING
 *   STAT BUSY              OK BUSY
 *   STAT OFF               OK OFF
 *   PING                   PONG
 *   BRIGHT 0..255          OK BRIGHT <n>
 *   RGB 255 128 0          OK RGB         (manual test, until next STAT)
 *   ?                      STATE <name>
 *
 * The host sends a STAT line every few seconds as a heartbeat. If nothing
 * arrives for STALE_MS the board decides the host is gone and breathes blue,
 * so a dead script looks different from "free".
 *
 * Blue is for the case where someone is there to see it. After STALE_DARK_MS
 * of silence the board gives up and goes dark instead, because a dock keeps
 * supplying USB power with no host attached - so an undocked laptop overnight
 * would otherwise leave the thing pulsing blue at an empty desk until morning.
 * The board still answers "?" with STALE while dark, so the distinction is
 * kept where it costs nothing.
 */

#include <Arduino.h>
#include <string.h>
#include <ctype.h>

// ---- configuration -------------------------------------------------------

// Which pin carries which leg. Identified by driving one channel at a time
// and looking: on this board D2 is blue and D4 is red, not the other way
// round. If your LED shows the wrong colours, swap these numbers rather than
// rewiring - they must stay within D2..D7 (see the SOFT PWM note above).
const uint8_t PIN_R = 4;
const uint8_t PIN_G = 3;
const uint8_t PIN_B = 2;

#define COMMON_ANODE 0        // 1 if the LED's common leg goes to 5V

const unsigned long BAUD      = 115200;
const unsigned long STALE_MS  = 20000;  // no host traffic for this long = stale
// Long enough that a daemon restart or a brief sleep still shows blue while you
// are sitting there, short enough that it does not glow all night.
const unsigned long STALE_DARK_MS = 600000UL;  // 10 min stale = go dark
const unsigned int  FADE_MS   = 8;      // ms per fade step between colours
const uint8_t       FADE_STEP = 4;      // channel units per fade step

uint8_t brightness = 255;               // global scale, 0..255

// ---- states --------------------------------------------------------------

enum Effect { SOLID, BREATHE_SLOW };

struct Status {
  const char *name;
  uint8_t r, g, b;
  Effect effect;
};

// Some colours are pre-dimmed where a raw channel would otherwise dominate;
// most cheap RGB LEDs are far brighter in green than in red or blue. FREE is
// deliberately not dimmed - it is pure green, no red or blue mixed in.
const Status STATUSES[] = {
  { "FREE",     0,   255, 0,   SOLID         },  // pure green - come on in
  { "UPCOMING", 255, 90,  0,   BREATHE_SLOW  },  // amber    - starting soon
  { "MEETING",  255, 0,   0,   SOLID         },  // red      - on a call
  { "BUSY",     150, 0,   200, SOLID         },  // purple   - focus / DND
  { "OFF",      0,   0,   0,   SOLID         },  // dark     - host said stop
  { "STALE",    0,   40,  120, BREATHE_SLOW  },  // dim blue - no host
};
const uint8_t N_STATUSES = sizeof(STATUSES) / sizeof(STATUSES[0]);

const uint8_t IDX_FREE  = 0;
const uint8_t IDX_OFF   = 4;
const uint8_t IDX_STALE = 5;

// ---- runtime state -------------------------------------------------------

uint8_t  currentIdx = IDX_STALE;   // what the host last told us
bool     stale      = true;
bool     darkened   = false;       // stale so long the board stopped showing it
bool     manual     = false;       // an RGB command is overriding the status
uint8_t  manualR = 0, manualG = 0, manualB = 0;

// Target and actual colour, before brightness/breathing is applied.
uint8_t tgtR = 0, tgtG = 0, tgtB = 0;
uint8_t curR = 0, curG = 0, curB = 0;

unsigned long lastHostMs = 0;
unsigned long lastFadeMs = 0;

char line[48];
uint8_t lineLen = 0;

// ---- SOFT PWM ------------------------------------------------------------
//
// Timer2 fires at 31.25 kHz. Each tick advances an 8-bit counter, so every
// channel gets a full 256-step cycle 122 times a second - above the flicker
// threshold. The ISR body is deliberately tiny: three compares and one port
// write, because it runs 31250 times a second.

volatile uint8_t dutyR = 0, dutyG = 0, dutyB = 0;

// All three pins must share PORTD (D0..D7) for the single-write trick.
#define LED_MASK ((1 << PIN_R) | (1 << PIN_G) | (1 << PIN_B))

ISR(TIMER2_COMPA_vect) {
  static uint8_t tick = 0;
  uint8_t on = 0;
  // duty 0 stays dark (nothing is > 255 at tick 255); duty 255 is 255/256 on.
  if (dutyR > tick) on |= (1 << PIN_R);
  if (dutyG > tick) on |= (1 << PIN_G);
  if (dutyB > tick) on |= (1 << PIN_B);
#if COMMON_ANODE
  on = ~on & LED_MASK;
#endif
  PORTD = (PORTD & ~LED_MASK) | on;   // preserves D0/D1, the serial pins
  tick++;
}

void softPwmBegin() {
  // CTC mode, prescaler 8, top 63  ->  16 MHz / 8 / 64 = 31.25 kHz.
  TCCR2A = (1 << WGM21);
  TCCR2B = (1 << CS21);
  OCR2A  = 63;
  TIMSK2 = (1 << OCIE2A);
}

void writeChannel(uint8_t pin, uint8_t value) {
  // Writes are single-byte, so the ISR cannot catch one half-updated.
  if (pin == PIN_R)      dutyR = value;
  else if (pin == PIN_G) dutyG = value;
  else if (pin == PIN_B) dutyB = value;
}

uint8_t scale(uint8_t value, uint8_t factor) {
  return (uint8_t)(((uint16_t)value * factor) / 255);
}

// Triangle wave 0..255 with the given period, used for the breathing effect.
uint8_t breathe(unsigned long periodMs) {
  unsigned long phase = millis() % periodMs;
  unsigned long half = periodMs / 2;
  uint8_t level = (phase < half)
      ? (uint8_t)((phase * 255UL) / half)
      : (uint8_t)(((periodMs - phase) * 255UL) / half);
  // Keep a floor so the LED never fully disappears mid-breath.
  return 60 + scale(level, 195);
}

void setTargetFromState() {
  if (manual) {
    tgtR = manualR; tgtG = manualG; tgtB = manualB;
    return;
  }
  if (stale && darkened) {
    tgtR = 0; tgtG = 0; tgtB = 0;
    return;
  }
  const Status &s = STATUSES[stale ? IDX_STALE : currentIdx];
  tgtR = s.r; tgtG = s.g; tgtB = s.b;
}

void stepToward(uint8_t &value, uint8_t target) {
  if (value == target) return;
  int16_t delta = (int16_t)target - (int16_t)value;
  if (delta > FADE_STEP)       value += FADE_STEP;
  else if (delta < -FADE_STEP) value -= FADE_STEP;
  else                         value = target;
}

void render() {
  uint8_t mod = 255;
  if (!manual) {
    if (!(stale && darkened) &&
        STATUSES[stale ? IDX_STALE : currentIdx].effect == BREATHE_SLOW) {
      mod = breathe(4000);
    }
  }
  uint8_t gain = scale(mod, brightness);
  writeChannel(PIN_R, scale(curR, gain));
  writeChannel(PIN_G, scale(curG, gain));
  writeChannel(PIN_B, scale(curB, gain));
}

int8_t statusIndex(const char *name) {
  for (uint8_t i = 0; i < N_STATUSES; i++) {
    if (strcasecmp(name, STATUSES[i].name) == 0) return (int8_t)i;
  }
  return -1;
}

// ---- command handling ----------------------------------------------------

void handleLine(char *buf) {
  // Trim trailing whitespace/CR.
  for (int i = strlen(buf) - 1; i >= 0 && isspace((unsigned char)buf[i]); i--) {
    buf[i] = '\0';
  }
  if (buf[0] == '\0') return;

  char *cmd = strtok(buf, " \t");
  if (!cmd) return;

  if (strcasecmp(cmd, "STAT") == 0) {
    char *arg = strtok(NULL, " \t");
    int8_t idx = arg ? statusIndex(arg) : -1;
    if (idx < 0 || idx == IDX_STALE) {
      Serial.println(F("ERR unknown state"));
      return;
    }
    lastHostMs = millis();
    stale = false;
    darkened = false;
    manual = false;
    currentIdx = (uint8_t)idx;
    setTargetFromState();
    Serial.print(F("OK "));
    Serial.println(STATUSES[currentIdx].name);

  } else if (strcasecmp(cmd, "PING") == 0) {
    lastHostMs = millis();
    stale = false;
    darkened = false;
    Serial.println(F("PONG"));

  } else if (strcasecmp(cmd, "BRIGHT") == 0) {
    char *arg = strtok(NULL, " \t");
    if (!arg) { Serial.println(F("ERR need 0..255")); return; }
    long v = atol(arg);
    brightness = (uint8_t)constrain(v, 0, 255);
    Serial.print(F("OK BRIGHT "));
    Serial.println(brightness);

  } else if (strcasecmp(cmd, "RGB") == 0) {
    char *a = strtok(NULL, " \t");
    char *b = strtok(NULL, " \t");
    char *c = strtok(NULL, " \t");
    if (!a || !b || !c) { Serial.println(F("ERR need r g b")); return; }
    manual = true;
    manualR = (uint8_t)constrain(atol(a), 0, 255);
    manualG = (uint8_t)constrain(atol(b), 0, 255);
    manualB = (uint8_t)constrain(atol(c), 0, 255);
    lastHostMs = millis();
    stale = false;
    darkened = false;
    setTargetFromState();
    Serial.println(F("OK RGB"));

  } else if (strcmp(cmd, "?") == 0 || strcasecmp(cmd, "STATE") == 0) {
    Serial.print(F("STATE "));
    Serial.println(STATUSES[stale ? IDX_STALE : currentIdx].name);

  } else {
    Serial.println(F("ERR unknown command"));
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (lineLen > 0) {
        line[lineLen] = '\0';
        handleLine(line);
        lineLen = 0;
      }
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = ch;
    } else {
      lineLen = 0;              // overlong line, drop it
    }
  }
}

// ---- sketch --------------------------------------------------------------

void setup() {
  pinMode(PIN_R, OUTPUT);
  pinMode(PIN_G, OUTPUT);
  pinMode(PIN_B, OUTPUT);
  softPwmBegin();

  Serial.begin(BAUD);

  // Power-on self test: red, green, blue, then settle into "no host yet".
  const uint8_t test[3][3] = { {255,0,0}, {0,255,0}, {0,0,255} };
  for (uint8_t i = 0; i < 3; i++) {
    writeChannel(PIN_R, test[i][0]);
    writeChannel(PIN_G, test[i][1]);
    writeChannel(PIN_B, test[i][2]);
    delay(200);
  }
  writeChannel(PIN_R, 0); writeChannel(PIN_G, 0); writeChannel(PIN_B, 0);

  lastHostMs = millis();
  currentIdx = IDX_FREE;
  stale = true;
  darkened = false;
  setTargetFromState();

  Serial.println(F("READY meeting_status_led"));
}

void loop() {
  readSerial();

  if (!stale && (millis() - lastHostMs) > STALE_MS) {
    stale = true;
    manual = false;
    darkened = false;
    setTargetFromState();
  }

  // Still nobody after a long while: stop advertising it and fade out.
  if (stale && !darkened && (millis() - lastHostMs) > STALE_DARK_MS) {
    darkened = true;
    setTargetFromState();
  }

  unsigned long now = millis();
  if (now - lastFadeMs >= FADE_MS) {
    lastFadeMs = now;
    stepToward(curR, tgtR);
    stepToward(curG, tgtG);
    stepToward(curB, tgtB);
  }

  render();
}
