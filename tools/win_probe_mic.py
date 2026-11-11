#!/usr/bin/env python3
"""Dump the Windows microphone privacy registry, so the mic signal can be
checked against reality before anything is built on it.

The Windows mic signal in signals_win32.py rests on one convention: an app
that is capturing right now has a LastUsedTimeStart and no LastUsedTimeStop.
This script shows the raw values so that claim can be confirmed rather than
assumed.

    python tools\\win_probe_mic.py                  (nothing using the mic)
    python tools\\win_probe_mic.py > during.txt     (while in a Teams call)

Then compare the two. The app on the call should move into "IN USE NOW".
Stdlib only; no admin rights needed.
"""

import datetime
import sys

try:
    import winreg
except ImportError:
    sys.exit("This only means anything on Windows (no winreg here).")

CONSENT = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
           r"\CapabilityAccessManager\ConsentStore")
HIVES = [("HKCU", winreg.HKEY_CURRENT_USER),
         ("HKLM", winreg.HKEY_LOCAL_MACHINE)]
# webcam is listed too: it is the same convention, and a second example makes
# it obvious whether the pattern is real or a coincidence.
CAPABILITIES = ["microphone", "webcam"]

EPOCH = datetime.datetime(1601, 1, 1)


def when(value):
    """Render a FILETIME (100ns ticks since 1601) readably."""
    if not value:
        return "0 (unset)"
    try:
        stamp = EPOCH + datetime.timedelta(microseconds=int(value) // 10)
    except (ValueError, OverflowError, OSError):
        return repr(value)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def subkeys(key):
    names = []
    i = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, i))
        except OSError:
            return names
        i += 1


def values(key):
    out = {}
    i = 0
    while True:
        try:
            name, value, _ = winreg.EnumValue(key, i)
        except OSError:
            return out
        out[name] = value
        i += 1


def walk(hive_name, hive, capability):
    """Yield (where, keyname, values) for every app under one capability."""
    path = "%s\\%s" % (CONSENT, capability)
    try:
        base = winreg.OpenKey(hive, path)
    except OSError as exc:
        print("  %s\\%s: not readable (%s)" % (hive_name, path, exc))
        return
    with base:
        for name in subkeys(base):
            if name == "NonPackaged":
                try:
                    with winreg.OpenKey(base, name) as np:
                        for leaf in subkeys(np):
                            try:
                                with winreg.OpenKey(np, leaf) as k:
                                    yield "NonPackaged", leaf, values(k)
                            except OSError:
                                continue
                except OSError:
                    continue
                continue
            try:
                with winreg.OpenKey(base, name) as k:
                    yield "Packaged", name, values(k)
            except OSError:
                continue


def pretty(keyname):
    if "#" in keyname:
        return keyname.replace("#", "\\").rsplit("\\", 1)[-1]
    return keyname


def main():
    print("Windows microphone/webcam consent store")
    print("now: %s\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    in_use = []
    for capability in CAPABILITIES:
        for hive_name, hive in HIVES:
            print("== %s %s ==" % (hive_name, capability))
            found = False
            for where, keyname, vals in walk(hive_name, hive, capability):
                found = True
                start = vals.get("LastUsedTimeStart")
                stop = vals.get("LastUsedTimeStop")
                live = bool(start) and not stop
                if live:
                    in_use.append("%s %s: %s"
                                  % (hive_name, capability, pretty(keyname)))
                print("  %-9s %-52s %s" % (where, pretty(keyname),
                                           "<-- IN USE NOW" if live else ""))
                print("      key   : %s" % keyname)
                print("      start : %s" % when(start))
                print("      stop  : %s" % when(stop))
                extra = {k: v for k, v in vals.items()
                         if k not in ("LastUsedTimeStart", "LastUsedTimeStop")}
                if extra:
                    print("      other : %s" % extra)
            if not found:
                print("  (no entries)")
            print("")

    print("== in use right now ==")
    for line in in_use:
        print("  %s" % line)
    if not in_use:
        print("  (nothing) - if you are on a call while running this, the "
              "convention does not hold and signals_win32.MicWatcher needs "
              "the pycaw capture fallback instead.")


if __name__ == "__main__":
    main()
