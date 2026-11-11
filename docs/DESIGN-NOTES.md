# Design notes

Why this works the way it does, and the approaches that turned out not to work.
Most of these were found the hard way.

## Detection

What to detect is platform-neutral; where the reasoning below names an API it is
the macOS one. The Windows equivalents are in [Windows](#windows).

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

On macOS older than 14.2 the per-process API is absent, and the mic signal
switches itself off rather than fall back to the device-wide flag and its
headset flaw. A light that lies is worse than a signal that is absent, which is
also why the fallback code was deleted rather than left in reach. `--probe`
reports whether the API is there.

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

Both AppleScript backends below were implemented, shipped disabled by
default, and later deleted: ~120 lines supporting two paths that do not work.

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

A launchd *user* agent can display a prompt - it runs in the GUI session - but
it cannot afford to wait for one. The daemon checks `sys.stdin.isatty()` and
uses that only to pick how long to wait: `auth_wait` (120s) in a terminal,
`auth_wait_background` (25s) under launchd. It starts either way. Waiting the
full two minutes under launchd would stall startup behind a dialog you may not
be looking at; not asking at all would mean the agent never gets a grant,
because a TCC grant belongs to the identity of the process that asked and the
terminal's grant is not the agent's.

**`flush()` on a serial port can hang forever.** `pyserial`'s `flush()` maps to
`termios.tcdrain()`, which blocks indefinitely when the far end stops consuming
bytes — an unpowered or crashed board hangs the daemon. The fix is to never
call it and rely on `write_timeout` instead.

## Windows

The Arduino side needed nothing: the sketch, the wiring and the serial protocol
never knew which OS was talking to them. What was macOS-specific was three
signals, so those moved behind a seam.

**A module per platform, not conditionals.** `meeting_status.py` imports
`signals_darwin` or `signals_win32` as `signals` based on `sys.platform`, and
the detection logic contains no OS branches at all. Each module exports the same
small contract - a `MicWatcher`, a `CALENDAR` source, `dnd_active()`, and the
`DEFAULT_PROC_IGNORE` / `DEFAULT_MEETING_APPS` lists. `common.py` holds `log()`,
`AudioState`, `STATES` and the playback debounce, and imports no siblings; it
exists to break the cycle, since the platform modules need those and
`meeting_status.py` needs the platform modules.

Four flat modules rather than a package, so launchd and Task Scheduler keep
invoking a plain script path.

The debounce lives in `common.collapse_audio()` and is shared deliberately. Two
copies drifting apart would make the light behave differently on the two
platforms for no visible reason.

**Process defaults are per platform.** `proc_ignore` and `meeting_apps` are
regexes over process names, and a macOS bundle id (`us\.zoom\.`) matches a
Windows process name (`Zoom.exe`) never. Shipping one set in
`config.example.json` would have given a Windows user a working mic signal and
a silently dead meeting-app signal - the worst shape of failure here, because
nothing errors. The defaults therefore live in the platform modules and the
example config does not set either key.

**Mic: the privacy registry, no dependency.** Windows records capture use under
`HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone`,
in two subtrees: `NonPackaged` for classic exes, whose key names are exe paths
with `\` mangled to `#`, and direct subkeys for MSIX/Store apps, which is how
current Teams ships. Each holds `LastUsedTimeStart` and `LastUsedTimeStop` as
FILETIME QWORDs, and a non-zero `Start` with `Stop == 0` means that app is
capturing right now. This is the data behind the taskbar mic indicator. It is
stdlib `winreg`, so the signal that decides the light has no dependency at all.
`tools/win_probe_mic.py` dumps both subtrees, and for the webcam too as a second
example of the same pattern; run it idle and again mid-call and diff.

**Playback: WASAPI via pycaw.** `AudioUtilities.GetAllSessions()`, filtered to
`State == 1`. The exe name comes from the session's `ProcessId` through
`OpenProcess` + `QueryFullProcessImageNameW` rather than pycaw's `.Process`,
which would pull in psutil for a name ctypes can get. Capture and playback are
split across two APIs here, unlike CoreAudio where one call answers both, so the
playback half can be missing on its own - and it is allowed to be. If pycaw will
not install, the light still works and only the listen-only-webinar case is
lost, which is why `setup.ps1` retries without it.

**Calendar: Outlook COM, and three traps.** `comtypes`, not `pywin32`:
`comtypes` is pure Python and pycaw already depends on it, while `pywin32`
ships compiled wheels and a new interpreter is a real risk of there not being
one. No permission grant is involved, so `authorized()` just reports whether
the dispatch succeeded.

All three traps produce silently wrong results rather than errors:

- `Items.IncludeRecurrences = True` expands a recurring series only if
  `Items.Sort("[Start]")` was applied **first**. A work calendar is mostly
  recurring, so the wrong order returns almost nothing and looks like an empty
  afternoon.
- `Items.Restrict()` takes a locale-formatted date string, not a datetime.
- Reading `.Start` on every item of a large mailbox is slow enough to notice, so
  the window is restricted first and the walk is capped at `MAX_ITEMS`.

A COM object also belongs to the thread that created it, and the source is used
from the main thread at startup and from the calendar polling thread after that.
`OutlookSource` keeps its dispatch in `threading.local()` and calls
`CoInitialize()` per thread.

The four EventKit filters map one-to-one onto Outlook properties, so the
filtering intent carries over exactly: `isAllDay` → `AllDayEvent`, cancelled →
`MeetingStatus in (5, 7)`, Free → `BusyStatus == 0`, declined →
`ResponseStatus == 4`.

**Focus Assist is not read.** It lives in a binary `CloudStore` registry blob
that Microsoft reshuffles between releases. `dnd_active()` returns `None`, which
the existing code already treats as "unavailable" and skips - the same call made
for the macOS DND plist, and for the same reason: a light that lies is worse
than a signal that is absent.

**`pythonw.exe` has no stderr.** The scheduled task runs `pythonw` to avoid a
console window, and that leaves `sys.stderr` as `None`, so an unguarded log
write would crash the daemon rather than merely lose a line. `common.log()`
returns early in that case, and `--log PATH` points `sys.stderr` at a file
before anything else runs - Task Scheduler has no equivalent of launchd's
`StandardErrorPath`, so the daemon has to do its own redirect.

**Task Scheduler is not launchd.** `RestartOnFailure` restarts the task when it
exits non-zero; it is not `KeepAlive`, which restarts on any exit at all. A
clean exit stays exited until the next logon. `ExecutionTimeLimit PT0S` is
needed to stop the default three-day kill, and `Priority 7` is the
`ProcessType=Background` equivalent. The XML must be UTF-16 to match its own
declaration or `schtasks /Create /XML` rejects it.

**Serial port lookup, the one change that helped both.** `/dev/cu.*` globs do
not exist on Windows, where the port is `COM3`. `port_match` now matches
`serial.tools.list_ports.comports()` on USB VID:PID with a description
fallback, which is more precise than the globs it replaced on macOS too.

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
