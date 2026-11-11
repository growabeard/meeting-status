"""Pieces shared by meeting_status.py and the per-platform signal modules.

This module exists to break an import cycle: the platform modules need log()
and AudioState, and meeting_status.py needs the platform modules. It therefore
imports no siblings and never will.
"""

import collections
import re
import sys
import time

# Every state the Arduino understands. OFF is sent on shutdown and is also
# settable by hand through the override file.
STATES = ("FREE", "UPCOMING", "BUSY", "MEETING", "OFF")


# What one pass over the audio system found.
#
#   capturing    name of a process holding the mic, or None
#   meeting_app  name of a meeting app that has been playing long enough to
#                count as a meeting, or None
#   candidates   meeting apps playing right now, before the hold applies
#   rows         [(name, capturing, playing, ignored)] for --probe
AudioState = collections.namedtuple(
    "AudioState", "capturing meeting_app candidates rows")


def log(msg):
    """Timestamped line to stderr, which the caller may have redirected.

    Guarded because pythonw.exe - which is how the Windows scheduled task runs
    this, to avoid a console window - gives the process no stderr at all, so
    an unguarded write would crash the daemon rather than merely lose a line.
    meeting_status.py --log points stderr at a file before anything else, so
    in practice only failures earlier than that go unrecorded.
    """
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        stream.flush()
    except (OSError, ValueError):
        pass


def compile_patterns(patterns, label):
    """Compile regexes, skipping (and naming) any that are malformed.

    A bad pattern in config.json must not take the daemon down, and must not
    fail silently either - a meeting_apps typo would otherwise look exactly
    like an app that never plays audio.
    """
    out = []
    for pat in patterns:
        try:
            out.append(re.compile(pat, re.I))
        except re.error as exc:
            log("bad %s pattern, ignoring: %r (%s)" % (label, pat, exc))
    return out


def collapse_audio(rows, meeting_apps, app_hold, playing_since):
    """Turn per-process audio rows into an AudioState.

    rows is [(name, capturing, playing, ignored)] from whichever OS API the
    platform module uses; playing_since is a dict the caller owns and this
    function updates, holding the time each meeting app started playing.

    Playback has to persist for app_hold seconds before it counts, or every
    Slack notification chime would read as a meeting. Capture gets no such
    grace period: an open mic is a meeting immediately.

    Shared because this debounce is the easy thing to get subtly wrong, and
    two copies drifting apart would make the light behave differently on the
    two platforms for no visible reason.
    """
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
        if not any(rx.search(name) for rx in meeting_apps):
            continue
        playing_now.add(name)
        since = playing_since.setdefault(name, now)
        if now - since >= app_hold and meeting_app is None:
            meeting_app = name
    # Forget apps that have stopped, so the next burst starts its own clock.
    for name in [n for n in playing_since if n not in playing_now]:
        del playing_since[name]
    return AudioState(capturing, meeting_app, sorted(playing_now), rows)
