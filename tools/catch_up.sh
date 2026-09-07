#!/bin/zsh
# Run everything that has gone stale, back to back, without waiting for a
# schedule and without three jobs reading the same servers at once.
#
# Written 7 September 2026, after `job-radar doctor` reported all three moving
# parts late on the same day: the last completed scan six days old, the source
# list thirteen, the seed shards nine. Each had its own broken scheduled job
# and each had been failing quietly for weeks.
#
# WHY THIS IS SEQUENTIAL, and it is the whole point of the file.
#
# A scan reads 17,923 sources. So does the weekly validation. The seed build
# reads the slow half of the same list. They belong to other people, and
# `fetch.PER_HOST_RPS` paces each host on its own clock precisely so this tool
# is a good guest. Two of these at once doubles the rate on every host in the
# list and defeats that pacing completely, from two directions at once when
# one of them runs on GitHub's runners. Politeness aside, it also produces a
# worse result: refusals climb, the seed build comes back short, and its
# freshness gate then correctly refuses to publish, so the hour is wasted.
#
# So each step waits for the one before it. The whole chain is roughly two
# hours and nobody has to sit with it.
set -u
cd "$(dirname "$0")/.."
PY=/Users/cal/.local/bin/python3
mkdir -p logs
LOG="logs/catch-up-$(date +%Y-%m-%d).log"

say() { print -r -- "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

say "catch-up starting"
$PY -m jobradar.cli doctor 2>&1 | tee -a "$LOG"

# 1. Wait for any scan already running. Not started here: one is usually
#    already going by the time anybody asks, and starting a second against the
#    same database is the overlap `cmd_scan` has its own long comment about.
while pgrep -f "jobradar.cli.*scan" >/dev/null 2>&1; do
  sleep 60
done
say "no scan running"

# 2. The seed. Local only, and deliberately never from GitHub Actions: those
#    are Azure addresses and these hosts refuse them far harder than a home
#    connection, so a build there publishes a seed with the roles missing
#    rather than a build that failed.
say "rebuilding the seed (about an hour)"
if $PY tools/refresh_seed.py >>"$LOG" 2>&1; then
  say "seed published"
else
  say "seed refresh FAILED, see $LOG. Nothing was published; the previous set is still live."
fi

# 3. The source list, on GitHub's runners, once the local network is quiet.
#    Dispatched rather than waited for: it opens a pull request when it wants
#    to prune, which is a thing to read rather than a thing to block on.
say "dispatching the weekly source validation"
if command -v gh >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/gh" ]]; then
  GH=$(command -v gh || echo "$HOME/.local/bin/gh")
  "$GH" workflow run "validate sources" >>"$LOG" 2>&1 \
    && say "validation dispatched" \
    || say "could not dispatch validation, run it from the Actions tab"
else
  say "gh not found, so the validation was not dispatched"
fi

say "catch-up finished"
$PY -m jobradar.cli doctor 2>&1 | tee -a "$LOG"
