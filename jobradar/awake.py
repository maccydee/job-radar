"""Keep the machine awake for as long as something is running.

A full scan takes the better part of an hour, and the way people actually use
this is to set it up, watch the dashboard fill, apply to something and shut
the laptop. The scan then dies at whatever percent it had reached, and the
next run starts from nothing. Asking somebody to babysit a progress bar for
fifty minutes is not a plan.

So the scan holds a power assertion while it runs and drops it the moment it
finishes. Nothing is configured and nothing is left behind: on every platform
here the assertion lives and dies with a process.

**What this cannot do, said plainly because the difference matters.** It stops
the machine falling asleep on its own while nobody touches it. It does not
stop a laptop sleeping when the lid is closed. On macOS that needs
`pmset disablesleep`, which is undocumented, system wide and needs a password,
and this tool is not going to ask for one. Closing the lid will normally end
the scan, and the message the user sees says so rather than implying
otherwise.

Normally, not always: somebody who HAS set `disablesleep` has a machine that
does not sleep on the lid either, and telling them their scan is about to die
is a false warning that costs them a scan they would otherwise have left
running. So the message reads the setting rather than asserting a default. If
it cannot read it, it keeps the warning, because a promise this tool cannot
check is worse than a caution somebody can ignore.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys


def _macos(pid: int):
    """`caffeinate -i -w PID`, which exits by itself when PID does.

    Tied to the process rather than given a duration, so a scan that finishes
    early releases immediately and a scan that overruns is still covered. If
    this process is killed with SIGKILL the child notices its target has gone
    and exits on its own, which is why `-w` is worth the extra argument over
    `-t`.
    """
    exe = shutil.which("caffeinate") or "/usr/bin/caffeinate"
    if not os.path.exists(exe):
        return None
    try:
        return subprocess.Popen([exe, "-i", "-w", str(pid)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None


# Windows: ES_CONTINUOUS keeps the state until it is cleared, ES_SYSTEM_REQUIRED
# is the "do not idle to sleep" bit. Deliberately no ES_DISPLAY_REQUIRED: the
# screen may sleep, the machine may not. Nobody wants a monitor burning for an
# hour because a job scan is running.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def _windows() -> bool:
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED))
    except Exception:
        return False


def _windows_release() -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)  # type: ignore[attr-defined]
    except Exception:
        pass


def _linux(reason: str):
    """systemd-inhibit, which every current desktop distribution has.

    `--what=idle` only: a scan is not a reason to block the user shutting the
    machine down or closing the lid, and asking for those would be rude in a
    way `idle` is not.
    """
    exe = shutil.which("systemd-inhibit")
    if not exe:
        return None
    try:
        return subprocess.Popen(
            [exe, "--what=idle", "--who=job-radar", f"--why={reason}",
             "--mode=block", "sleep", "86400"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


class keep_awake:
    """Hold a power assertion for the life of the block.

    Never raises and never blocks the work: a machine with no way to hold an
    assertion runs the scan anyway and says so once. `held` reports whether it
    actually got one, so the caller can tell the user the truth rather than a
    reassuring guess.
    """

    def __init__(self, reason: str = "job-radar is scanning", enabled: bool = True):
        self.reason = reason
        self.enabled = enabled
        self.held = False
        self._proc = None
        self._windows = False

    def __enter__(self) -> "keep_awake":
        if not self.enabled:
            return self
        if sys.platform == "darwin":
            self._proc = _macos(os.getpid())
            self.held = self._proc is not None
        elif sys.platform.startswith("win"):
            self._windows = _windows()
            self.held = self._windows
        else:
            self._proc = _linux(self.reason)
            self.held = self._proc is not None
        return self

    def __exit__(self, *exc) -> bool:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        if self._windows:
            _windows_release()
            self._windows = False
        self.held = False
        return False


def sleep_disabled() -> bool | None:
    """Whether this machine is set never to sleep at all.

    True, False, or None for "cannot tell", which is a third answer and not a
    quiet False. On macOS `pmset -g` reports `SleepDisabled 1` when somebody
    has run `sudo pmset disablesleep 1`; on that machine the lid does nothing
    and a scan survives it.

    Never raises and never blocks for long. This is called to choose the
    wording of one sentence, and a scan must not fail or hang because a
    diagnostic command did.
    """
    if sys.platform != "darwin":
        return None
    exe = shutil.which("pmset") or "/usr/bin/pmset"
    if not os.path.exists(exe):
        return None
    try:
        out = subprocess.run([exe, "-g"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "SleepDisabled":
            return parts[1] == "1"
    # The key is only printed when it has been set, so its absence is a real
    # answer on macOS rather than an unreadable one.
    return False


def describe(held: bool) -> str:
    """One line for the user, and an honest one.

    A message saying "your machine will stay awake" would be a lie the first
    time somebody shuts the lid, so this says what is actually true, which
    means reading the setting rather than assuming the default.
    """
    if not held:
        return ("This machine has no way to stay awake on request, so a scan "
                "will stop if it sleeps.")
    if sleep_disabled() is True:
        # `disablesleep` is system wide, so the lid is covered too.
        return ("Your machine will not fall asleep on its own while this runs, "
                "and it is set never to sleep at all, so closing the lid will "
                "not stop it either.")
    # False and None both keep the caution. An unverified promise that a scan
    # survives the lid is how somebody loses an hour.
    return ("Your machine will not fall asleep on its own while this runs. "
            "Closing the lid will still stop it.")
