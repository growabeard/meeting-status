# meeting-status

A background agent for macOS and Windows that works out whether you are in a
meeting and tells an Arduino, which shows it on an RGB LED.

| LED | State | Meaning |
|-----|-------|---------|
| solid green | `FREE` | nothing going on |
| breathing amber | `UPCOMING` | a calendar event starts within 5 minutes |
| solid red | `MEETING` | audio is being captured, or an event is happening now |
| solid purple | `BUSY` | Focus / Do Not Disturb is on (macOS only) |
| breathing dim blue | *stale* | the board has heard nothing from the host for 20s |
| dark | `OFF` | the agent shut down cleanly |

No cloud service, no API tokens, no Teams or Slack integration. Detection is
local: who is capturing audio, and what the calendar says.

The board does not know or care which OS is driving it. Everything
platform-specific lives in `signals_darwin.py` and `signals_win32.py`, which
expose the same contract to the same detection logic.

## Quick start — macOS

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

Find the port with `ls /dev/cu.usbserial* /dev/cu.usbmodem*`, or let `--probe`
name the one it picked. The Arduino IDE works for flashing too.

## Quick start — Windows

> **Untested.** The Windows signals and the scheduled task were written against
> the documented APIs but have not yet been run on a Windows machine. The macOS
> path is in daily use. If something here is wrong, an issue with the traceback
> is useful.

```powershell
.\setup.ps1                                   # venv + dependencies
copy config.example.json config.json
.\.venv\Scripts\python.exe meeting_status.py --probe
```

If PowerShell refuses to run the script, use
`powershell -ExecutionPolicy Bypass -File .\setup.ps1`.

There is no permission prompt to grant: the mic signal reads the microphone
privacy registry, and Outlook COM needs no grant. `--probe` should report a
serial port, a calendar that is `authorized`, and a resolved state.

The calendar needs **classic** Outlook installed, because that is what registers
the COM server. Running new Outlook day to day is fine as long as classic is
still there. Check in PowerShell:

```powershell
$ol = New-Object -ComObject Outlook.Application
$ol.Session.GetDefaultFolder(9).Items.Count
```

A number means the calendar signal will work. An error means it will not, and
the mic signal carries the light on its own.

Then flash the board and run it:

```powershell
arduino-cli compile --fqbn arduino:avr:uno arduino/meeting_status_led
arduino-cli upload  --fqbn arduino:avr:uno -p COM3 arduino/meeting_status_led
.\.venv\Scripts\python.exe meeting_status.py --config config.json -v
```

Find the port in Device Manager under Ports (COM & LPT), or let `--probe` name
the one it picked.

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
2. **Microphone** — any process *capturing* audio. Catches Teams, Zoom, Meet
   and Slack huddles without knowing anything about those apps. This is the
   signal that does the work, and it is the one with no dependencies:
   CoreAudio's per-process API on macOS, the microphone privacy registry on
   Windows.
3. **Calendar** — an event in progress (`MEETING`) or starting soon
   (`UPCOMING`); EventKit on macOS, Outlook COM on Windows. All-day, cancelled,
   Free-marked and declined events are skipped, as are titles matching
   `event_ignore`.
4. **Meeting app playing audio** — covers the case signals 2 and 3 miss: a
   listen-only webinar where you never get mic access. Requires continuous
   playback for `meeting_app_hold` seconds so notification chimes do not count.
   Needs `pycaw` on Windows and is the one signal allowed to be missing.
5. **Focus / DND** — best effort, and macOS only. macOS moves this file between
   releases; when unreadable the signal is skipped silently. Windows Focus
   Assist is not read at all — it lives in a binary blob Microsoft reshuffles
   between releases, so the signal reports itself unavailable rather than guess.

## Overriding it by hand

The override is a file, so anything that can write a file can drive the light.
`bin/ms` wraps it on macOS, `bin/ms.ps1` on Windows:

```sh
./bin/ms            # what the light is doing, and why
./bin/ms meeting    # force red - busy / free / off / upcoming also work
./bin/ms auto       # back to automatic
./bin/ms log        # follow the daemon log
```

```powershell
.\bin\ms.ps1            # what the light is doing, and why
.\bin\ms.ps1 meeting    # force red
.\bin\ms.ps1 auto       # back to automatic
.\bin\ms.ps1 log        # follow the daemon log
```

Add `bin` to your `PATH` to drop the `./`. Or skip the script entirely — the
override file path is `~/.meeting-status` on both platforms, because
`expanduser("~")` resolves to your user profile on Windows:

```sh
echo MEETING > ~/.meeting-status    # force a state
rm ~/.meeting-status                # back to automatic
```

The override beats every other signal, including the mic, so `FREE` holds green
through a real call. Writing `AUTO`, `NONE` or `CLEAR` is the same as deleting
the file. An unrecognised value is ignored and detection carries on, so a typo
fails safe. The daemon picks up a change on its next poll, about a second.

## Run it in the background

### macOS — launchd

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

### Windows — Task Scheduler

```powershell
.\install-task.ps1      # generates the task XML with absolute paths, registers it
Get-Content -Wait $env:LOCALAPPDATA\meeting-status\meeting-status.log
.\uninstall-task.ps1    # stop and remove
```

The task runs at logon under your own account, at below-normal priority, with
no console window because the action is `pythonw.exe`. That also means the
process has no stderr at all, so the task passes `--log` and the daemon
redirects its own output to
`%LOCALAPPDATA%\meeting-status\meeting-status.log`.

One honest difference from launchd: `RestartOnFailure` restarts the task if it
exits with an error, but it is not launchd's `KeepAlive`. A clean exit stays
exited until the next logon.

Rerun `install-task.ps1` if you move the directory, for the same reason as on
macOS.

## Commands

```sh
meeting_status.py                 # the daemon
meeting_status.py --probe         # every signal, the audio process list, resolved state
meeting_status.py --once          # print the current state and exit
meeting_status.py --no-serial     # detection only, no Arduino
meeting_status.py --send MEETING  # force one state onto the LED and exit
meeting_status.py --log PATH      # append log output to a file instead of stderr
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
| `port` / `port_match` | serial device, or the VID:PID and description substrings used to find one |
| `proc_ignore` | process names allowed to hold the mic without it counting |
| `meeting_apps` | process name regexes whose audio playback counts as a meeting |
| `meeting_app_hold` | seconds of continuous playback before that counts (20) |
| `event_ignore` | regexes for calendar titles that are not meetings |
| `lead_minutes` | how early `UPCOMING` starts (5) |
| `auth_wait_background` | seconds the background agent waits for the calendar prompt (25, macOS only) |
| `heartbeat` | how often state is resent; must stay under the sketch's 20s stale timeout |
| `use_mic` / `use_calendar` / `use_dnd` | turn whole signals off |

`proc_ignore` and `meeting_apps` match process names, which differ per OS, so
their defaults live in `signals_darwin.py` and `signals_win32.py` rather than in
`config.example.json` — a macOS bundle id would match a Windows exe name never,
and the result would be a silently dead signal. Set either key in `config.json`
to replace the platform default; run `--probe` to see the names on your machine.

## Troubleshooting

**LED breathes dim blue.** The board is not hearing the host. Check the log,
check that `--probe` still names a port, and confirm nothing else has the port
open — only one process can, so close the Arduino IDE serial monitor. If both a
Mac and a PC are plugged into the same board, whichever opened the port first
wins and the other logs that it cannot open it.

**Colours are wrong.** The LED's legs are not in the order the sketch assumes.
Identify them with `RGB 255 0 0` / `RGB 0 255 0` / `RGB 0 0 255` and swap
`PIN_R` / `PIN_G` / `PIN_B` to match.

**LED is red all the time.** Something is capturing audio. Run `--probe` and
look for a `CAPTURING` row; it names the process. Add it to `proc_ignore` if it
should not count.

**Nothing ever goes red for a call (macOS).** Check `--probe` for
`Per-process capture API: yes`. On macOS older than 14.2 that API does not
exist, and the mic signal switches itself off rather than fall back to the
device-wide flag, which cannot tell headset playback from capture and would
turn the light red for music. Drive the LED from the calendar and the override
file instead.

**Nothing ever goes red for a call (Windows).** Run
`python tools\win_probe_mic.py` with nothing using the mic, then again during a
call, and compare. It prints which apps the privacy registry says are capturing
right now. If the second run shows nothing, the registry is not reporting the
app you are using and the calendar signal is the one to rely on.

**No calendar events ever (macOS).** Check the log for `EventKit access
authorized`. If it says `notDetermined`, the prompt was missed — `launchctl
kickstart -k gui/$(id -u)/local.meetingstatus` makes the agent ask again.
Confirm the account is in Calendar.app — EventKit only sees what Calendar.app
has. For Exchange or Microsoft 365, add it under System Settings → Internet
Accounts.

**No calendar events ever (Windows).** Check the log for an Outlook dispatch
error. Classic Outlook must be installed to register the COM server; see the
two-line PowerShell check in the Windows quick start.

**Board resets when the agent connects.** Expected. Opening the port toggles
DTR on most boards; the agent waits 2 seconds for the bootloader before
sending.

## Repo layout

```
meeting_status.py                      detection, precedence, serial, CLI
common.py                              log(), AudioState, STATES, the playback debounce
signals_darwin.py                      macOS signals: CoreAudio, EventKit, DND plist
signals_win32.py                       Windows signals: privacy registry, pycaw, Outlook COM
bin/ms  /  bin/ms.ps1                  read and override the light from the shell
arduino/meeting_status_led/            the sketch
config.example.json                    config template
setup.sh  /  setup.ps1                 create the venv
install-agent.sh / uninstall-agent.sh  launchd agent (macOS)
install-task.ps1 / uninstall-task.ps1  scheduled task (Windows)
meeting-status.plist.template          launchd plist, paths substituted at install
meeting-status.task.xml.template       Task Scheduler XML, same idea
tools/win_probe_mic.py                 dump the Windows microphone privacy registry
docs/PROTOCOL.md                       host <-> board serial contract
docs/DESIGN-NOTES.md                   why it works this way, and what does not work
```

## Requirements

Python 3.9+ and an ATmega328P Arduino on either platform.

- **macOS** 14.2 or newer for the mic signal; older releases have no
  per-process audio API, and the calendar and override signals still work.
- **Windows** 10 1903 or newer for the microphone privacy registry, and classic
  Outlook installed for the calendar signal. `pycaw` is optional and only
  powers the meeting-app playback signal.

## License

MIT — see [LICENSE](LICENSE).
