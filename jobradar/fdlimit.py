"""Raise the open-file limit before a command that reads thousands of sources.

Every connection holds a file descriptor. launchd starts jobs with a soft
limit of 256, and a scan holds about 1,040 (16 concurrent reads plus idle
keep-alive connections, the database and the logs). Past the limit a new
connection cannot open a socket, and the failure surfaces as a DNS error,
"nodename nor servname provided", which looks like the network is down.

Measured 8 Oct 2026: the 04:00 scan lost 574 of 1,151 source reads that way
while 445 separate DNS probes in the same window all succeeded. Failed reads
are recorded as unknown rather than empty, so nothing was wrongly closed, but
roughly half the market went unread each day for at least a week.

The scheduled scripts also set `ulimit -n`, which is belt and braces. This is
the one that travels with the code to anyone else's machine and scheduler.
"""
from __future__ import annotations

try:
    import resource
except ImportError:          # Windows has no setrlimit and no such limit
    resource = None

# About eight times the measured need, so a slow leak shows up in the fd log
# long before it reaches the ceiling.
WANTED = 8192


def raise_file_limit(wanted: int = WANTED) -> tuple[int, int] | None:
    """Raise the soft limit towards `wanted`, never past the hard limit.

    Returns (old, new) when it changed anything, None when it did not need to
    or could not. It never raises: a limit that cannot be raised must not stop
    a scan that might still mostly work.
    """
    if resource is None:
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft >= wanted:
            return None
        ceiling = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        if ceiling <= soft:
            return None
        resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
        return soft, ceiling
    except (ValueError, OSError):
        return None
