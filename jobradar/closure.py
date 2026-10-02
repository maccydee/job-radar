"""Marking a role closed when the board stops listing it, safely.

Nothing in this tool ever did. `roles.last_seen` was written on every scan and
read by nothing that decided anything, so a vacancy filled in July sat on the
dashboard in October looking exactly like one posted that morning. The only
`closed` rows on the maintainer's database were set by hand.

The obvious implementation is the bug this repository keeps producing. "Any
role missing from the latest scan is closed" fabricates closures wholesale,
because a role is missing from a scan for a dozen reasons that have nothing to
do with the employer:

- the board answered 403, or a TLS handshake never completed;
- the host was still inside a 23 hour block it set during the last run;
- `--limit` read a stride of the list and never asked;
- the pager stopped at its own page cap, so the role is on page four;
- the API changed shape and the adapter now raises on every payload;
- the run was killed half way through.

Every one of those produces a clean "47 roles closed" that is entirely
invented, which is precisely the failure-renders-as-success shape CLAUDE.md
is about. `deadwood.py` makes the same distinction one layer out, for deleting
sources, and found that around 8% of boards it had called dead were serving
jobs again five days later.

So the invariant, and everything in this module exists to hold it:

    A role may only be closed on the evidence of a SUCCESSFUL READ of the
    specific source it came from. Absence from a run where that source
    failed, was skipped, was throttled, was cut off, or was never attempted
    is not evidence of anything.

Which needs three things that did not exist:

1. `roles.source_key` -- which source a role came from. Empty for anything
   `migrate` or `seed load` wrote, and empty can never be closed.
2. `source_reads` -- per run, which sources were read and whether the payload
   actually parsed. A 200 with an unparseable body is a FAILURE here, the
   same way `validate` and `adapters.parse_or_why` treat it.
3. `store.touch_seen` -- `last_seen` meaning "a source listed this role",
   not "a role got through screening". Without that a role whose title stops
   matching an edited config looks identical to one taken down.

Three states, not two. A stale role whose source has not been read
successfully since is UNKNOWN, which is neither "still listed" nor "gone",
and the dashboard says so rather than rendering it as an open vacancy.
"""

from __future__ import annotations

from datetime import date

from . import store

# How many separate successful reads must have missed a role before it closes.
#
# Two, and they have to fall on different days. One reading is one reading:
# a search index mid-reindex, an ATS serving a stale cache, a pager that
# dropped a page, all return a plausible subset of a board exactly once. Two
# readings taken on different days, each of which returned postings from that
# board and neither of which contained this one, is two independent
# confirmations rather than one event seen twice.
#
# Different DAYS rather than different runs, because a run count is gameable
# by frequency -- three scans in an hour is three runs and one afternoon's
# information. `deadwood.MIN_RUNS` carries the same guard for the same reason.
#
# Not higher, because the scan runs daily and the live window is a fortnight:
# at two days a role spends the rest of that fortnight visible and labelled
# before anything acts, and at five or ten the feature would effectively never
# fire on a board people actually read.
MIN_CONFIRMATIONS = 2

# Statuses a sweep may move to `closed`.
#
# `new` is the untouched majority. `interested` is in here deliberately: it is
# the reader saying they want the role, not a record of anything that passed
# between them and the employer, and a vacancy they were interested in having
# been taken down is exactly the thing they need told. It is reported
# separately from `new` for the same reason, because it is the one auto-closed
# status somebody chose by hand.
#
# Everything else is left alone. `applied`, `submitted`, `interviewing` and
# `offer` are live applications and the posting coming down says nothing about
# them; `rejected`, `withdrawn` and `skipped` are endings the reader already
# recorded, and overwriting one with a guess loses the only copy.
AUTO_CLOSABLE = ("new", "interested")

# A read that counts as evidence about a role's absence.
#
# `ok`: the payload parsed into the shape the adapter expects. Not "the
#   request returned": see `store.SCHEMA`.
# `complete`: the fetcher did not stop at its own page cap. The first 60 of
#   200 postings cannot say a posting is not on a board.
# `keyword`: 0. Reed, LinkedIn and the Workable search answer for the terms
#   they were given, and `MAX_KEYWORD_TITLES` caps those terms at twelve, so
#   reordering titles in a config changes which postings are reachable at all.
#   A role missing from a keyword search may simply not match today's config.
# `roles > 0`: the read returned postings. This is the `deadwood.py` lesson
#   brought inside the role table. A thirty-person employer between hires and
#   a board whose API quietly changed shape return byte-identical answers --
#   HTTP 200, parses, no rows -- and no single reading can tell them apart. A
#   read that returned other postings and not this one is positive evidence;
#   a read that returned nothing at all is the ambiguous case, and it is
#   withheld and counted rather than acted on.
_EVIDENCE = ("sr.ok=1 AND sr.complete=1 AND sr.keyword=0 AND sr.roles>0")

# Strictly after `last_seen`. A read on the day a role was last seen is, or
# may be, the read that saw it.
_SINCE = "sr.read_on > r.last_seen"

_CONFIRMATIONS = f"""
    (SELECT COUNT(DISTINCT sr.read_on) FROM source_reads sr
      WHERE sr.source_key = r.source_key AND {_EVIDENCE} AND {_SINCE})"""

_LAST_READ = f"""
    (SELECT MAX(sr.read_on) FROM source_reads sr
      WHERE sr.source_key = r.source_key AND {_EVIDENCE} AND {_SINCE})"""

# Reads of the source since the role was last seen that answered with an empty
# board. Not evidence, but the reason a role the reader expects to see here
# is not here, so it is counted and reported rather than silently dropped.
_EMPTY_READS = f"""
    (SELECT COUNT(DISTINCT sr.read_on) FROM source_reads sr
      WHERE sr.source_key = r.source_key AND sr.ok=1 AND sr.complete=1
        AND sr.keyword=0 AND sr.roles=0 AND {_SINCE})"""

# A role with no URL cannot be closed by this, and not only because
# `ACTIONABLE_SQL` says it is not a role anybody can act on.
#
# `migrate` imported the old `state/seen.json`, whose rows hold a uid, a
# company and a title and no link. Their uid is hashed from company|title|
# location rather than from a URL, so no posting parsed off a board will ever
# produce that id, so `touch_seen` can never bump their `last_seen` however
# alive they are. They would be closed by this sweep on the first two reads of
# whatever source they were attributed to, every one of them, confidently.
_HAS_URL = "COALESCE(r.url,'') LIKE 'http%'"


# The three states as one SQL expression, over a query aliasing `roles` as r.
#
# One definition, used by `source_states` and by the dashboard's own row
# query. Two copies of this would be two answers to "is this role still
# listed", and the dashboard would be the one people believed.
STATE_SQL = f"""
    CASE
      WHEN r.last_seen >= (SELECT MAX(last_seen) FROM roles) THEN 'listed'
      WHEN {_CONFIRMATIONS} > 0 THEN 'absent'
      WHEN (SELECT COUNT(*) FROM source_reads sr
             WHERE sr.source_key = r.source_key AND {_EVIDENCE}
               AND sr.read_on >= r.last_seen) > 0 THEN 'listed'
      ELSE 'unknown'
    END"""


def _rows(con):
    q = ",".join("?" * len(AUTO_CLOSABLE))
    return con.execute(f"""
        SELECT r.uid, r.company, r.title, r.source_key, r.last_seen,
               r.platform, COALESCE(st.status,'new') AS status,
               {_CONFIRMATIONS} AS confirmations,
               {_LAST_READ} AS last_read,
               {_EMPTY_READS} AS empty_reads
        FROM roles r LEFT JOIN role_state st ON st.uid = r.uid
        WHERE COALESCE(r.source_key,'') <> ''
          AND {_HAS_URL}
          AND COALESCE(st.status,'new') IN ({q})
        ORDER BY r.company COLLATE NOCASE, r.title COLLATE NOCASE
    """, tuple(AUTO_CLOSABLE)).fetchall()


def candidates(con) -> list[dict]:
    """Every role with enough evidence to close, and the evidence itself.

    A report, not an action: a sweep that can only be inspected by running it
    is a sweep nobody can review, which is how a pull request titled "Prune 2
    dead source(s)" came to delete 17,171 rows.
    """
    store._ensure_columns(con)
    out = []
    for r in _rows(con):
        if (r["confirmations"] or 0) < MIN_CONFIRMATIONS:
            continue
        out.append({
            "uid": r["uid"], "company": r["company"], "title": r["title"],
            "source_key": r["source_key"], "platform": r["platform"],
            "last_seen": r["last_seen"], "status": r["status"],
            "confirmations": r["confirmations"], "last_read": r["last_read"],
        })
    return out


def withheld(con) -> list[dict]:
    """Stale roles this will not close, and the reason in each case.

    The honest other half of `candidates`. A sweep that reports only what it
    did hides the far larger set it could not decide about, and "0 closed"
    with nothing beside it reads as "nothing has gone", which on this database
    is untrue by over a thousand roles.
    """
    store._ensure_columns(con)
    newest = _newest_scan(con)
    out = []
    for r in _rows(con):
        if (r["confirmations"] or 0) >= MIN_CONFIRMATIONS:
            continue
        if newest and r["last_seen"] >= newest:
            continue            # seen on the most recent scan; not stale
        if r["empty_reads"]:
            why = (f"its source has been read {r['empty_reads']} time(s) "
                   f"since and answered with no postings at all, which is "
                   f"not evidence about one posting")
        elif r["confirmations"]:
            why = (f"read successfully {r['confirmations']} time(s) since, "
                   f"{MIN_CONFIRMATIONS} needed")
        else:
            why = "its source has not been read successfully since"
        out.append({"uid": r["uid"], "company": r["company"],
                    "title": r["title"], "last_seen": r["last_seen"],
                    "source_key": r["source_key"], "why": why})
    return out


def close_absent(con, apply: bool = True) -> dict:
    """Close every role with enough evidence. Returns what it did and why.

    `apply=False` makes it a dry run that still returns the full answer, so
    `job-radar closures` can show the exact set before anybody writes it.
    """
    store._ensure_columns(con)
    cands = candidates(con)
    closed, interested = [], []
    for c in cands:
        if c["status"] == "interested":
            interested.append(c["uid"])
        closed.append(c["uid"])
        if apply:
            # Through `set_status`, not a bare UPDATE: it is the one place
            # that validates the status and keeps `updated_at` honest.
            store.set_status(con, c["uid"], "closed", note=_note(c))
    kept = withheld(con)
    return {
        "closed": closed,
        "was_interested": interested,
        "withheld": kept,
        "withheld_empty": sum(1 for k in kept if "no postings at all" in k["why"]),
        "withheld_unread": sum(1 for k in kept
                               if "not been read successfully" in k["why"]),
        "applied": bool(apply),
    }


def _note(c: dict) -> str:
    """What closed this role, and on what evidence.

    Written into the note so the reader can disagree with it. A status change
    with no reason attached is a thing that happened TO their board; this one
    names the source, the number of readings and the date of the last, which
    is enough to go and look.
    """
    return (f"closed automatically on {date.today().isoformat()}: absent from "
            f"{c['confirmations']} successful reads of {c['source_key']}, the "
            f"last on {c['last_read']}; last listed {c['last_seen']}")


def _newest_scan(con) -> str:
    r = con.execute("SELECT MAX(last_seen) m FROM roles").fetchone()
    return (r["m"] if r else "") or ""


def source_states(con) -> dict[str, str]:
    """Per role: 'listed', 'absent' or 'unknown'.

    The third state is the whole point. A role last seen three weeks ago whose
    board has not been successfully read since is not a live vacancy and is
    not a closed one: nobody knows, and rendering it identically to a role
    confirmed on this morning's scan is the same mistake as reading "we cannot
    say" as "no", which CLAUDE.md records three separate instances of.

    - 'listed'  seen on the most recent scan, or a successful read of its
                source on or after the day it was last seen.
    - 'absent'  stale, and its source HAS been read successfully since
                without it. On its way to closed; `MIN_CONFIRMATIONS`
                readings is what finishes it.
    - 'unknown' stale, and no successful read of its source since. Nothing
                here knows anything about this role's current state.
    """
    store._ensure_columns(con)
    rows = con.execute(
        f"SELECT r.uid, {STATE_SQL} AS state FROM roles r").fetchall()
    return {r["uid"]: r["state"] for r in rows}


# ------------------------------------------------------------- backfill

def _fold(name: str) -> str:
    return " ".join((name or "").lower().split())


def backfill_source_keys(con, sources) -> dict:
    """Attribute existing roles to a configured source, where it is certain.

    Deliberately conservative, and deliberately leaves rows empty. A wrong
    attribution here is not a cosmetic error: it closes a live role on another
    board's evidence, and it does it silently. So a role is only attributed
    when the answer is unambiguous, and ambiguity is counted rather than
    resolved by a guess.

    Two passes, both exact:

    1. (platform, company) maps to exactly one configured source. This is the
       employer-board case, which is most of the list: every adapter sets
       `Job.company` from `Source.company` for a board belonging to one
       employer.
    2. (company) alone maps to exactly one configured source, for rows with
       no platform recorded -- `seed load` and `migrate` wrote thousands of
       those.

    Keyword templates are excluded from both. Their URLs hold a `{keyword}`
    placeholder, so the key in the file is not a key anything was ever read
    under, and the roles they return are the aggregator rows whose company is
    read off the posting rather than off the source.
    """
    store._ensure_columns(con)
    by_pair: dict[tuple, set] = {}
    by_company: dict[str, set] = {}
    for s in sources:
        if getattr(s, "keyword_template", False) or "{" in s.url:
            continue
        key = s.key
        by_pair.setdefault((s.platform or "", _fold(s.company)), set()).add(key)
        by_company.setdefault(_fold(s.company), set()).add(key)

    # Only rows that could ever be confirmed or refuted.
    #
    # The first run of this on the maintainer's database attributed 3,818 of
    # the 4,260 rows `migrate` imported from the old `state/seen.json` to a
    # board, on a company-name match. Every one of those rows holds a uid, a
    # company and a title and no link, so no posting read off that board can
    # ever produce its id: the attribution can never be tested, by this or by
    # anything else, and writing it down says we know where a role came from
    # when we do not. `_HAS_URL` already refuses to close them, so the only
    # thing those 3,818 rows could do is mislead somebody reading the column.
    rows = con.execute(
        "SELECT uid, company, platform FROM roles "
        "WHERE COALESCE(source_key,'')='' AND COALESCE(url,'') LIKE 'http%'"
    ).fetchall()
    filled = ambiguous = unmatched = 0
    for r in rows:
        company = _fold(r["company"])
        plat = r["platform"] or ""
        keys = by_pair.get((plat, company)) if plat else None
        if keys is None:
            keys = by_company.get(company)
        if not keys:
            unmatched += 1
            continue
        if len(keys) > 1:
            ambiguous += 1
            continue
        con.execute("UPDATE roles SET source_key=? WHERE uid=?",
                    (next(iter(keys)), r["uid"]))
        filled += 1
    return {"looked_at": len(rows), "filled": filled,
            "ambiguous": ambiguous, "unmatched": unmatched}
