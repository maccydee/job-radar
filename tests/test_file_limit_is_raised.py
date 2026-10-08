"""A scan must not start with the 256-file limit launchd gives scheduled jobs.

A scan holds about 1,040 descriptors. At 256 every connection past the limit
failed its DNS lookup, so half of each day's source reads were lost with an
error that read as a network outage. See jobradar/fdlimit.py.
"""
import resource
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.fdlimit import raise_file_limit  # noqa: E402


def _restore(soft, hard):
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_a_low_soft_limit_is_raised_to_the_wanted_figure():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        got = raise_file_limit(1024)
        now = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        assert got == (256, 1024), got
        assert now == 1024, now
    finally:
        _restore(soft, hard)


def test_it_never_goes_past_the_hard_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 512))
        got = raise_file_limit(8192)
        assert got == (128, 512), got
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 512
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        except (ValueError, OSError):
            pass  # a lowered hard limit cannot be raised back by this process


def test_a_limit_already_high_enough_is_left_alone():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (2048, hard))
        assert raise_file_limit(1024) is None
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 2048
    finally:
        _restore(soft, hard)
