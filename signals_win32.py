"""Windows signal sources: the microphone privacy registry for the mic,
WASAPI sessions for meeting-app audio, and Outlook COM for the calendar.

The counterpart to signals_darwin.py; see that file for the contract both
modules satisfy. The differences worth knowing:

  * The mic signal reads the registry Windows itself uses to drive the taskbar
    microphone indicator. Stdlib winreg, no dependency, no admin rights, and
    it attributes use to a process the way CoreAudio does on macOS.
  * Focus Assist is not read at all - see dnd_active() below.
  * Outlook COM needs no permission grant, so the auth_wait config that exists
    for the macOS TCC prompt goes unused here.

The Windows-only imports are guarded so this module can be imported on any
platform for syntax and name checking; everything then reports unavailable.
"""

import ctypes
import datetime
import threading

from common import collapse_audio, compile_patterns, log

try:
    import winreg
except ImportError:            # not Windows: importable, but inert
    winreg = None

# --------------------------------------------------------------------------
# Platform wording and defaults
# --------------------------------------------------------------------------

AUDIO_API_NAME = "Windows audio"
PROCESS_API_HINT = "needs the microphone privacy registry, Windows 10 1903+"
CALENDAR_NAME = "Outlook"
DND_HINT = "None = Focus Assist is not read on Windows"

# Processes that may hold the microphone without it meaning you are in a
# meeting. Substring match on the executable name, case insensitive.
DEFAULT_PROC_IGNORE = [
    "textinputhost.exe",      # voice typing
    "speechruntime.exe",
    "searchhost.exe",         # Windows Search / the old Cortana box
    "searchapp.exe",
    "cortana.exe",
]

# Apps whose audio *playback* means you are in a meeting, even when the mic is
# never opened (a listen-only webinar, a muted-by-host call). Regex, matched
# case insensitively against the executable name or the packaged app identity,
# so "ms-?teams" catches both ms-teams.exe and MSTeams_8wekyb3d8bbwe.
# Browsers are deliberately absent: a browser plays everything.
DEFAULT_MEETING_APPS = [
    r"ms-?teams|^teams\.exe",
    r"zoom",
    r"slack",
    r"webex|ciscocollab|ptoneclk",
    r"lync\.exe",             # Skype for Business
]


# --------------------------------------------------------------------------
# Microphone, via the capability-access registry (stdlib winreg)
# --------------------------------------------------------------------------

# Windows records, per app, when the microphone was last picked up and put
# down. An app that is capturing right now has a start time and no stop time.
# This is the same bookkeeping behind the taskbar microphone indicator.
MIC_CONSENT_KEY = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                   r"\CapabilityAccessManager\ConsentStore\microphone")

# Packaged apps (current Teams) sit in subkeys directly; classic executables
# (Zoom, Slack) sit under NonPackaged with their path mangled, '\' -> '#'.
_NONPACKAGED = "NonPackaged"

_K32 = (ctypes.WinDLL("kernel32", use_last_error=True)
        if hasattr(ctypes, "WinDLL") else None)


def _consent_roots():
    """The registry hives to look in, per-user first."""
    if winreg is None:
        return []
    return [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]


def _subkeys(key):
    names = []
    i = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, i))
        except OSError:
            return names
        i += 1


def _values(key):
    out = {}
    i = 0
    while True:
        try:
            name, value, _type = winreg.EnumValue(key, i)
        except OSError:
            return out
        out[name] = value
        i += 1


def _app_name(keyname):
    """A display name for a consent-store subkey.

    Packaged identities ("MSTeams_8wekyb3d8bbwe") are used as they are, the
    way bundle ids are on macOS. NonPackaged keys are mangled executable
    paths - "C:#Program Files#Zoom#bin#Zoom.exe" - so take the leaf.
    """
    if "#" in keyname:
        return keyname.replace("#", "\\").rsplit("\\", 1)[-1]
    return keyname


def _exe_name(pid):
    """Executable name for a pid, or None. Needs no special privilege."""
    if _K32 is None or not pid:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _K32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                              int(pid))
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buf))
        if not _K32.QueryFullProcessImageNameW(handle, 0, buf,
                                               ctypes.byref(size)):
            return None
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        _K32.CloseHandle(handle)


class MicWatcher:
    """What audio is doing right now, from the registry plus WASAPI.

    Two sources rather than one, because Windows splits what CoreAudio
    exposes together: capture is in the privacy registry (reliable, free, and
    the thing the OS indicator itself uses), while playback has to come from
    enumerating audio sessions. Capture is the signal that decides, so it is
    the one with no dependency - if pycaw is missing, the light still works
    and only the listen-only-webinar case is lost.
    """

    def __init__(self, proc_ignore=(), meeting_apps=(), app_hold=20.0):
        self.proc_ignore = [s.lower() for s in proc_ignore]
        self.meeting_apps = compile_patterns(meeting_apps, "meeting_apps")
        self.app_hold = float(app_hold)
        self._playing_since = {}
        self.available = False
        self.process_api = False
        self.audio_error = None
        self._sessions = None          # None = not tried, False = unavailable
        for root in _consent_roots():
            try:
                winreg.OpenKey(root, MIC_CONSENT_KEY).Close()
            except OSError:
                continue
            self.available = True
            self.process_api = True
            break

    # -- capture, from the registry --

    def _capturing(self):
        """Names of apps holding the microphone right now."""
        names = set()
        for root in _consent_roots():
            try:
                base = winreg.OpenKey(root, MIC_CONSENT_KEY)
            except OSError:
                continue
            with base:
                groups = [("", base)]
                if _NONPACKAGED in _subkeys(base):
                    try:
                        groups.append(
                            (_NONPACKAGED,
                             winreg.OpenKey(base, _NONPACKAGED)))
                    except OSError:
                        pass
                for label, group in groups:
                    for name in _subkeys(group):
                        if not label and name == _NONPACKAGED:
                            continue
                        try:
                            with winreg.OpenKey(group, name) as sub:
                                vals = _values(sub)
                        except OSError:
                            continue
                        start = vals.get("LastUsedTimeStart")
                        stop = vals.get("LastUsedTimeStop")
                        # Picked up and not yet put down.
                        if start and not stop:
                            names.add(_app_name(name))
                    if label:
                        group.Close()
        return names

    # -- playback, from WASAPI via pycaw --

    def _playing(self):
        """Names of processes with an active playback session."""
        if self._sessions is None:
            try:
                import comtypes
                from pycaw.pycaw import AudioUtilities
            except Exception as exc:
                self._sessions = False
                self.audio_error = (
                    "pycaw unavailable (%s) - the meeting-app audio signal is "
                    "off; mic and calendar are unaffected" % exc)
                log("mic: %s" % self.audio_error)
            else:
                # Enumeration happens on whichever thread polls, so COM has
                # to be initialised there; this is a no-op if it already is.
                try:
                    comtypes.CoInitialize()
                except Exception:
                    pass
                self._sessions = AudioUtilities
        if not self._sessions:
            return set()
        names = set()
        try:
            for sess in self._sessions.GetAllSessions():
                if sess.State != 1:        # AudioSessionStateActive
                    continue
                name = _exe_name(sess.ProcessId)
                if name:
                    names.add(name)
        except Exception as exc:
            self.audio_error = "audio session enumeration failed (%s)" % exc
        return names

    def processes(self):
        """[(name, capturing, playing, ignored)] for processes using audio."""
        capturing = self._capturing()
        playing = self._playing()
        rows = []
        for name in sorted(capturing | playing):
            ignored = any(s in name.lower() for s in self.proc_ignore)
            rows.append((name, name in capturing, name in playing, ignored))
        return rows

    def snapshot(self):
        return collapse_audio(self.processes(), self.meeting_apps,
                              self.app_hold, self._playing_since)


# --------------------------------------------------------------------------
# Calendar, via Outlook COM
# --------------------------------------------------------------------------

OL_CALENDAR = 9               # olFolderCalendar
OL_CANCELLED = (5, 7)         # olMeetingCanceled, olMeetingReceivedAndCanceled
OL_FREE = 0                   # olFree
OL_DECLINED = 4               # olResponseDeclined

# Restrict() narrows the window, but a recurring series still expands into one
# item per occurrence, so cap the walk rather than trust the mailbox.
MAX_ITEMS = 500

# Outlook parses this against the user's locale, so it is a format string for
# Outlook's benefit, not ours.
_OL_TIME_FMT = "%m/%d/%Y %I:%M %p"


def _to_datetime(value):
    """Outlook times come back as naive local datetimes; tolerate strings."""
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)
    return datetime.datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")


class OutlookSource:
    """Reads the default calendar out of a locally installed Outlook.

    Local COM rather than Microsoft Graph on purpose: no app registration, no
    OAuth, no tenant admin, and it sees whatever the signed-in profile sees.
    The cost is that it needs classic Outlook present - the new Outlook client
    alone does not register the COM server.

    The connection is per-thread. A COM object belongs to the apartment that
    created it, and this gets used from both the main thread (the startup
    check) and the calendar polling thread, so each takes its own. Attaching
    to an already-running Outlook is cheap, so this costs nothing real.
    """

    def __init__(self):
        self.error = None
        self._tls = threading.local()

    def _app(self):
        app = getattr(self._tls, "app", None)
        if app is not None:
            return app
        try:
            import comtypes
            import comtypes.client
        except ImportError as exc:
            self.error = ("comtypes missing (%s) - pip install comtypes"
                          % exc)
            return None
        try:
            comtypes.CoInitialize()
            app = comtypes.client.CreateObject("Outlook.Application")
        except Exception as exc:
            self.error = ("cannot reach Outlook over COM (%s) - classic "
                          "Outlook has to be installed; the new Outlook app "
                          "on its own does not register the COM server" % exc)
            return None
        self._tls.app = app
        self.error = None
        return app

    def status_name(self):
        return "available" if self._app() is not None else "unavailable"

    def authorized(self):
        return self._app() is not None

    def request_access(self, wait=0.0):
        """No-op: Outlook COM needs no grant. wait matches the contract."""
        return self.authorized()

    def events(self, lead_seconds):
        """[(title, seconds_until_start, seconds_until_end)] over now..lead."""
        app = self._app()
        if app is None:
            raise RuntimeError(self.error or "Outlook unavailable")
        folder = app.Session.GetDefaultFolder(OL_CALENDAR)
        items = folder.Items
        # Order matters. IncludeRecurrences only expands a recurring series on
        # a collection that is already sorted by start, and a work calendar is
        # mostly recurring meetings - setting these the other way round
        # quietly returns almost nothing rather than failing.
        items.Sort("[Start]")
        items.IncludeRecurrences = True

        now = datetime.datetime.now()
        until = now + datetime.timedelta(seconds=float(lead_seconds))
        selection = items.Restrict(
            "[Start] <= '%s' AND [End] >= '%s'"
            % (until.strftime(_OL_TIME_FMT), now.strftime(_OL_TIME_FMT))
        )

        out = []
        item = selection.GetFirst()
        seen = 0
        while item is not None and seen < MAX_ITEMS:
            seen += 1
            try:
                if item.AllDayEvent:
                    pass
                elif int(item.MeetingStatus) in OL_CANCELLED:
                    pass
                elif int(item.BusyStatus) == OL_FREE:
                    pass
                # A meeting you declined is not a meeting you are in. Exchange
                # keeps declined invitations on the calendar marked Busy, so
                # without this they would light the LED red.
                elif int(item.ResponseStatus) == OL_DECLINED:
                    pass
                else:
                    begins = _to_datetime(item.Start)
                    ends = _to_datetime(item.End)
                    out.append((
                        str(item.Subject or "(untitled)"),
                        int((begins - now).total_seconds()),
                        int((ends - now).total_seconds()),
                    ))
            except Exception:
                pass            # one odd item must not kill the poll
            try:
                item = selection.GetNext()
            except Exception:
                break
        return out


CALENDAR = OutlookSource()


# --------------------------------------------------------------------------
# Focus Assist
# --------------------------------------------------------------------------


def dnd_active():
    """Always None: Focus Assist is deliberately not read.

    Its state lives in a binary CloudStore registry blob with no documented
    layout, which Microsoft reshuffles between releases. Reading it would mean
    parsing an undocumented structure that breaks on a Windows update, and a
    light that lies is worse than a signal that is absent - the same reason
    the macOS device-wide audio fallback was deleted rather than kept. None
    tells the caller "not readable here" and the signal is skipped.
    """
    return None
