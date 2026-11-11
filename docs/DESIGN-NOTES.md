# Design notes

Why this works the way it does, and the approaches that turned out not to work.
Most of these were found the hard way.

## Detection

### Microphone: capture, not permission, not connection

The mic signal reads CoreAudio's per-process properties
(`kAudioProcessPropertyIsRunningInput`, macOS 14.2+) through `ctypes`. No
third-party dependency and no permission of its own.

What it reacts to, precisely:

- **Live capture only.** Not microphone *permission* — an app can hold mic
  access for months without registering here. Not a mic being *connected* —
  plugging in a headset adds an idle device and changes nothing.
- **Capture, not playback.** This is the subtle one. A USB or Bluetooth headset
  is a single CoreAudio device carrying both input and output streams, and the
  obvious property to use, `kAudioDevicePropertyDeviceIsRunningSomewhere`, is
  device-wide with no input scope. Using it means music through a headset reads
  as a meeting. The per-process API separates the two: a playing-but-not-
  capturing process reports `capturing=0, playing=1` and is ignored.
- **Minus known noise.** Siri, dictation and some accessibility daemons open
  the mic without a meeting; they live in `proc_ignore`.

On macOS older than 14.2 the per-process API is absent and the code falls back
to the device-wide flag, which has the headset flaw above. `--probe` reports
which path is in use.

The four-character selector codes are easy to get wrong and fail silently when
they are. The correct ones are `piri` (input running) and `piro` (output
running) — both lowercase. `pirn`, `pirI` and `pirO` all return "not
available", which looks identical to "nothing is capturing".

### Meeting app playing audio

Covers the meetings the mic signal cannot see: a listen-only Zoom webinar where
you never get mic access, or a call where the host muted everyone. Only apps
matching `meeting_apps` count.

Playback must persist for `meeting_app_hold` seconds (20 by default). Without
that wait every Slack notification chime would read as a meeting — Slack is on
the list precisely because huddles run through it, and it also pings all day.
Verified both ways: a 40-second tone fires the signal and clears the instant it
stops; a 3-second chime never fires.

Browsers are deliberately absent from the default list. A browser plays
everything, so including Chrome means being in a meeting all day. The cost is
that browser-based Zoom and Meet are not caught by this signal — though they
are caught by the mic signal whenever you have mic access.

This needs the per-process API, because the device-wide flag cannot say *which*
app is playing. On older macOS the signal is simply off.

### Calendar

Read through EventKit via PyObjC. Events are skipped when they are all-day,
cancelled, marked Free, declined, or match `event_ignore`.

The declined check matters with Exchange: a declined invitation stays on the
calendar marked Busy, so without it a meeting you turned down lights the LED
red. `event_ignore` carries patterns like `\bdue\b` for the deadline reminders
people park on the calendar as Busy blocks.

EventKit only sees accounts added to Calendar.app. For Exchange or Microsoft
365 that means adding the account under System Settings → Internet Accounts;
once it is there this works with no code changes.

### Precedence

Override, mic capture, calendar in progress, meeting app audio, DND, calendar
upcoming, free. Mic and calendar both resolve to `MEETING`, so their relative
order only decides which reason string is logged — the calendar is checked
first because it can name the meeting.

## Dead ends

Recorded so they are not retried.

**AppleScript to Calendar.app.** Dead. Even `count of calendars` returns
`-1712 AppleEvent timed out` after two minutes, cold or warm. This drove the
move to EventKit.

**AppleScript to Outlook.** Dead on the current Outlook for Mac (the Hx
engine), which has no AppleScript support at all. Only the classic build
answers.

**Outlook's local store.** `Outlook 15 Profiles/Main Identity/Data/
Outlook.sqlite` exists but belongs to the classic build and goes stale once Hx
takes over. There is no readable local calendar database for the new Outlook.

**Teams and Slack presence from the apps themselves.** Both keep it in local
databases with no stable format; scraping breaks on every update. The mic
signal gets the same answer more reliably and with no maintenance.

**`~/Library/Accounts/Accounts4.sqlite`.** Needs Full Disk Access, which is a
heavy permission for a status light.

**Focus / DND.** `~/Library/DoNotDisturb/DB/Assertions.json` is the documented
location but is empty on current macOS. The code returns `None` when it cannot
read it and the signal is skipped silently rather than guessing.

## Two traps worth knowing

**TCC prompts need the main thread's run loop.** Requesting calendar access
from a background polling thread never completes — the prompt cannot appear, so
the completion handler never fires and the request hangs until it times out.
The fix is to request on the main thread at startup while pumping
`NSRunLoop.runUntilDate_`.

A launchd agent cannot display a prompt at all. The daemon therefore checks
`sys.stdin.isatty()` and only requests access when something is attached to
prompt on; otherwise it logs that the calendar signal is off and carries on.
Without that guard, startup under launchd stalls for two minutes waiting for a
dialog nobody can see.

**`flush()` on a serial port can hang forever.** `pyserial`'s `flush()` maps to
`termios.tcdrain()`, which blocks indefinitely when the far end stops consuming
bytes — an unpowered or crashed board hangs the daemon. The fix is to never
call it and rely on `write_timeout` instead.

## The sketch

**Software PWM.** The LED here is wired to D2/D3/D4, none of which are hardware
PWM pins on an ATmega328P (PWM lives on 3, 5, 6, 9, 10, 11). `analogWrite` on a
non-PWM pin collapses to on/off, which loses brightness control, the breathing
effect and every colour that is not a corner of the RGB cube.

So the sketch drives PWM itself from a Timer2 compare interrupt at 31.25 kHz.
An 8-bit counter advances each tick, giving every channel a 256-step cycle 122
times a second — above the flicker threshold. The ISR body is three compares
and one port write, because it runs 31250 times a second:

```c
PORTD = (PORTD & ~LED_MASK) | on;
```

All three pins must live in one port register for that single write, which is
why they are restricted to `D2..D7`. D0 and D1 are excluded because they are
the serial lines, and the mask is written so they are never touched.

The cost: the sketch owns Timer2, so `tone()` will not work alongside it. On a
non-ATmega328P board the register names will not compile — move the LED to
three hardware PWM pins and revert to `analogWrite`.

**Non-blocking.** No `delay()` in `loop()`. Fades, breathing and the stale
watchdog all run off `millis()` so serial is never missed.

**Leg order.** Pin-to-colour is set by `PIN_R` / `PIN_G` / `PIN_B` and varies
by part. The build in this repo matches an LED wired D2=blue, D3=green, D4=red,
identified by driving one channel at a time and looking at it.
