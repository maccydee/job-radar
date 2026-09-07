"""Holding the machine awake while a scan runs, and being honest about it.

A full scan is the better part of an hour, and the way people use this is to
set it up, watch the dashboard fill, apply to something and shut the laptop.
The scan then dies at whatever percent it reached and the next one starts from
nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import awake


def test_it_never_stops_the_work_when_it_cannot_hold_anything():
    """A machine with no way to hold an assertion has to run the scan anyway.
    Refusing to scan because the laptop might sleep would be absurd."""
    with mock.patch.object(awake, "_macos", lambda pid: None), \
            mock.patch.object(awake, "_linux", lambda reason: None), \
            mock.patch.object(awake, "_windows", lambda: False):
        with awake.keep_awake("x") as k:
            assert k.held is False
    # And it says so rather than claiming otherwise.
    assert "no way to stay awake" in awake.describe(False)


def test_the_message_does_not_promise_more_than_it_delivers():
    """It stops an idle machine napping. It does not survive the lid closing,
    which on macOS needs `pmset disablesleep`: undocumented, system wide, and
    needing a password this tool will not ask for. A message saying "your
    machine will stay awake" is a lie the first time somebody shuts the lid.

    Both halves are pinned, and the environment with them. This asserted the
    warning unconditionally and so depended on the machine it ran on: it
    passed on CI and failed on a Mac where `pmset disablesleep 1` had been
    set, because there the warning is the false statement and the message
    correctly stops making it. A test whose verdict turns on the developer's
    power settings is not testing the code.
    """
    from unittest import mock

    with mock.patch.object(awake, "sleep_disabled", return_value=False):
        held = awake.describe(True)
    assert "lid" in held.lower(), held
    assert "lid will still stop it" in held, held

    # And the opposite machine, where that warning would be the lie.
    with mock.patch.object(awake, "sleep_disabled", return_value=True):
        never = awake.describe(True)
    assert "will not stop it" in never, never

    # "Cannot tell" keeps the caution: an unverified promise that a scan
    # survives the lid is how an hour gets lost.
    with mock.patch.object(awake, "sleep_disabled", return_value=None):
        unsure = awake.describe(True)
    assert "lid will still stop it" in unsure, unsure


def test_the_assertion_is_dropped_on_the_way_out():
    calls = {"terminated": 0}

    class FakeProc:
        def terminate(self):
            calls["terminated"] += 1

        def wait(self, timeout=None):
            return 0

    with mock.patch.object(awake, "_macos", lambda pid: FakeProc()), \
            mock.patch.object(sys, "platform", "darwin"):
        with awake.keep_awake("x") as k:
            assert k.held is True
        assert k.held is False
    assert calls["terminated"] == 1, "the assertion outlived the scan"


def test_an_exception_still_releases_it():
    """A scan that dies must not leave the machine pinned awake for ever."""
    class FakeProc:
        def __init__(self):
            self.gone = False

        def terminate(self):
            self.gone = True

        def wait(self, timeout=None):
            return 0

    proc = FakeProc()
    with mock.patch.object(awake, "_macos", lambda pid: proc), \
            mock.patch.object(sys, "platform", "darwin"):
        try:
            with awake.keep_awake("x"):
                raise RuntimeError("scan blew up")
        except RuntimeError:
            pass
    assert proc.gone, "an assertion survived a failed scan"


def test_windows_asks_for_the_system_and_not_the_screen():
    """ES_SYSTEM_REQUIRED without ES_DISPLAY_REQUIRED. Nobody wants a monitor
    burning for an hour because a job scan is running."""
    # Read the code, not the comments. The first version of this grepped the
    # whole module and failed on the comment explaining why the display bit is
    # not used, which is the third time today a test has caught its own
    # explanation.
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(awake._windows)))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "_ES_SYSTEM_REQUIRED" in names
    assert not any("DISPLAY" in n for n in names), names
    assert hasattr(awake, "_windows_release"), "the state is never cleared"


def test_macos_ties_the_assertion_to_the_process_not_a_timer():
    """`-w PID` exits by itself when the scan does, so a run that finishes
    early releases immediately and one that overruns is still covered. A
    timer would have to guess, and the guess is the estimate that has been
    wrong all week."""
    import inspect
    src = inspect.getsource(awake._macos)
    assert '"-w"' in src
    assert '"-t"' not in src


def test_it_really_holds_one_here():
    """The unit tests above are all stubs, so one of them has to be real or
    they prove only that the stubs work."""
    if sys.platform != "darwin":
        return
    with awake.keep_awake("job-radar test") as k:
        if not k.held:
            return                      # no caffeinate on this machine
        out = subprocess.run(["pmset", "-g", "assertions"],
                             capture_output=True, text=True).stdout
        assert "PreventUserIdleSystemSleep" in out
