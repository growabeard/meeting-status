#!/usr/bin/env python3
"""
meeting-status: watch macOS for "am I in a meeting?" and push the answer to an
Arduino over USB serial, which drives an RGB LED.

Signals used (highest confidence first):

  1. mic       - any audio *input* device is running somewhere (CoreAudio).
                 Covers Teams calls, Slack huddles, Zoom, Meet in a browser -
                 anything that opens the microphone. No permissions needed.
  2. calendar  - an event is in progress or starts soon, read from Calendar.app
                 and/or Microsoft Outlook via AppleScript. Needs a one-time
                 Automation permission grant (macOS will prompt).
  3. dnd       - macOS Focus / Do Not Disturb, best effort (format varies by
                 OS release; silently skipped when unreadable).
  4. override  - a file you write by hand, wins over everything. Good for
                 "I'm heads-down, leave me alone".

Resolved states, highest precedence first:

  MEETING   mic is live, or a calendar event is happening right now
  BUSY      Focus/DND is on
  UPCOMING  a calendar event starts within `lead_minutes`
  FREE      none of the above

Usage:
  meeting_status.py                 run the daemon
  meeting_status.py --probe         show audio devices + what each signal says
  meeting_status.py --once          print the resolved status and exit
  meeting_status.py --no-serial     run the detection loop without an Arduino
  meeting_status.py --send MEETING  force a state onto the LED and exit
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # Serial. port=null auto-detects the first matching device.
    "port": None,
    "port_globs": [
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.wchusbserial*",
        "/dev/cu.SLAB_USBtoUART*",
    ],
    "baud": 115200,
    # Timing, seconds.
    "mic_poll": 2.0,
    "calendar_poll": 60.0,
    "heartbeat": 5.0,
    # Calendar.
    # "eventkit" reads the real calendar database through EventKit and is the
    # one that works. "calendar" and "outlook" are AppleScript fallbacks kept
    # for machines where EventKit access is refused; on current macOS
    # Calendar.app tends not to answer AppleScript at all (-1712) and the new
    # Outlook has no AppleScript support, so neither is enabled by default.
    "calendar_backends": ["eventkit"],
    "lead_minutes": 5,
    # Events whose title matches any of these regexes are ignored (case
    # insensitive). All-day events are always ignored.
    # Titles that are busy blocks rather than meetings. "due" catches the
    # deadline reminders Outlook users tend to park on the calendar as Busy.
    "event_ignore": ["^focus", "^lunch", "^hold\\b", "block", "no meeting",
                     "\\bdue\\b", "\\bwalk time\\b", "^ooo\\b", "^pto\\b"],
    # Audio input devices to ignore - virtual/loopback devices that report
    # themselves as running all the time. Substring match, case insensitive.
    "mic_ignore": [
        "zoomaudiodevice",
        "blackhole",
        "loopback",
        "soundflower",
        "krisp",
        "vb-cable",
        "microsoft teams audio",
    ],
    # Processes that may legitimately hold the microphone without it meaning
    # you are in a meeting. Substring match on the bundle id, case insensitive.
    "proc_ignore": [
        "com.apple.corespeech",            # Siri / "Hey Siri"
        "com.apple.assistantd",
        "com.apple.accessibility.heard",
        "com.apple.universalaccessd",
        "com.apple.controlcenter",
    ],
    # Apps whose audio *playback* means you are in a meeting, even when the
    # mic is never opened (a listen-only Zoom webinar, a muted-by-host call).
    # Regex, matched case insensitively against the bundle id. Browsers are
    # deliberately absent: a browser plays everything, not just meetings.
    "meeting_apps": [
        r"us\.zoom\.",
        r"com\.microsoft\.teams",
        r"com\.tinyspeck\.slack",
        r"webex|cisco-systems\.spark",
        r"com\.google\.chrome\.helper\.meet",
    ],
    # How long a meeting app must play audio before it counts. Notification
    # chimes are a second or two; this keeps a Slack ping from reading as a
    # meeting. Raise it if stray sounds still get through.
    "meeting_app_hold": 20.0,
    # Signals to use at all.
    "use_mic": True,
    "use_calendar": True,
    "use_dnd": True,
    "override_file": "~/.meeting-status",
    "auth_wait": 120.0,
    "verbose": False,
}

CONFIG_SEARCH = [
    "./config.json",
    "~/.config/meeting-status/config.json",
]

STATES = ("FREE", "UPCOMING", "BUSY", "MEETING", "OFF")


def load_config(path=None):
    cfg = dict(DEFAULT_CONFIG)
    candidates = [path] if path else CONFIG_SEARCH
    for c in candidates:
        if not c:
            continue
        p = os.path.expanduser(c)
        if os.path.isfile(p):
            with open(p) as fh:
                cfg.update(json.load(fh))
            cfg["_config_path"] = p
            break
    return cfg


def log(msg):
    sys.stderr.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Signal 1: microphone in use, via CoreAudio (ctypes, no dependencies)
# --------------------------------------------------------------------------


class _PropAddr(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


def _fourcc(s):
    return int.from_bytes(s.encode("ascii"), "big")


_SYS_OBJ = 1
_P_DEVICES = _fourcc("dev#")   # kAudioHardwarePropertyDevices
_P_STREAMS = _fourcc("stm#")   # kAudioDevicePropertyStreams
_P_RUNNING = _fourcc("gone")   # kAudioDevicePropertyDeviceIsRunningSomewhere
_P_NAME = _fourcc("lnam")      # kAudioObjectPropertyName
_S_GLOBAL = _fourcc("glob")
_S_INPUT = _fourcc("inpt")

# Per-process audio objects, macOS 14.2+. These selectors are easy to get
# wrong: input/output are 'piri'/'piro', verified against live playback.
_P_PROCESSES = _fourcc("prs#")    # kAudioHardwarePropertyProcessObjectList
_P_PID = _fourcc("ppid")          # kAudioProcessPropertyPID
_P_BUNDLE_ID = _fourcc("pbid")    # kAudioProcessPropertyBundleID
_P_RUNNING_IN = _fourcc("piri")   # kAudioProcessPropertyIsRunningInput
_P_RUNNING_OUT = _fourcc("piro")  # kAudioProcessPropertyIsRunningOutput
_ELEM_MAIN = 0
_CF_UTF8 = 0x08000100


class MicWatcher:
    """Reports whether anything is actually capturing audio right now.

    Two strategies, because the obvious one is wrong:

    Preferred - the per-process API (macOS 14.2+). CoreAudio exposes one audio
    object per process with kAudioProcessPropertyIsRunningInput, so we can ask
    "is any process capturing?" and get the bundle id of the culprit.

    Fallback - per-device kAudioDevicePropertyDeviceIsRunningSomewhere. This is
    what this script used to do, and it is subtly broken for anyone wearing a
    USB or Bluetooth headset: such a headset is a SINGLE audio device carrying
    both input and output streams, and that property is device-wide with no
    input scope. Playing music through the headset marks the device as running
    and the old code called it a meeting. Only used when the process API is
    unavailable, and it is no more accurate there than it ever was.

    Neither strategy cares about microphone permission or whether a mic is
    merely plugged in. Connecting a headset adds an idle device; an app holding
    mic permission but not recording does not register. Capture has to be live.
    """

    def __init__(self, ignore=(), proc_ignore=(), meeting_apps=(),
                 app_hold=20.0):
        self.ignore = [s.lower() for s in ignore]
        self.proc_ignore = [s.lower() for s in proc_ignore]
        self.meeting_apps = []
        for pat in meeting_apps:
            try:
                self.meeting_apps.append(re.compile(pat, re.I))
            except re.error:
                log("bad meeting_apps pattern, ignoring: %r" % pat)
        self.app_hold = float(app_hold)
        self._playing_since = {}
        self.available = False
        self.process_api = False
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            return
        self.ca = ctypes.CDLL(ca_path)
        self.cf = ctypes.CDLL(cf_path)
        u32 = ctypes.c_uint32
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [
            u32, ctypes.POINTER(_PropAddr), u32, ctypes.c_void_p,
            ctypes.POINTER(u32),
        ]
        self.ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self.ca.AudioObjectGetPropertyData.argtypes = [
            u32, ctypes.POINTER(_PropAddr), u32, ctypes.c_void_p,
            ctypes.POINTER(u32), ctypes.c_void_p,
        ]
        self.ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self.cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, u32,
        ]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.available = True
        self.process_api = self._size(_SYS_OBJ, _P_PROCESSES, _S_GLOBAL) is not None

    # -- low level helpers --

    def _size(self, obj, selector, scope):
        addr = _PropAddr(selector, scope, _ELEM_MAIN)
        size = ctypes.c_uint32(0)
        err = self.ca.AudioObjectGetPropertyDataSize(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(size)
        )
        return None if err else size.value

    def _get(self, obj, selector, scope, buf, size):
        addr = _PropAddr(selector, scope, _ELEM_MAIN)
        io = ctypes.c_uint32(size)
        err = self.ca.AudioObjectGetPropertyData(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(io), buf
        )
        return err == 0

    def _object_list(self, selector):
        size = self._size(_SYS_OBJ, selector, _S_GLOBAL)
        if not size:
            return []
        n = size // ctypes.sizeof(ctypes.c_uint32)
        arr = (ctypes.c_uint32 * n)()
        if not self._get(_SYS_OBJ, selector, _S_GLOBAL, arr, size):
            return []
        return list(arr)

    def _cfstring(self, obj, selector):
        ref = ctypes.c_void_p()
        if not self._get(obj, selector, _S_GLOBAL, ctypes.byref(ref),
                         ctypes.sizeof(ref)) or not ref:
            return None
        buf = ctypes.create_string_buffer(512)
        ok = self.cf.CFStringGetCString(ref, buf, 512, _CF_UTF8)
        self.cf.CFRelease(ref)
        return buf.value.decode("utf-8", "replace") if ok else None

    def _flag(self, obj, selector):
        val = ctypes.c_uint32(0)
        if not self._get(obj, selector, _S_GLOBAL, ctypes.byref(val),
                         ctypes.sizeof(val)):
            return None
        return bool(val.value)

    # -- per-process view (preferred) --

    def processes(self):
        """[(name, capturing, playing, ignored)] for processes using audio."""
        out = []
        if not self.process_api:
            return out
        for proc in self._object_list(_P_PROCESSES):
            try:
                name = self._cfstring(proc, _P_BUNDLE_ID)
                if not name:
                    pid = ctypes.c_int32(0)
                    self._get(proc, _P_PID, _S_GLOBAL, ctypes.byref(pid), 4)
                    name = "pid:%d" % pid.value
                capturing = self._flag(proc, _P_RUNNING_IN)
                playing = self._flag(proc, _P_RUNNING_OUT)
                if capturing is None and playing is None:
                    continue
                ignored = any(s in name.lower() for s in self.proc_ignore)
                out.append((name, bool(capturing), bool(playing), ignored))
            except OSError:
                continue
        return out

    # -- per-device view (fallback, and useful diagnostics) --

    def _is_input(self, dev):
        return bool(self._size(dev, _P_STREAMS, _S_INPUT))

    def inputs(self):
        """[(name, device_running, ignored)] for every audio input device.

        device_running is the device-wide flag, so on a headset it goes true
        for playback too. Shown in --probe for diagnosis; not trusted for the
        decision unless the process API is missing.
        """
        out = []
        if not self.available:
            return out
        for dev in self._object_list(_P_DEVICES):
            try:
                if not self._is_input(dev):
                    continue
                name = self._cfstring(dev, _P_NAME) or "<unknown>"
                ignored = any(s in name.lower() for s in self.ignore)
                out.append((name, bool(self._flag(dev, _P_RUNNING)), ignored))
            except OSError:
                continue
        return out

    # -- the decision --

    def active(self):
        """Name of whatever is capturing audio, or None."""
        if not self.available:
            return None
        if self.process_api:
            for name, capturing, _playing, ignored in self.processes():
                if capturing and not ignored:
                    return name
            return None
        for name, running, ignored in self.inputs():
            if running and not ignored:
                return name
        return None

    def meeting_audio(self):
        """Name of a meeting app that has been playing audio long enough.

        Catches the meetings the mic signal cannot see: a Zoom webinar where
        you have no mic access, or a call where the host muted everyone. Needs
        the per-process API - the device-wide flag cannot attribute playback
        to an app, so this signal is simply off on older macOS.

        Playback has to persist for app_hold seconds. Without that, every
        Slack notification chime would read as a meeting.
        """
        if not self.process_api or not self.meeting_apps:
            return None
        return self._meeting_scan()[0]

    def meeting_candidates(self):
        """Meeting apps playing audio right now, before the hold is applied.

        Diagnostics only - a one-shot --probe has no history to debounce
        against, so it reports candidates and lets you read the hold.
        """
        if not self.process_api or not self.meeting_apps:
            return []
        return self._meeting_scan()[1]

    def _meeting_scan(self):
        """(name past the hold or None, [all candidates])."""
        now = time.time()
        playing_now = set()
        hit = None
        for name, _capturing, playing, ignored in self.processes():
            if not playing or ignored:
                continue
            if not any(rx.search(name) for rx in self.meeting_apps):
                continue
            playing_now.add(name)
            since = self._playing_since.setdefault(name, now)
            if now - since >= self.app_hold and hit is None:
                hit = name
        # Forget apps that have stopped, so the next burst starts its own clock.
        for name in [n for n in self._playing_since if n not in playing_now]:
            del self._playing_since[name]
        return hit, sorted(playing_now)


# --------------------------------------------------------------------------
# Signal 2: calendar, via AppleScript
# --------------------------------------------------------------------------

# Both scripts take lead-seconds as argv[1] and print one line per relevant
# event: "title \t seconds_until_start \t seconds_until_end". Deltas are
# computed inside AppleScript so there is no date parsing or locale guessing.

SCRIPT_CALENDAR = r'''
on run argv
    set leadSecs to (item 1 of argv) as integer
    set nowD to (current date)
    set endD to nowD + leadSecs
    set out to ""
    tell application "Calendar"
        repeat with cal in calendars
            try
                set evs to (every event of cal whose allday event is false and start date < endD and end date > nowD)
                repeat with e in evs
                    try
                        set s to (((start date of e) - nowD) as integer)
                        set f to (((end date of e) - nowD) as integer)
                        set out to out & (summary of e) & tab & s & tab & f & linefeed
                    end try
                end repeat
            end try
        end repeat
    end tell
    return out
end run
'''

SCRIPT_OUTLOOK = r'''
on run argv
    set leadSecs to (item 1 of argv) as integer
    set nowD to (current date)
    set endD to nowD + leadSecs
    set out to ""
    tell application "Microsoft Outlook"
        set evs to (every calendar event whose start time < endD and end time > nowD)
        repeat with e in evs
            try
                if (all day flag of e) is false then
                    set s to (((start time of e) - nowD) as integer)
                    set f to (((end time of e) - nowD) as integer)
                    set out to out & (subject of e) & tab & s & tab & f & linefeed
                end if
            end try
        end repeat
    end tell
    return out
end run
'''

BACKEND_SCRIPTS = {"calendar": SCRIPT_CALENDAR, "outlook": SCRIPT_OUTLOOK}


# EventKit authorization status codes, for readable diagnostics.
EK_STATUS = {
    0: "notDetermined", 1: "restricted", 2: "denied",
    3: "authorized", 4: "writeOnly", 5: "fullAccess",
}
EK_OK = (3, 5)

# EKEventStatusCanceled / EKEventAvailabilityFree / EKParticipantStatusDeclined.
EK_CANCELLED = 3
EK_FREE = 1
EK_DECLINED = 3


class EventKitSource:
    """Reads events straight out of the calendar database via EventKit.

    This is the backend that actually works on current macOS: it sees every
    account Calendar.app knows about, Exchange/Outlook included, with none of
    the AppleEvent timeouts the AppleScript route suffers from.

    macOS will prompt for calendar access the first time. The prompt is
    attributed to the Python binary running this, so grant it once from a
    terminal before handing the job to launchd - a prompt raised by a launchd
    agent is easy to miss and the request just times out.
    """

    def __init__(self):
        self.store = None
        self.error = None
        self._ek = None
        self._nsdate = None

    def _load(self):
        if self.store is not None:
            return True
        try:
            import EventKit
            from Foundation import NSDate
        except ImportError as exc:
            self.error = ("EventKit bindings missing (%s) - "
                          "pip install pyobjc-framework-EventKit" % exc)
            return False
        self._ek = EventKit
        self._nsdate = NSDate
        self.store = EventKit.EKEventStore.alloc().init()
        return True

    def status(self):
        """Raw authorization status, or None if the bindings are missing."""
        if not self._load():
            return None
        return self._ek.EKEventStore.authorizationStatusForEntityType_(
            self._ek.EKEntityTypeEvent
        )

    def status_name(self):
        st = self.status()
        return "unavailable" if st is None else EK_STATUS.get(st, str(st))

    def authorized(self):
        return self.status() in EK_OK

    def request_access(self, wait=120.0):
        """Ask for calendar access. MUST be called on the main thread.

        The TCC prompt is presented by the system on the main run loop, so the
        run loop has to be pumped while we wait - a plain Event.wait() blocks
        the thread the prompt needs and the request silently never completes.
        That is why the daemon does this once at startup rather than lazily
        from the polling thread.
        """
        if not self._load():
            return False
        st = self.status()
        if st in EK_OK:
            return True
        if st == 2:
            self.error = ("calendar access denied - turn it back on in System "
                          "Settings > Privacy & Security > Calendars")
            return False
        if st == 1:
            self.error = "calendar access restricted by policy"
            return False

        done = threading.Event()
        result = {}

        def handler(granted, err):
            result["granted"] = bool(granted)
            result["error"] = err
            done.set()

        if hasattr(self.store, "requestFullAccessToEventsWithCompletion_"):
            self.store.requestFullAccessToEventsWithCompletion_(handler)
        else:
            self.store.requestAccessToEntityType_completion_(
                self._ek.EKEntityTypeEvent, handler
            )

        try:
            from Foundation import NSRunLoop, NSDate as _NSDate
            loop = NSRunLoop.currentRunLoop()
            deadline = time.monotonic() + wait
            while not done.is_set() and time.monotonic() < deadline:
                loop.runUntilDate_(_NSDate.dateWithTimeIntervalSinceNow_(0.25))
        except ImportError:
            done.wait(wait)

        if not done.is_set():
            self.error = ("no answer to the calendar permission prompt within "
                          "%.0fs - run --probe from your own terminal and "
                          "click Allow" % wait)
            return False
        if not result.get("granted"):
            self.error = "calendar access not granted (%s)" % result.get("error")
            return False
        self.error = None
        return True

    def events(self, lead_seconds):
        """[(title, seconds_until_start, seconds_until_end)] overlapping now..lead."""
        if not self._load():
            raise RuntimeError(self.error or "EventKit unavailable")
        if not self.authorized():
            raise RuntimeError(
                self.error or "no calendar access (status: %s) - run --probe "
                "from a terminal and click Allow" % self.status_name()
            )
        start = self._nsdate.date()
        end = self._nsdate.dateWithTimeIntervalSinceNow_(float(lead_seconds))
        pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
            start, end, None
        )
        out = []
        for ev in (self.store.eventsMatchingPredicate_(pred) or []):
            try:
                if ev.isAllDay():
                    continue
                if ev.status() == EK_CANCELLED:
                    continue
                if ev.availability() == EK_FREE:
                    continue
                # A meeting you declined is not a meeting you are in. Exchange
                # keeps declined invitations on the calendar marked Busy, so
                # without this they would light the LED red.
                declined = False
                for person in (ev.attendees() or []):
                    if person.isCurrentUser():
                        declined = person.participantStatus() == EK_DECLINED
                        break
                if declined:
                    continue
                title = str(ev.title() or "(untitled)")
                begins = int(ev.startDate().timeIntervalSinceNow())
                ends = int(ev.endDate().timeIntervalSinceNow())
                out.append((title, begins, ends))
            except Exception:
                continue        # one odd event must not kill the poll
        return out


EVENTKIT = EventKitSource()


class CalendarWatcher:
    """Polls calendars on a background thread; main loop reads cached state."""

    def __init__(self, backends, lead_minutes, ignore, poll, verbose=False):
        self.backends = list(backends)
        self.lead = int(lead_minutes) * 60
        self.ignore = [re.compile(p, re.I) for p in ignore]
        self.poll = poll
        self.verbose = verbose
        self.failures = {b: 0 for b in self.backends}
        self._lock = threading.Lock()
        self._current = None   # title of event in progress
        self._next = None      # (title, seconds_until_start)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:          # never let the thread die
                log("calendar: %s" % exc)
            self._stop.wait(self.poll)

    def _query(self, backend):
        if backend == "eventkit":
            return [e for e in EVENTKIT.events(self.lead)
                    if not any(rx.search(e[0]) for rx in self.ignore)]
        script = BACKEND_SCRIPTS[backend]
        proc = subprocess.run(
            ["osascript", "-", str(self.lead)],
            input=script, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "osascript failed").strip())
        events = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            title = parts[0].strip()
            try:
                start, end = int(parts[1]), int(parts[2])
            except ValueError:
                continue
            if any(rx.search(title) for rx in self.ignore):
                continue
            events.append((title, start, end))
        return events

    def refresh(self):
        events = []
        for backend in list(self.backends):
            try:
                events.extend(self._query(backend))
                self.failures[backend] = 0
            except Exception as exc:
                self.failures[backend] += 1
                log("calendar backend %r failed (%d): %s"
                    % (backend, self.failures[backend], exc))
                if self.failures[backend] >= 3:
                    log("calendar backend %r disabled" % backend)
                    self.backends.remove(backend)

        current = None
        upcoming = None
        for title, start, end in events:
            if start <= 0 < end:
                current = title if current is None else current
            elif 0 < start <= self.lead:
                if upcoming is None or start < upcoming[1]:
                    upcoming = (title, start)
        with self._lock:
            self._current, self._next = current, upcoming
        if self.verbose:
            log("calendar: now=%r next=%r (%d events)"
                % (current, upcoming, len(events)))

    def state(self):
        with self._lock:
            return self._current, self._next


# --------------------------------------------------------------------------
# Signal 3: Focus / Do Not Disturb (best effort)
# --------------------------------------------------------------------------

DND_PATHS = [
    "~/Library/DoNotDisturb/DB/Assertions.json",
    "~/Library/Group Containers/group.com.apple.donotdisturb/"
    "Library/DoNotDisturb/DB/Assertions.json",
]


def dnd_active():
    """True / False, or None when the OS does not expose it here."""
    for path in DND_PATHS:
        p = os.path.expanduser(path)
        if not os.path.isfile(p):
            continue
        try:
            with open(p) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        records = data.get("data") or []
        for rec in records:
            details = rec.get("storeAssertionRecords") or []
            if details:
                return True
        return False
    return None


# --------------------------------------------------------------------------
# Signal 4: manual override file
# --------------------------------------------------------------------------


def override_state(path):
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        return None
    try:
        with open(p) as fh:
            val = fh.read().strip().upper()
    except OSError:
        return None
    if not val or val in ("AUTO", "NONE", "CLEAR"):
        return None
    return val if val in STATES else None


# --------------------------------------------------------------------------
# Resolve
# --------------------------------------------------------------------------


def resolve(cfg, mic, cal):
    """Returns (state, reason)."""
    forced = override_state(cfg["override_file"])
    if forced:
        return forced, "override file"

    if cfg["use_mic"] and mic is not None:
        name = mic.active()
        if name:
            return "MEETING", "mic in use (%s)" % name

    current = nxt = None
    if cfg["use_calendar"] and cal is not None:
        current, nxt = cal.state()
    if current:
        return "MEETING", "calendar: %s" % current

    if cfg["use_mic"] and mic is not None:
        name = mic.meeting_audio()
        if name:
            return "MEETING", "meeting app audio (%s)" % name

    if cfg["use_dnd"] and dnd_active():
        return "BUSY", "do not disturb"

    if nxt:
        return "UPCOMING", "in %d min: %s" % (max(1, nxt[1] // 60), nxt[0])

    return "FREE", "idle"


# --------------------------------------------------------------------------
# Serial link
# --------------------------------------------------------------------------


class SerialLink:
    """Auto-detecting, self-reconnecting line protocol to the Arduino."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.ser = None
        self.port = None
        self._warned = False

    def find_port(self):
        if self.cfg.get("port"):
            p = self.cfg["port"]
            return p if os.path.exists(p) else None
        for pattern in self.cfg["port_globs"]:
            hits = sorted(glob.glob(pattern))
            if hits:
                return hits[0]
        return None

    def connect(self):
        import serial                        # pyserial

        port = self.find_port()
        if not port:
            if not self._warned:
                log("serial: no Arduino found (globs: %s)"
                    % ", ".join(self.cfg["port_globs"]))
                self._warned = True
            return False
        try:
            # write_timeout matters: without it a write to a board that has
            # stopped consuming bytes blocks the whole daemon forever.
            self.ser = serial.Serial(port, self.cfg["baud"],
                                     timeout=0.2, write_timeout=2.0)
        except Exception as exc:
            if not self._warned:
                log("serial: cannot open %s: %s" % (port, exc))
                self._warned = True
            return False
        self.port = port
        self._warned = False
        # Opening the port toggles DTR, which resets most Arduino boards.
        # Give the bootloader time before talking to it.
        time.sleep(2.0)
        self.ser.reset_input_buffer()
        log("serial: connected on %s @ %d" % (port, self.cfg["baud"]))
        return True

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def send(self, state):
        if self.ser is None and not self.connect():
            return False
        try:
            # No flush()/tcdrain() here on purpose - it blocks indefinitely
            # when the far end stops reading. write_timeout covers us instead.
            self.ser.write(("STAT %s\n" % state).encode("ascii"))
            if self.cfg.get("verbose"):
                reply = self.ser.readline().decode("ascii", "replace").strip()
                if reply:
                    log("serial: <- %s" % reply)
            return True
        except Exception as exc:
            log("serial: write failed on %s: %s" % (self.port, exc))
            self.close()
            return False


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def cmd_probe(cfg):
    mic = MicWatcher(cfg["mic_ignore"], cfg["proc_ignore"],
                     cfg["meeting_apps"], cfg["meeting_app_hold"])
    print("CoreAudio available: %s" % mic.available)
    print("Per-process capture API: %s"
          % ("yes" if mic.process_api
             else "NO - falling back to the device flag, which counts "
                  "headset playback as capture"))

    if mic.process_api:
        print("\nProcesses using audio (capture is what decides):")
        rows = [r for r in mic.processes() if r[1] or r[2]]
        if not rows:
            print("  (nothing playing or capturing)")
        for name, capturing, playing, ignored in rows:
            print("  %s %s  %s%s" % ("CAPTURING" if capturing else "         ",
                                     "playing" if playing else "       ",
                                     name,
                                     "   (ignored)" if ignored else ""))

    print("\nAudio input devices (device-wide flag, headsets go true on "
          "playback too):")
    for name, running, ignored in mic.inputs():
        flag = "running" if running else "idle   "
        print("  %s  %s%s" % (flag, name, "   (ignored)" if ignored else ""))

    print("\nmic active: %r" % mic.active())
    cands = mic.meeting_candidates()
    print("meeting apps playing: %s  (counts after %gs of continuous playback)"
          % (", ".join(cands) if cands else "none", mic.app_hold))

    print("\nFocus / DND: %r  (None = not readable on this macOS)"
          % dnd_active())
    print("override file %s: %r"
          % (cfg["override_file"], override_state(cfg["override_file"])))

    print("\nEventKit calendar access: %s" % EVENTKIT.status_name())
    if not EVENTKIT.authorized():
        print("Requesting access - a system prompt should appear now...")
        EVENTKIT.request_access(wait=120.0)
        print("EventKit calendar access: %s" % EVENTKIT.status_name())

    cal = CalendarWatcher(cfg["calendar_backends"], cfg["lead_minutes"],
                          cfg["event_ignore"], cfg["calendar_poll"],
                          verbose=True)
    print("Backends: %s" % ", ".join(cfg["calendar_backends"]))
    print("Querying calendars (first run raises a permission prompt - "
          "click Allow)...")
    cal.refresh()
    print("  in progress: %r" % (cal.state()[0],))
    print("  next:        %r" % (cal.state()[1],))
    if EVENTKIT.error:
        print("  eventkit: %s" % EVENTKIT.error)

    state, reason = resolve(cfg, mic, cal)
    print("\n=> %s  (%s)" % (state, reason))

    link = SerialLink(cfg)
    print("\nSerial port: %r" % link.find_port())


def run_daemon(cfg, use_serial=True):
    mic = (MicWatcher(cfg["mic_ignore"], cfg["proc_ignore"],
                      cfg["meeting_apps"], cfg["meeting_app_hold"])
           if cfg["use_mic"] else None)
    if mic is not None and not mic.available:
        log("mic: CoreAudio unavailable, mic signal disabled")
        mic = None

    cal = None
    if cfg["use_calendar"] and cfg["calendar_backends"]:
        if "eventkit" in cfg["calendar_backends"]:
            # Main thread, before the polling thread exists: the permission
            # prompt needs this run loop.
            #
            # Only ask when a human is watching. Under launchd there is nobody
            # to click the prompt and the system cannot present it anyway, so
            # requesting would just stall startup for the whole timeout. In
            # that case check the existing grant and carry on.
            interactive = sys.stdin.isatty()
            if EVENTKIT.authorized():
                log("calendar: EventKit access %s" % EVENTKIT.status_name())
            elif interactive:
                if EVENTKIT.request_access(wait=cfg.get("auth_wait", 120.0)):
                    log("calendar: EventKit access %s" % EVENTKIT.status_name())
                else:
                    log("calendar: %s" % EVENTKIT.error)
            else:
                log("calendar: EventKit access is %s and nothing is attached "
                    "to prompt on - run --probe in a terminal once to grant "
                    "it; the calendar signal is off until then"
                    % EVENTKIT.status_name())
        cal = CalendarWatcher(cfg["calendar_backends"], cfg["lead_minutes"],
                              cfg["event_ignore"], cfg["calendar_poll"],
                              verbose=cfg["verbose"])
        cal.start()

    link = SerialLink(cfg) if use_serial else None

    stop = threading.Event()

    def handle_signal(signum, _frame):
        log("signal %d, shutting down" % signum)
        stop.set()

    try:
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
    except ValueError:
        pass          # not on the main thread; caller handles shutdown

    last_state = None
    last_reason = None
    last_sent = 0.0
    log("started (mic=%s calendar=%s dnd=%s serial=%s)"
        % (mic is not None, cal is not None, cfg["use_dnd"], use_serial))

    while not stop.is_set():
        state, reason = resolve(cfg, mic, cal)
        now = time.monotonic()
        changed = state != last_state
        due = now - last_sent >= cfg["heartbeat"]
        if changed:
            log("%s  <- %s" % (state, reason))
        elif reason != last_reason and cfg.get("verbose"):
            # Same state, new cause. Worth a line: it is the only way to tell
            # "still red because the mic is open" from "still red because the
            # calendar says so", which matters when you are working out why
            # the light will not go green.
            log("%s  .. %s" % (state, reason))
        last_reason = reason
        if link and (changed or due):
            if link.send(state):
                last_sent = now
        last_state = state
        stop.wait(cfg["mic_poll"])

    if link:
        link.send("OFF")
        link.close()
    if cal:
        cal.stop()
    log("stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--probe", action="store_true",
                    help="dump every signal and exit")
    ap.add_argument("--once", action="store_true",
                    help="print the resolved status and exit")
    ap.add_argument("--no-serial", action="store_true",
                    help="run detection only, do not talk to the Arduino")
    ap.add_argument("--send", metavar="STATE", choices=STATES,
                    help="send one state to the Arduino and exit (%s)"
                         % "|".join(STATES))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.verbose:
        cfg["verbose"] = True

    if args.send:
        link = SerialLink(cfg)
        ok = link.send(args.send)
        link.close()
        print("sent %s: %s" % (args.send, "ok" if ok else "failed"))
        return 0 if ok else 1

    if args.probe:
        cmd_probe(cfg)
        return 0

    if args.once:
        mic = (MicWatcher(cfg["mic_ignore"], cfg["proc_ignore"],
                          cfg["meeting_apps"], cfg["meeting_app_hold"])
               if cfg["use_mic"] else None)
        cal = None
        if cfg["use_calendar"]:
            if "eventkit" in cfg["calendar_backends"]:
                EVENTKIT.request_access(wait=120.0)
            cal = CalendarWatcher(cfg["calendar_backends"],
                                  cfg["lead_minutes"], cfg["event_ignore"],
                                  cfg["calendar_poll"])
            cal.refresh()
        state, reason = resolve(cfg, mic, cal)
        print("%s  (%s)" % (state, reason))
        return 0

    run_daemon(cfg, use_serial=not args.no_serial)
    return 0


if __name__ == "__main__":
    sys.exit(main())
