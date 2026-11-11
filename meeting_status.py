#!/usr/bin/env python3
"""
meeting-status: work out whether you are in a meeting and push the answer to
an Arduino over USB serial, which drives an RGB LED.

Runs on macOS and Windows. Everything platform-specific lives in
signals_darwin.py / signals_win32.py, picked below by sys.platform; the
signals themselves, and the order they are consulted in, are the same on both.

Signals, highest precedence first. The first one that fires wins:

  1. override  - a state you write into a file by hand, beats everything else.
  2. mic       - a process is *capturing* audio. Catches Teams, Zoom, Meet and
                 Slack huddles without knowing anything about those apps.
  3. calendar  - an event is in progress, read from the local calendar.
  4. app audio - a known meeting app has been *playing* audio for a while.
                 Catches the listen-only webinar the mic signal cannot see.
  5. dnd       - Focus / Do Not Disturb, best effort. Skipped silently where
                 the OS does not expose it.
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
import json
import os
import re
import signal
import sys
import threading
import time

from common import STATES, log

# The platform seam. Each module provides the same handful of names; see the
# contract in signals_darwin.py. Importing by platform rather than branching
# inside the signals keeps the detection logic below free of OS conditionals.
if sys.platform == "darwin":
    import signals_darwin as signals
elif sys.platform == "win32":
    import signals_win32 as signals
else:
    raise SystemExit(
        "meeting-status supports macOS and Windows; this is %r. The signals "
        "are the OS-specific part - port signals_darwin.py to add another."
        % sys.platform
    )

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # Serial. port=null auto-detects the first matching device.
    "port": None,
    # Patterns tried against each serial port's "vid:pid", device name and
    # description, case insensitively, first match wins. USB ids rather than
    # device-path globs because the path is OS-shaped (/dev/cu.* on macOS,
    # COM3 on Windows) while the id of the USB-serial bridge is not.
    "port_match": [
        "1a86:7523",      # CH340 - the usual Arduino clone bridge
        "1a86:55d4",      # CH9102
        "2341:",          # Arduino SA
        "2a03:",          # Arduino.org era boards
        "0403:6001",      # FTDI FT232R
        "10c4:ea60",      # Silicon Labs CP210x
        "usbmodem", "usbserial", "wchusbserial",   # macOS device names
        "ch340", "arduino",                        # Windows descriptions
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
    # These two name processes, and processes are named differently on each
    # OS - bundle ids on macOS, executable names on Windows - so the defaults
    # come from the platform module. Overriding either in config.json replaces
    # the platform default wholesale; run --probe to see the names to use.
    "proc_ignore": list(signals.DEFAULT_PROC_IGNORE),
    "meeting_apps": list(signals.DEFAULT_MEETING_APPS),
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


# --------------------------------------------------------------------------
# Calendar polling (platform-independent; the source is not)
# --------------------------------------------------------------------------


class CalendarWatcher:
    """Polls a calendar source on a background thread; the loop reads a cache.

    The source is injected rather than reached for, so the same polling,
    caching and filtering serves EventKit on macOS and Outlook on Windows.
    """

    def __init__(self, source, lead_minutes, ignore, poll, verbose=False):
        self.source = source
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
            events = [e for e in self.source.events(self.lead)
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

    if cfg["use_dnd"] and signals.dnd_active():
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
        """The configured port, or the first one matching port_match."""
        if self.cfg.get("port"):
            p = self.cfg["port"]
            # A COM name is not a filesystem path, so only check existence
            # where it is one; on Windows, let the open attempt be the test.
            if os.sep in p or p.startswith("/"):
                return p if os.path.exists(p) else None
            return p
        from serial.tools import list_ports     # pyserial

        patterns = [s.lower() for s in self.cfg["port_match"]]
        best = None
        for info in list_ports.comports():
            ident = "%04x:%04x" % (info.vid, info.pid) if info.vid else ""
            haystack = " ".join(
                str(x).lower() for x in
                (ident, info.device, info.description, info.manufacturer)
                if x and x != "n/a"
            )
            if not any(p in haystack for p in patterns):
                continue
            # On macOS the same bridge appears as both /dev/cu.* and
            # /dev/tty.*; cu is the one that does not block waiting for DCD.
            if "/dev/tty." in info.device:
                continue
            if best is None or info.device < best:
                best = info.device
        return best

    def connect(self):
        import serial                        # pyserial

        port = self.find_port()
        if not port:
            if not self._warned:
                log("serial: no Arduino found (port_match: %s)"
                    % ", ".join(self.cfg["port_match"]))
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
    mic = signals.MicWatcher(cfg["proc_ignore"], cfg["meeting_apps"],
                             cfg["meeting_app_hold"])
    if not mic.available:
        log("mic: %s unavailable, mic signal off" % signals.AUDIO_API_NAME)
        return None
    if not mic.process_api:
        log("mic: no per-process audio API (%s), mic signal off"
            % signals.PROCESS_API_HINT)
        return None
    return mic


def build_calendar(cfg):
    """A CalendarWatcher, not yet started, or None. Call on the main thread.

    Where the platform needs a permission grant, the prompt is presented on
    the main run loop, so asking has to happen here rather than lazily from
    the polling thread. On a platform that needs no grant this is a no-op
    that reports whether the source answered at all.

    On macOS a TCC grant is tied to the identity of the process that asked, so
    a grant given to a terminal run does not carry over to the launchd agent -
    the agent asks for itself. It can: a user agent runs in the GUI session.
    What it cannot do is wait two minutes for someone to notice the prompt, so
    the background wait is short and startup carries on regardless. The mic
    signal needs no permission and works meanwhile.
    """
    if not cfg["use_calendar"]:
        return None
    name = signals.CALENDAR_NAME
    src = signals.CALENDAR
    if src.authorized():
        log("calendar: %s access %s" % (name, src.status_name()))
    else:
        interactive = sys.stdin.isatty()
        wait = cfg["auth_wait"] if interactive else cfg["auth_wait_background"]
        if src.request_access(wait=wait):
            log("calendar: %s access %s" % (name, src.status_name()))
        elif interactive:
            log("calendar: %s" % src.error)
        else:
            log("calendar: %s access is %s after asking for %gs - if no "
                "prompt appeared, run --probe in a terminal; the calendar "
                "signal is off until it is granted"
                % (name, src.status_name(), wait))
    return CalendarWatcher(src, cfg["lead_minutes"], cfg["event_ignore"],
                           cfg["calendar_poll"], verbose=cfg["verbose"])


def cmd_probe(cfg):
    mic = signals.MicWatcher(cfg["proc_ignore"], cfg["meeting_apps"],
                             cfg["meeting_app_hold"])
    print("%s available: %s" % (signals.AUDIO_API_NAME, mic.available))
    print("Per-process capture API: %s"
          % ("yes" if mic.process_api
             else "NO - %s; the mic signal is off" % signals.PROCESS_API_HINT))

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
    # Windows splits capture and playback across two APIs, so the playback
    # half can be missing on its own. Say so rather than show an empty list.
    if getattr(mic, "audio_error", None):
        print("  note: %s" % mic.audio_error)

    print("\nFocus / DND: %r  (%s)" % (signals.dnd_active(), signals.DND_HINT))
    print("override file %s: %r"
          % (cfg["override_file"], override_state(cfg["override_file"])))

    src = signals.CALENDAR
    name = signals.CALENDAR_NAME
    print("\n%s calendar access: %s" % (name, src.status_name()))
    if not src.authorized():
        print("Requesting access - a system prompt should appear now...")
        src.request_access(wait=cfg["auth_wait"])
        print("%s calendar access: %s" % (name, src.status_name()))

    cal = CalendarWatcher(src, cfg["lead_minutes"], cfg["event_ignore"],
                          cfg["calendar_poll"], verbose=True)
    print("Querying the calendar...")
    cal.refresh()
    print("  in progress: %r" % (cal.state()[0],))
    print("  next:        %r" % (cal.state()[1],))
    if src.error:
        print("  %s: %s" % (name.lower(), src.error))

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
    ap.add_argument("--log", metavar="PATH",
                    help="append all output to PATH instead of stderr "
                         "(the Windows scheduled task uses this; launchd "
                         "redirects for itself)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    # Before anything else, so a failure to start is recorded rather than
    # discarded. log() writes to whatever sys.stderr is at the time, so
    # pointing stderr at the file catches tracebacks as well as log lines.
    if args.log:
        path = os.path.expanduser(args.log)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        sys.stderr = open(path, "a", buffering=1, encoding="utf-8",
                          errors="replace")

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
