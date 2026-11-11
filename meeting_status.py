#!/usr/bin/env python3
"""
meeting-status: watch macOS for "am I in a meeting?" and push the answer to an
Arduino over USB serial, which drives an RGB LED.

Signals, highest precedence first. The first one that fires wins:

  1. override  - a state you write into a file by hand, beats everything else.
  2. mic       - a process is *capturing* audio, via CoreAudio's per-process
                 API. Catches Teams, Zoom, Meet and Slack huddles without
                 knowing anything about those apps. Needs no permissions.
  3. calendar  - an event is in progress, read from the calendar database via
                 EventKit. Needs a one-time Calendar grant.
  4. app audio - a known meeting app has been *playing* audio for a while.
                 Catches the listen-only webinar the mic signal cannot see.
  5. dnd       - macOS Focus / Do Not Disturb, best effort (the file moves
                 between OS releases; silently skipped when unreadable).
  6. calendar  - an event starts within lead_minutes.

Resolved states:

  MEETING   capturing audio, an event is on now, or a meeting app is playing
  BUSY      Focus / DND is on
  UPCOMING  an event starts within lead_minutes
  FREE      none of the above
  OFF       sent on shutdown, and settable by hand

Usage:
  meeting_status.py                 run the daemon
  meeting_status.py --probe         show every signal and what it says
  meeting_status.py --once          print the resolved status and exit
  meeting_status.py --no-serial     run the detection loop without an Arduino
  meeting_status.py --send MEETING  force a state onto the LED and exit
"""

import argparse
import collections
import ctypes
import ctypes.util
import glob
import json
import os
import re
import signal
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
    "lead_minutes": 5,
    # Events whose title matches any of these regexes are ignored (case
    # insensitive). All-day events are always ignored.
    # Titles that are busy blocks rather than meetings. "due" catches the
    # deadline reminders Outlook users tend to park on the calendar as Busy.
    "event_ignore": ["^focus", "^lunch", "^hold\\b", "block", "no meeting",
                     "\\bdue\\b", "\\bwalk time\\b", "^ooo\\b", "^pto\\b"],
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
    # Same, when running in the background with nobody at a terminal.
    # Short on purpose: the prompt still appears on screen, but startup
    # must not hang waiting for someone to walk past and click it.
    "auth_wait_background": 25.0,
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
_S_GLOBAL = _fourcc("glob")

# Per-process audio objects, macOS 14.2+. These selectors are easy to get
# wrong: input/output are 'piri'/'piro', verified against live playback.
_P_PROCESSES = _fourcc("prs#")    # kAudioHardwarePropertyProcessObjectList
_P_PID = _fourcc("ppid")          # kAudioProcessPropertyPID
_P_BUNDLE_ID = _fourcc("pbid")    # kAudioProcessPropertyBundleID
_P_RUNNING_IN = _fourcc("piri")   # kAudioProcessPropertyIsRunningInput
_P_RUNNING_OUT = _fourcc("piro")  # kAudioProcessPropertyIsRunningOutput
_ELEM_MAIN = 0
_CF_UTF8 = 0x08000100


AudioState = collections.namedtuple(
    "AudioState", "capturing meeting_app candidates rows")


class MicWatcher:
    """Reports what audio is doing right now, via CoreAudio's per-process API.

    CoreAudio exposes one audio object per process, each carrying
    kAudioProcessPropertyIsRunningInput/Output, so we can ask "is anything
    capturing?" and name the culprit by bundle id.

    The obvious alternative is wrong, and this used to use it: per-device
    kAudioDevicePropertyDeviceIsRunningSomewhere. A USB or Bluetooth headset is
    a SINGLE audio device carrying both input and output streams, and that
    property is device-wide with no input scope - so playing music through a
    headset marks the device as running and read as a meeting. There is no way
    to tell capture from playback that way, which is why this needs macOS 14.2
    or newer and switches the mic signal off rather than guess on older ones.

    It cares about capture being live, not about permission or hardware: an app
    holding mic permission without recording does not register, and connecting
    a headset only adds an idle device.
    """

    def __init__(self, proc_ignore=(), meeting_apps=(), app_hold=20.0):
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

    # -- the decision --

    def snapshot(self):
        """Everything the mic signal needs, from one pass over the processes.

        Capture and meeting-app playback used to be two calls, which meant two
        full CoreAudio enumerations per poll to answer one question.

        Playback has to persist for app_hold seconds before it counts, or every
        Slack notification chime would read as a meeting.
        """
        rows = self.processes()
        now = time.time()
        capturing = None
        meeting_app = None
        playing_now = set()
        for name, is_capturing, is_playing, ignored in rows:
            if ignored:
                continue
            if is_capturing and capturing is None:
                capturing = name
            if not is_playing:
                continue
            if not any(rx.search(name) for rx in self.meeting_apps):
                continue
            playing_now.add(name)
            since = self._playing_since.setdefault(name, now)
            if now - since >= self.app_hold and meeting_app is None:
                meeting_app = name
        # Forget apps that have stopped, so the next burst starts its own clock.
        for name in [n for n in self._playing_since if n not in playing_now]:
            del self._playing_since[name]
        return AudioState(capturing, meeting_app, sorted(playing_now), rows)


# --------------------------------------------------------------------------
# Signal 2: calendar, via EventKit
# --------------------------------------------------------------------------


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

    macOS prompts for calendar access the first time. The grant belongs to the
    identity of the process that asked, so granting it in a terminal does not
    grant it to the launchd agent - the agent has to ask for itself, which it
    can, being a user agent in the GUI session.
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

        from Foundation import NSRunLoop
        loop = NSRunLoop.currentRunLoop()
        deadline = time.monotonic() + wait
        while not done.is_set() and time.monotonic() < deadline:
            loop.runUntilDate_(self._nsdate.dateWithTimeIntervalSinceNow_(0.25))

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
    """Polls EventKit on a background thread; the main loop reads the cache."""

    def __init__(self, lead_minutes, ignore, poll, verbose=False):
        self.lead = int(lead_minutes) * 60
        self.ignore = [re.compile(p, re.I) for p in ignore]
        self.poll = poll
        self.verbose = verbose
        self.failures = 0
        self._lock = threading.Lock()
        self._current = None   # title of the event in progress
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

    def refresh(self):
        try:
            events = [e for e in EVENTKIT.events(self.lead)
                      if not any(rx.search(e[0]) for rx in self.ignore)]
        except Exception as exc:
            self.failures += 1
            log("calendar: read failed (%d): %s" % (self.failures, exc))
            # Keep the last good answer. Blanking it here would drop the light
            # to FREE mid-meeting on one transient error, and the old code also
            # disabled the calendar permanently after three - which, with one
            # backend, meant three blips killed the signal until a restart.
            return
        self.failures = 0

        current = None
        upcoming = None
        for title, start, end in events:
            if start <= 0 < end:
                if current is None:
                    current = title
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

    audio = mic.snapshot() if mic is not None else None
    if audio and audio.capturing:
        return "MEETING", "mic in use (%s)" % audio.capturing

    current = nxt = None
    if cal is not None:
        current, nxt = cal.state()
    if current:
        return "MEETING", "calendar: %s" % current

    # Ranked below the calendar so a named meeting gives the better reason.
    if audio and audio.meeting_app:
        return "MEETING", "meeting app audio (%s)" % audio.meeting_app

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


def build_mic(cfg):
    """A MicWatcher, or None when the mic signal cannot or should not run."""
    if not cfg["use_mic"]:
        return None
    mic = MicWatcher(cfg["proc_ignore"], cfg["meeting_apps"],
                     cfg["meeting_app_hold"])
    if not mic.available:
        log("mic: CoreAudio unavailable, mic signal off")
        return None
    if not mic.process_api:
        log("mic: no per-process audio API (needs macOS 14.2 or newer), "
            "mic signal off")
        return None
    return mic


def build_calendar(cfg):
    """A CalendarWatcher, not yet started, or None. Call on the main thread.

    The permission prompt is presented on the main run loop, so asking has to
    happen here rather than lazily from the polling thread.

    A TCC grant is tied to the identity of the process that asked, so a grant
    given to a terminal run does not carry over to the launchd agent - the
    agent asks for itself. It can: a user agent runs in the GUI session. What
    it cannot do is wait two minutes for someone to notice the prompt, so the
    background wait is short and startup carries on regardless. The mic signal
    needs no permission and works meanwhile.
    """
    if not cfg["use_calendar"]:
        return None
    if EVENTKIT.authorized():
        log("calendar: EventKit access %s" % EVENTKIT.status_name())
    else:
        interactive = sys.stdin.isatty()
        wait = cfg["auth_wait"] if interactive else cfg["auth_wait_background"]
        if EVENTKIT.request_access(wait=wait):
            log("calendar: EventKit access %s" % EVENTKIT.status_name())
        elif interactive:
            log("calendar: %s" % EVENTKIT.error)
        else:
            log("calendar: EventKit access is %s after asking for %gs - if no "
                "prompt appeared, run --probe in a terminal; the calendar "
                "signal is off until it is granted"
                % (EVENTKIT.status_name(), wait))
    return CalendarWatcher(cfg["lead_minutes"], cfg["event_ignore"],
                           cfg["calendar_poll"], verbose=cfg["verbose"])


def cmd_probe(cfg):
    mic = MicWatcher(cfg["proc_ignore"], cfg["meeting_apps"],
                     cfg["meeting_app_hold"])
    print("CoreAudio available: %s" % mic.available)
    print("Per-process capture API: %s"
          % ("yes" if mic.process_api
             else "NO - needs macOS 14.2 or newer; the mic signal is off"))

    audio = mic.snapshot()
    print("\nProcesses using audio (capture is what decides):")
    rows = [r for r in audio.rows if r[1] or r[2]]
    if not rows:
        print("  (nothing playing or capturing)")
    for name, capturing, playing, ignored in rows:
        print("  %s %s  %s%s" % ("CAPTURING" if capturing else "         ",
                                 "playing" if playing else "       ",
                                 name,
                                 "   (ignored)" if ignored else ""))

    print("\nmic active: %r" % audio.capturing)
    print("meeting apps playing: %s  (counts after %gs of continuous "
          "playback)" % (", ".join(audio.candidates) or "none", mic.app_hold))

    print("\nFocus / DND: %r  (None = not readable on this macOS)"
          % dnd_active())
    print("override file %s: %r"
          % (cfg["override_file"], override_state(cfg["override_file"])))

    print("\nEventKit calendar access: %s" % EVENTKIT.status_name())
    if not EVENTKIT.authorized():
        print("Requesting access - a system prompt should appear now...")
        EVENTKIT.request_access(wait=cfg["auth_wait"])
        print("EventKit calendar access: %s" % EVENTKIT.status_name())

    cal = CalendarWatcher(cfg["lead_minutes"], cfg["event_ignore"],
                          cfg["calendar_poll"], verbose=True)
    print("Querying the calendar...")
    cal.refresh()
    print("  in progress: %r" % (cal.state()[0],))
    print("  next:        %r" % (cal.state()[1],))
    if EVENTKIT.error:
        print("  eventkit: %s" % EVENTKIT.error)

    state, reason = resolve(cfg, mic if cfg["use_mic"] else None, cal)
    print("\n=> %s  (%s)" % (state, reason))
    print("\nSerial port: %r" % SerialLink(cfg).find_port())


def run_daemon(cfg, use_serial=True):
    mic = build_mic(cfg)
    cal = build_calendar(cfg)
    if cal is not None:
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
        if changed:
            log("%s  <- %s" % (state, reason))
        elif reason != last_reason and cfg["verbose"]:
            # Same state, new cause. Worth a line: it is the only way to tell
            # "still red because the mic is open" from "still red because the
            # calendar says so", which matters when you are working out why
            # the light will not go green.
            log("%s  .. %s" % (state, reason))
        last_reason = reason
        if link and (changed or now - last_sent >= cfg["heartbeat"]):
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
        mic = build_mic(cfg)
        cal = build_calendar(cfg)
        if cal is not None:
            cal.refresh()
        state, reason = resolve(cfg, mic, cal)
        print("%s  (%s)" % (state, reason))
        return 0

    run_daemon(cfg, use_serial=not args.no_serial)
    return 0


if __name__ == "__main__":
    sys.exit(main())
