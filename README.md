# meeting-status

A macOS background agent that works out whether you are in a meeting and tells
an Arduino, which shows it on an RGB LED.

| LED | State | Meaning |
|-----|-------|---------|
| solid green | `FREE` | nothing going on |
| breathing amber | `UPCOMING` | a calendar event starts within 5 minutes |
| solid red | `MEETING` | audio is being captured, or an event is happening now |
| solid purple | `BUSY` | macOS Focus / Do Not Disturb is on |
| breathing dim blue | *stale* | the board has heard nothing from the Mac for 20s |
| dark | `OFF` | the agent shut down cleanly |

No cloud service, no API tokens, no Teams or Slack integration. Detection is
local: who is capturing audio, and what the calendar says.

## Quick start

```sh
./setup.sh                                    # venv + dependencies
cp config.example.json config.json
./.venv/bin/python meeting_status.py --probe   # run in a terminal the FIRST time
```

`--probe` triggers the macOS Calendar permission prompt. Grant it there and
you can see straight away whether the calendar signal works. Note that a TCC
grant belongs to the identity of the process that asked, so granting it in a
terminal does not grant it to the background agent — the agent asks again for
itself on first start, and the prompt appears in your GUI session. It waits
`auth_wait_background` seconds (25) for an answer and starts either way.

Then flash the board and run it:

```sh
arduino-cli compile --fqbn arduino:avr:uno arduino/meeting_status_led
arduino-cli upload  --fqbn arduino:avr:uno -p /dev/cu.usbserial-XXXX arduino/meeting_status_led
./.venv/bin/python meeting_status.py --config config.json -v
```

Find the port with `ls /dev/cu.usbserial* /dev/cu.usbmodem*`. The Arduino IDE
works for flashing too.

## Hardware

A common-cathode RGB LED and three 220Ω resistors. Any ATmega328P board
(Uno, Nano, Pro Mini); a CH340 clone is fine.

```
D2 --[220R]-- B leg
D3 --[220R]-- G leg
D4 --[220R]-- R leg
GND ---------- common (longest) leg
```

Two things worth knowing before you wire it:

- **Leg order varies between parts.** The pin-to-colour mapping above is just
  what one particular LED happened to be. If the colours come out wrong, swap
  `PIN_R` / `PIN_G` / `PIN_B` at the top of the sketch instead of rewiring. To
  identify the legs, send `RGB 255 0 0`, `RGB 0 255 0`, `RGB 0 0 255` and note
  which colour appears for each.
- **D2/D3/D4 are not PWM pins.** The sketch generates PWM in software from a
  Timer2 interrupt, so any three pins in `D2..D7` work. That is what buys the
  brightness control and the breathing effect on non-PWM pins; the cost is that
  the sketch owns Timer2, so `tone()` will not work alongside it. See
  [docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md).

For a common-anode LED, wire the common leg to 5V and set `COMMON_ANODE 1` at
the top of the sketch.

## How it decides

Five signals, highest precedence first. Full reasoning in
[docs/DESIGN-NOTES.md](docs/DESIGN-NOTES.md).

1. **Override file** — `echo MEETING > ~/.meeting-status` forces a state.
   Delete the file to return to automatic.
2. **Microphone** — any process *capturing* audio, via CoreAudio's per-process
   API. Catches Teams, Zoom, Meet and Slack huddles without knowing anything
   about those apps. This is the signal that does the work.
3. **Calendar** — an event in progress (`MEETING`) or starting soon
   (`UPCOMING`), read through EventKit. All-day, cancelled, Free-marked and
   declined events are skipped, as are titles matching `event_ignore`.
4. **Meeting app playing audio** — covers the case signals 2 and 3 miss: a
   listen-only webinar where you never get mic access. Requires continuous
   playback for `meeting_app_hold` seconds so notification chimes do not count.
5. **Focus / DND** — best effort. macOS moves this file between releases; when
   unreadable the signal is skipped silently.

## Run it in the background

```sh
./install-agent.sh      # generates the plist with absolute paths, loads it
tail -f ~/Library/Logs/meeting-status.log
./uninstall-agent.sh    # unload and remove
```

The agent is a launchd *user* agent: it starts at login, restarts if it dies,
and runs at background priority. Measured cost when idle: 0.0% CPU, ~47MB RSS.

`install-agent.sh` generates `~/Library/LaunchAgents/local.meetingstatus.plist`
from `meeting-status.plist.template`, substituting this checkout's path. Rerun
it if you move the directory — launchd needs absolute paths and cannot expand
`~`.

## Commands

```sh
meeting_status.py                 # the daemon
meeting_status.py --probe         # every signal, the audio process list, resolved state
meeting_status.py --once          # print the current state and exit
meeting_status.py --no-serial     # detection only, no Arduino
meeting_status.py --send MEETING  # force one state onto the LED and exit
meeting_status.py -v              # also log cause changes, not just state changes
```

`--probe` is the diagnostic to reach for first. It prints which audio API is in
use, every process holding audio and whether it is capturing or playing,
calendar authorization, the override file, the resolved state and the serial
port it found.

Board commands are documented in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Configuration

Edit `config.json` (copied from `config.example.json`; it is gitignored so your
local settings stay out of the repo).

| Key | Purpose |
|-----|---------|
| `port` / `port_globs` | serial device, or the patterns used to find one |
| `proc_ignore` | bundle ids allowed to hold the mic without it counting |
| `meeting_apps` | bundle id regexes whose audio playback counts as a meeting |
| `meeting_app_hold` | seconds of continuous playback before that counts (20) |
| `event_ignore` | regexes for calendar titles that are not meetings |
| `lead_minutes` | how early `UPCOMING` starts (5) |
| `auth_wait_background` | seconds the background agent waits for the calendar prompt (25) |
| `heartbeat` | how often state is resent; must stay under the sketch's 20s stale timeout |
| `use_mic` / `use_calendar` / `use_dnd` | turn whole signals off |

`mic_ignore` applies only to the pre-macOS-14.2 fallback path.

To find the bundle id to add to `proc_ignore` or `meeting_apps`, run `--probe`
and read the process list.

## Troubleshooting

**LED breathes dim blue.** The board is not hearing the Mac. Check the log,
check `ls /dev/cu.*`, and confirm nothing else has the port open — only one
process can, so close the Arduino IDE serial monitor.

**Colours are wrong.** The LED's legs are not in the order the sketch assumes.
Identify them with `RGB 255 0 0` / `RGB 0 255 0` / `RGB 0 0 255` and swap
`PIN_R` / `PIN_G` / `PIN_B` to match.

**LED is red all the time.** Something is capturing audio. Run `--probe` and
look for a `CAPTURING` row; it names the bundle id. Add it to `proc_ignore` if
it should not count.

**Music through my headset turns the LED red.** `--probe` is reporting
`Per-process capture API: no`, so the device-wide fallback is in use and cannot
tell headset playback from capture. Upgrade macOS, or set `use_mic: false` and
drive the LED from the override file.

**No calendar events ever.** Check the log for `EventKit access authorized`. If
it says `notDetermined`, the prompt was missed — `launchctl kickstart -k
gui/$(id -u)/local.meetingstatus` makes the agent ask again.
Confirm the account is in Calendar.app — EventKit only sees what Calendar.app
has. For Exchange or Microsoft 365, add it under System Settings → Internet
Accounts.

**Board resets when the agent connects.** Expected. Opening the port toggles
DTR on most boards; the agent waits 2 seconds for the bootloader before
sending.

## Repo layout

```
meeting_status.py                      the host agent (single file, stdlib + pyserial + PyObjC)
arduino/meeting_status_led/            the sketch
config.example.json                    config template
setup.sh                               create the venv
install-agent.sh / uninstall-agent.sh  launchd agent
meeting-status.plist.template          launchd plist, paths substituted at install
docs/PROTOCOL.md                       host <-> board serial contract
docs/DESIGN-NOTES.md                   why it works this way, and what does not work
```

## Requirements

macOS 14.2 or newer for per-process audio detection (older macOS falls back to
a less accurate device-wide check), Python 3.9+, and an ATmega328P Arduino.

## License

MIT — see [LICENSE](LICENSE).
