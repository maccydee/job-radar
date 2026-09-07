"""The line about the lid has to be true on the machine reading it.

`describe` asserted "Closing the lid will still stop it" unconditionally. On a
Mac where somebody has run `sudo pmset disablesleep 1` that is false: sleep is
disabled system wide, the lid does nothing, and the scan runs on. Telling that
person their scan is about to die is a false warning, and the cost of it is a
scan they close the laptop on rather than leave running.

Callum, 7 September 2026: "Pretty sure I've set the laptop to do nothing when
I shut the lid." He had. `pmset -g` reported `SleepDisabled 1` and `sleep 0`
on both power sources, and this tool had been contradicting him.

The other half matters more. "Cannot tell" keeps the warning. A caution
somebody can ignore is cheap; a promise this tool cannot check is how an hour
gets lost.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar import awake


def _pmset(stdout):
    return mock.patch.object(
        awake.subprocess, "run",
        return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""))


class ReadingTheSetting(unittest.TestCase):
    def setUp(self):
        self.darwin = mock.patch.object(awake.sys, "platform", "darwin")
        self.exists = mock.patch.object(awake.os.path, "exists",
                                        return_value=True)
        self.darwin.start()
        self.exists.start()

    def tearDown(self):
        self.darwin.stop()
        self.exists.stop()

    def test_disabled_is_read(self):
        with _pmset("System-wide power settings:\n SleepDisabled\t\t1\n"):
            self.assertIs(awake.sleep_disabled(), True)

    def test_explicitly_zero_is_read(self):
        with _pmset("System-wide power settings:\n SleepDisabled\t\t0\n"):
            self.assertIs(awake.sleep_disabled(), False)

    def test_absent_means_not_disabled(self):
        # macOS only prints the key once it has been set, so its absence is a
        # real answer here rather than an unreadable one.
        with _pmset("Currently in use:\n sleep\t\t1\n displaysleep\t10\n"):
            self.assertIs(awake.sleep_disabled(), False)

    def test_a_command_that_fails_is_cannot_tell_not_false(self):
        with mock.patch.object(awake.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertIsNone(awake.sleep_disabled())

    def test_a_command_that_hangs_is_cannot_tell(self):
        with mock.patch.object(
                awake.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("pmset", 5)):
            self.assertIsNone(awake.sleep_disabled())


class NotOnMacOS(unittest.TestCase):
    def test_other_platforms_cannot_tell(self):
        with mock.patch.object(awake.sys, "platform", "linux"):
            self.assertIsNone(awake.sleep_disabled())

    def test_a_missing_pmset_cannot_tell(self):
        with mock.patch.object(awake.sys, "platform", "darwin"), \
                mock.patch.object(awake.shutil, "which", return_value=None), \
                mock.patch.object(awake.os.path, "exists", return_value=False):
            self.assertIsNone(awake.sleep_disabled())


class TheSentence(unittest.TestCase):
    def test_a_machine_that_never_sleeps_is_not_warned_about_the_lid(self):
        with mock.patch.object(awake, "sleep_disabled", return_value=True):
            said = awake.describe(True)
        self.assertIn("will not stop it", said)

    def test_an_ordinary_machine_still_is(self):
        with mock.patch.object(awake, "sleep_disabled", return_value=False):
            said = awake.describe(True)
        self.assertIn("Closing the lid will still stop it", said)

    def test_cannot_tell_keeps_the_warning(self):
        # An unverified promise that a scan survives the lid is how somebody
        # loses an hour. A caution they can ignore costs nothing.
        with mock.patch.object(awake, "sleep_disabled", return_value=None):
            said = awake.describe(True)
        self.assertIn("Closing the lid will still stop it", said)

    def test_no_assertion_is_made_when_nothing_is_held(self):
        with mock.patch.object(awake, "sleep_disabled", return_value=True):
            said = awake.describe(False)
        self.assertNotIn("lid", said)
        self.assertIn("no way to stay awake", said)

    def test_the_setting_is_not_read_when_nothing_is_held(self):
        # There is nothing to say about the lid when no assertion was taken,
        # so there is no reason to shell out.
        called = []
        with mock.patch.object(awake, "sleep_disabled",
                               side_effect=lambda: called.append(1)):
            awake.describe(False)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
