"""macOS signal sources: CoreAudio for the mic, EventKit for the calendar,
and the Do Not Disturb assertions file for Focus.

meeting_status.py imports one of these per-platform modules by sys.platform
and uses nothing from it beyond the contract below, so the detection logic
never grows an `if darwin` branch:

    DEFAULT_PROC_IGNORE, DEFAULT_MEETING_APPS   platform-shaped config defaults
    AUDIO_API_NAME, PROCESS_API_HINT            wording for logs and --probe
    CALENDAR_NAME, DND_HINT
    MicWatcher(proc_ignore, meeting_apps, app_hold)
        .available, .process_api, .snapshot() -> AudioState
    CALENDAR
        .authorized(), .status_name(), .request_access(wait),
        .events(lead_seconds) -> [(title, secs_to_start, secs_to_end)]
    dnd_active() -> True / False / None
"""

import ctypes
import ctypes.util
import json
import os
import threading
import time

from common import collapse_audio, compile_patterns, log

# --------------------------------------------------------------------------
# Platform wording and defaults
# --------------------------------------------------------------------------

AUDIO_API_NAME = "CoreAudio"
PROCESS_API_HINT = "needs macOS 14.2 or newer"
CALENDAR_NAME = "EventKit"
DND_HINT = "None = not readable on this macOS"

# Processes that may legitimately hold the microphone without it meaning you
# are in a meeting. Substring match on the bundle id, case insensitive.
DEFAULT_PROC_IGNORE = [
    "com.apple.corespeech",            # Siri / "Hey Siri"
    "com.apple.assistantd",
    "com.apple.accessibility.heard",
    "com.apple.universalaccessd",
    "com.apple.controlcenter",
]

# Apps whose audio *playback* means you are in a meeting, even when the mic is
# never opened (a listen-only Zoom webinar, a muted-by-host call). Regex,
# matched case insensitively against the bundle id. Browsers are deliberately
# absent: a browser plays everything, not just meetings.
DEFAULT_MEETING_APPS = [
    r"us\.zoom\.",
    r"com\.microsoft\.teams",
    r"com\.tinyspeck\.slack",
    r"webex|cisco-systems\.spark",
    r"com\.google\.chrome\.helper\.meet",
]


# --------------------------------------------------------------------------
# Microphone, via CoreAudio (ctypes, no dependencies)
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
        self.meeting_apps = compile_patterns(meeting_apps, "meeting_apps")
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
        """
        return collapse_audio(self.processes(), self.meeting_apps,
                              self.app_hold, self._playing_since)


# --------------------------------------------------------------------------
# Calendar, via EventKit
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


CALENDAR = EventKitSource()


# --------------------------------------------------------------------------
# Focus / Do Not Disturb (best effort)
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
