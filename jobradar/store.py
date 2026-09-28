"""The database. One place that knows about a role.

Replaces three stores that used to hold overlapping truths: the seen-set in
`state/seen.json`, the application history in `applications.local.yaml`, and
the last scan in `out/roles.json`. Two defects this week came from the same
fact living in two places, which is the whole argument for this file.

Stays local and gitignored: it holds application history, private notes and
paths to documents, and the repository is public.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PATH = Path("data/job-radar.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS roles (
  uid TEXT PRIMARY KEY,
  company TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  location TEXT DEFAULT '',
  city TEXT DEFAULT '',
  country TEXT DEFAULT '',
  work_mode TEXT DEFAULT 'unstated',
  employment TEXT DEFAULT 'unstated',
  sector TEXT DEFAULT '',
  platform TEXT DEFAULT '',
  department TEXT DEFAULT '',
  salary_min REAL, salary_max REAL, salary_currency TEXT,
  salary_period TEXT DEFAULT 'year',
  salary_confirmed INTEGER DEFAULT 0,
  salary_label TEXT DEFAULT '',
  posted_at TEXT,
  description TEXT DEFAULT '',
  score REAL DEFAULT 0,
  reasons TEXT DEFAULT '[]',
  flags TEXT DEFAULT '[]',
  first_seen TEXT NOT NULL,
  first_run INTEGER DEFAULT 0,
  last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roles_last_seen ON roles(last_seen);
CREATE INDEX IF NOT EXISTS idx_roles_company ON roles(company);

CREATE TABLE IF NOT EXISTS role_state (
  uid TEXT PRIMARY KEY REFERENCES roles(uid) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'new',
  note TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL REFERENCES roles(uid) ON DELETE CASCADE,
  kind TEXT NOT NULL,              -- cv | cover_letter | screen | jd_snapshot
  path TEXT DEFAULT '',
  rating REAL,
  summary TEXT DEFAULT '',
  gates TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_uid ON artifacts(uid, kind);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL REFERENCES roles(uid) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done|failed
  requested_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT,
  attempts INTEGER DEFAULT 0,
  error TEXT DEFAULT '',
  log TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

# Terminal states: these roles stop appearing in results.
SETTLED = {"rejected", "withdrawn", "skipped", "closed"}
STATUSES = ["new", "interested", "applied", "submitted", "interviewing",
            "offer", "rejected", "withdrawn", "skipped", "closed"]

# Applications with something still owed on them, by you or by them. These are
# the rows with a deadline attached, and they were scattered among hundreds of
# roles nobody has touched.
IN_FLIGHT = {"applied", "submitted", "interviewing", "offer"}

# Applications that ended. Deliberately excludes "skipped": you never applied
# to those, so they are noise in a record of what you actually went for.
CLOSED_OUT = {"rejected", "withdrawn", "closed"}

# How far along an application is, for deciding which of two records of the
# same job to keep. A merge must never trade an interview for an "interested".
PROGRESS = {"new": 0, "skipped": 1, "interested": 2, "applied": 3,
            "submitted": 4, "closed": 5, "withdrawn": 6, "rejected": 7,
            "interviewing": 8, "offer": 9}


class StoreError(Exception):
    """A `--db` path this tool cannot use, phrased for whoever typed it.

    Everything here used to come out as a raw sqlite exception with a nine
    frame traceback above it. Three separate mistakes -- pointing `--db` at a
    directory, at a file that is not a database, and into a directory this
    user cannot write -- all surfaced as `unable to open database file` or
    `file is not a database`, which name neither the path nor the fix. The
    message is the whole of the useful part, so it is written here and the
    traceback is dropped.
    """


def _path_problem(p: Path, must_exist: bool) -> str:
    """Why `p` cannot be used, or "" if there is nothing wrong with the path.

    Checked before sqlite is asked, because sqlite collapses every one of
    these into the same sentence and this is the layer that still knows which
    mistake was made.
    """
    if p.is_dir():
        return (f"{p} is a directory, not a database file.\n"
                f"--db wants the file itself, the way "
                f"`--db {p / DEFAULT_PATH.name}` does.")
    if must_exist:
        if not p.exists():
            # The bug this exists for: `job-radar list --db typo.db` created
            # an empty database, printed "0 role(s)", exited 0, and left a
            # 64KB file behind. "0 roles" from a mistyped path is
            # indistinguishable from "0 roles" from the real one, and it is
            # the confident wrong answer this whole tool exists to not give.
            return (f"No database at {p}.\n"
                    f"A read command will not create one, because an empty "
                    f"database answers every question with nothing and that "
                    f"reads exactly like a real answer. Check the path, or "
                    f"run `job-radar scan` to build it.")
        return ""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (f"Cannot create {p.parent} to hold the database: {exc}\n"
                f"Pick a --db path somewhere you can write.")
    return ""


def _sqlite_problem(p: Path, exc: Exception) -> str:
    """One line naming which of sqlite's two sentences this is."""
    msg = str(exc).lower()
    if "not a database" in msg or "encrypted" in msg:
        return (f"{p} exists but is not a job-radar database "
                f"(sqlite says: {exc}).\n"
                f"--db wants the .db file a scan wrote, not a config, an "
                f"export or a shard set.")
    if "unable to open" in msg or "readonly" in msg or "attempt to write" in msg:
        # WAL mode needs to create `-wal` and `-shm` beside the file, so a
        # readable database in an unwritable directory fails here too, and
        # "unable to open database file" never says that is what happened.
        return (f"Cannot open {p}: this user cannot write there, and the "
                f"database needs to write beside itself.\n"
                f"sqlite says: {exc}")
    return f"Cannot use {p} as a database: {exc}"


def connect(path: str | Path | None = None, *,
            must_exist: bool = False) -> sqlite3.Connection:
    """Open the database. `must_exist` refuses to invent one.

    Creating on demand is right for `scan` and `seed load`, which are the two
    commands whose job is to fill a database, and wrong for every command that
    only reads one: those are the ones a typo turns into a confident empty
    answer plus a stray file. So the read path passes `must_exist=True` and
    the two writers do not.
    """
    p = Path(path or DEFAULT_PATH)
    # `:memory:` is a database that by definition does not exist on disk, and
    # `scan --dry-run` is built on it. None of the path rules apply.
    if str(p) != ":memory:":
        why = _path_problem(p, must_exist)
        if why:
            raise StoreError(why)
    try:
        con = sqlite3.connect(p, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
    except sqlite3.DatabaseError as exc:
        # `from None`: the traceback is nine frames of this tool's own call
        # stack above one sentence, and the sentence is the only part that
        # tells anybody what to change.
        raise StoreError(_sqlite_problem(p, exc)) from None
    # A database made by an older version is missing columns the dashboard
    # now reads. Adding them on open rather than only in the write path means
    # `serve` and `list` cannot crash on a database that has not been scanned
    # since the upgrade.
    _ensure_columns(con)
    return con


@contextmanager
def open_db(path=None):
    # Deliberately calls `connect` with the path alone. Tests stub
    # `store.connect` with a one-argument function, which is the whole of the
    # signature this has ever needed, and threading `must_exist` through here
    # would break every one of them for a keyword nothing asks this for: the
    # read commands call `connect` themselves. Two of them went red on exactly
    # that.
    con = connect(path)
    try:
        yield con
    finally:
        con.close()


# ------------------------------------------------------------------ roles

def _try_alter(con, ddl: str) -> None:
    """Run an ALTER, tolerating another thread having just run the same one."""
    try:
        con.execute(ddl)
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def claim(con, name: str) -> bool:
    """Take an exclusive lock named `name`, or return False.

    Every guard in this tool used to be check-then-act: read a count, decide,
    then write. Under a ThreadingHTTPServer that loses every race that matters
    -- one double-click spawned two `claude` subprocesses into the same folder,
    and three parallel rank requests started three full runs. An INSERT on a
    primary key is atomic, so the database decides instead of the handler.
    """
    _ensure_columns(con)
    try:
        con.execute("INSERT INTO locks (name, taken_at) VALUES (?,?)",
                    (name, _now()))
        return True
    except sqlite3.IntegrityError:
        return False


def claim_slot(con, prefix: str, key: str, limit: int) -> tuple[bool, str]:
    """Take one of `limit` slots named `prefix:key`, or say why not.

    Two different guards in one call, because they answer two different
    questions and the old single `generate` lock conflated them. `prefix:key`
    is exclusivity: two runs for the same role write the same folder, which is
    the collision the original lock existed to stop. `limit` is capacity: each
    run is a `claude` subprocess that spends money, so the number in flight is
    bounded whatever the browser asks for.

    Nothing here reads a count and then decides. The INSERT decides, and the
    count is taken after it so a loser of the race releases rather than
    proceeding on a number that was true a moment ago.
    """
    if not claim(con, f"{prefix}:{key}"):
        return False, "already running for this role"
    running = con.execute(
        "SELECT COUNT(*) c FROM locks WHERE name LIKE ?", (f"{prefix}:%",)
    ).fetchone()["c"]
    if running > limit:
        release(con, f"{prefix}:{key}")
        return False, f"{limit} generations already running"
    return True, ""


def release(con, name: str) -> None:
    con.execute("DELETE FROM locks WHERE name=?", (name,))


def clear_locks(con) -> int:
    """Drop every lock. Called on server start: a lock outlives the process
    that took it, and a stale one refuses work for ever."""
    try:
        return con.execute("DELETE FROM locks").rowcount
    except sqlite3.OperationalError:
        return 0


def _ensure_columns(con) -> None:
    """Add columns to a database made by an older version."""
    # Twelve simultaneous opens produced eleven "duplicate column name"
    # crashes: this is a check then an ALTER, with a ThreadingHTTPServer
    # opening a connection per request and the page firing three at once. The
    # loser of the race is not wrong, it is late.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(roles)")}
    if "first_run" not in cols:
        _try_alter(con, "ALTER TABLE roles ADD COLUMN first_run INTEGER DEFAULT 0")
    # Fit against the CV, from `job-radar rank`. -1 means "not yet judged",
    # which is different from 0 ("judged, and wrong for you") and has to stay
    # different or an unranked board sorts as though every role were terrible.
    if "fit" not in cols:
        _try_alter(con, "ALTER TABLE roles ADD COLUMN fit INTEGER DEFAULT -1")
    if "fit_why" not in cols:
        _try_alter(con, "ALTER TABLE roles ADD COLUMN fit_why TEXT DEFAULT ''")
    # Permanent, contract or unstated. Existing rows arrive here as 'unstated',
    # which is the truthful answer for a row nothing has classified yet: the
    # backfill is `job-radar rescreen`, and until it runs the dashboard shows
    # them under "Not stated" rather than quietly calling them permanent.
    if "employment" not in cols:
        _try_alter(con,
                   "ALTER TABLE roles ADD COLUMN employment TEXT DEFAULT 'unstated'")
    acols = {r["name"] for r in con.execute("PRAGMA table_info(artifacts)")}
    if "body" not in acols:
        _try_alter(con, "ALTER TABLE artifacts ADD COLUMN body TEXT DEFAULT ''")
    con.execute("CREATE TABLE IF NOT EXISTS locks ("
                "name TEXT PRIMARY KEY, taken_at TEXT NOT NULL)")


def upsert_roles(con, jobs: Iterable, run: int | None = None,
                 first_seen: str | None = None) -> tuple[int, int]:
    """Insert or refresh roles. Returns (new, seen_before).

    `first_seen` is never overwritten: it is what makes "new since last run"
    meaningful across months rather than across one scan.

    It can be SET on insert, though, because "when this row was written" and
    "when anybody first saw this job" are not the same date and only one of
    them is useful. `seed load` imports tens of thousands of roles the builder
    observed days ago; stamping them with today made `list --new` answer with
    the entire database. Following the sequence the README recommends, a scan
    reported 3 new roles and `list --new` reported 437, one minute apart,
    against the same database. Defaults to today, which is right for a scan.

    `run` is the run number to stamp on rows this call inserts. It defaults to
    "one past the counter, read right now", which is only correct while
    nothing else is scanning. Two scans overlapping -- a cron run and a manual
    one, which is an entirely ordinary Tuesday -- read that counter at
    different moments and stamped and queried different numbers, so the loser
    reported `250 match your config, 0 new` on 250 roles that were all new.
    A caller that will later ask "what was new on MY run" has to pin the
    number once and pass it to both halves. See `cli.cmd_scan`.
    """
    _ensure_columns(con)
    today = date.today().isoformat()
    seen_first = first_seen or today
    run = int(run) if run is not None else int(get_meta(con, "runs", "0")) + 1
    new = seen = 0
    for j in jobs:
        row = con.execute("SELECT uid FROM roles WHERE uid=?", (j.uid,)).fetchone()
        vals = (
            j.company, j.title, j.url, j.location, j.city or "", j.country or "",
            j.work_mode or "unstated", j.sector or "", j.platform, j.department or "",
            j.salary.min, j.salary.max, j.salary.currency, j.salary.period,
            1 if j.salary.confirmed else 0, j.salary.label(),
            j.posted_at, (j.description or "")[:20000], j.score,
            json.dumps(j.reasons), json.dumps(j.flags), today,
        )
        if row:
            seen += 1
            # Never overwrite a longer description, or a confirmed salary,
            # with the emptier version the list endpoint returns.
            #
            # LinkedIn, Workday and SmartRecruiters all omit the description
            # from their search results, which is why `enrich` exists to fetch
            # it per job. An unconditional UPDATE meant every scan threw that
            # work away and re-fetched it, and with --no-enrich it was
            # destroyed and never came back.
            # `sector` is the BOARD's tag, not something read off the
            # posting, so a writer that does not carry it is missing it
            # rather than contradicting it. `seed load` is exactly that
            # writer: shard rows had no sector, so importing a seed on top
            # of a scanned database blanked the column on every role it
            # touched, the dashboard's sector filter collapsed to "Other",
            # and `seed load` printed "Stored." either way. Empty never
            # overwrites a value that is there.
            # Same guard, extended to place on 2026-08-31. Loading a seed on
            # top of a scanned database blanked `country` on 53 roles the
            # scan had just resolved: shard rows carry a country only when
            # the builder could read one, and an empty one means "this
            # writer does not know", never "this role has no country". A
            # role whose country goes blank drops out of every
            # country-filtered view, which from the reader's side is
            # indistinguishable from the job being withdrawn.
            con.execute("""UPDATE roles SET company=?,title=?,url=?,
                location=COALESCE(NULLIF(?,''),location),
                city=COALESCE(NULLIF(?,''),city),
                country=COALESCE(NULLIF(?,''),country),
                work_mode=?,sector=COALESCE(NULLIF(?,''),sector),platform=?,department=?,
                salary_min=CASE WHEN ?=1 OR salary_confirmed=0 THEN ? ELSE salary_min END,
                salary_max=CASE WHEN ?=1 OR salary_confirmed=0 THEN ? ELSE salary_max END,
                salary_currency=CASE WHEN ?=1 OR salary_confirmed=0 THEN ? ELSE salary_currency END,
                salary_period=CASE WHEN ?=1 OR salary_confirmed=0 THEN ? ELSE salary_period END,
                salary_confirmed=CASE WHEN ?=1 OR salary_confirmed=0 THEN ? ELSE salary_confirmed END,
                salary_label=CASE WHEN ?=1 OR salary_confirmed=0 THEN ? ELSE salary_label END,
                posted_at=?,
                description=CASE WHEN LENGTH(?) >= LENGTH(COALESCE(description,''))
                                 THEN ? ELSE description END,
                score=?,reasons=?,flags=?,last_seen=? WHERE uid=?""",
                vals[:10]
                + (vals[14], vals[10], vals[14], vals[11], vals[14], vals[12],
                   vals[14], vals[13], vals[14], vals[14], vals[14], vals[15])
                + (vals[16], vals[17], vals[17])
                + vals[18:] + (j.uid,))
        else:
            new += 1
            con.execute("""INSERT INTO roles (company,title,url,location,city,country,
                work_mode,sector,platform,department,salary_min,salary_max,
                salary_currency,salary_period,salary_confirmed,salary_label,posted_at,
                description,score,reasons,flags,last_seen,first_seen,first_run,uid)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                # last_seen is today (we have just confirmed it), but
                # first_seen is when it was first OBSERVED, which for a
                # seeded row is the day the shard set was built.
                vals + (seen_first, run, j.uid))
            con.execute("INSERT OR IGNORE INTO role_state (uid,status,updated_at) "
                        "VALUES (?,'new',?)", (j.uid, today))
        # Employment type, written separately and on purpose.
        #
        # The UPDATE above threads twenty-two values through one positional
        # tuple that is then re-sliced by index, which is a shape this repo
        # has already been bitten by: adding a column in the middle of it
        # renumbers everything after and stores each value in its neighbour's
        # column, confidently and with no error.
        #
        # 'unstated' never overwrites a decided value, the same guard `sector`
        # and `country` carry above and for the same reason: a writer that
        # could not classify a posting is missing the answer, not contradicting
        # it. The list endpoints of LinkedIn, Workday and SmartRecruiters send
        # no description at all, so a scan pass that has not enriched a role
        # yet would otherwise wipe the classification the enriched pass made.
        emp = getattr(j, "employment", "") or ""
        if emp and emp != "unstated":
            con.execute("UPDATE roles SET employment=? WHERE uid=?", (emp, j.uid))
    return new, seen


# How long a role stays on the board after a scan last saw it.
#
# The dashboard used to show only roles whose last_seen equalled the newest
# date in the table. One `--limit 25` run then replaced a 60-role board with
# 4, because those 4 now held the newest date; `list` had no such filter and
# accumulated every role ever seen, so the two views disagreed by 71 rows on
# the same database. A window fixes both: a posting that stops appearing ages
# off in a fortnight instead of the instant a source is throttled.
LIVE_WINDOW_DAYS = 14

LIVE_SQL = (f"r.last_seen >= date((SELECT MAX(last_seen) FROM roles), "
            f"'-{LIVE_WINDOW_DAYS} days')")

# A role you cannot open is not a role you can act on.
#
# `migrate` imports the old state/seen.json, which holds a uid, a company and
# a title and no link, because its whole job was answering "have I seen this
# before". Those rows went into the same table as real listings and the
# dashboard rendered them with `href=""`: 103 of them on this database,
# indistinguishable from a live vacancy until you clicked one and the page
# reloaded on itself.
#
# They still have to exist, or every role in the old seen-set comes back as
# new. They just must not be offered as something to apply to.
ACTIONABLE_SQL = "COALESCE(r.url,'') LIKE 'http%'"

# "New" means first seen on the most recent scan DATE, not on the most recent
# run NUMBER.
#
# Keyed on the run number, a second scan the same afternoon bumped the counter
# and every role from the morning stopped being new, so the answer to "what
# arrived today" became zero while twenty-one things had genuinely arrived.
# Rescanning is normal, and a person rescanning should see the same answer,
# not lose it. The date is what they mean by "today".
NEW_SQL = "r.first_seen = (SELECT MAX(last_seen) FROM roles)"


def current_run(con) -> int:
    """The run number the last completed scan wrote."""
    return int(get_meta(con, "runs", "0"))


def new_today(con) -> set[str]:
    """Roles first seen on the latest scan date. Stable across rescans."""
    _ensure_columns(con)
    return {r["uid"] for r in con.execute(
        f"SELECT uid FROM roles r WHERE {NEW_SQL}")}


def new_since_last_run(con, uids: list[str], run: int | None = None) -> set[str]:
    """Roles first seen on THIS run, not merely today.

    Keying on the date meant a second scan the same afternoon re-reported every
    role from the morning as new, which is exactly the behaviour the seen-set
    exists to prevent.

    The very first run reports nothing as new: "here are 300 new roles" on day
    one is not an alert, it is the whole database.

    `run` is this caller's own run number, and it must be the same one it
    handed `upsert_roles`. Left to the default it is re-derived from the
    counter, which another scan may have moved in between: measured, two
    scans over the same 250 roles on a fresh database had the second one
    stamp its rows `first_run=1` and then go looking for `first_run=2`.
    """
    _ensure_columns(con)
    this_run = (int(run) if run is not None
                else int(get_meta(con, "runs", "0")) + 1)
    if this_run <= 1 or not uids:
        return set()
    q = ",".join("?" * len(uids))
    rows = con.execute(
        f"SELECT uid FROM roles WHERE first_run=? AND uid IN ({q})",
        (this_run, *uids))
    return {r["uid"] for r in rows}


def set_status(con, uid: str, status: str, note: str | None = None) -> None:
    """Move a role to `status`. `note=None` leaves any note alone.

    The note used to be kept whenever the new one was empty, which is right
    for the buttons -- Skip and Apply send a status and no note, and must not
    wipe "second round 3 Sept" on the way past. But it also made a note
    impossible to delete: clearing the box in the dashboard sent "", the API
    answered ok, the page said "Note saved" and reloaded, and the old note was
    still sitting there. Nothing anywhere could remove one.

    So the two cases are told apart at the edge instead. None means "not
    supplied, keep what is there"; "" means "the person emptied the box".
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    con.execute("""INSERT INTO role_state (uid,status,note,updated_at)
        VALUES (?,?,COALESCE(?,''),?)
        ON CONFLICT(uid) DO UPDATE SET status=excluded.status,
          note=COALESCE(?, role_state.note),
          updated_at=excluded.updated_at""",
        (uid, status, note, date.today().isoformat(), note))


def _as_list(raw) -> list:
    """A JSON list out of a column, or an empty one. Never raises."""
    try:
        out = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return out if isinstance(out, list) else []


def live_jobs(con) -> list:
    """Every role still on the board, as `Job` objects.

    The static dashboard rendered whatever THIS scan matched, and `serve`
    reads the database, so the two disagreed exactly where it mattered most.
    After a `seed load` of 267 roles followed by `scan --limit 400`, the page
    written to `out/index.html` was titled "15 roles worth a look" while
    `job-radar serve` showed 270. Both were internally consistent and one of
    them was answering a question nobody asked: a reader wants their board,
    not the slice of it one command happened to touch.

    Settled roles are left out. `serve` returns them and greys them out
    client-side, which it can do because it has a page to grey them on; a
    written file has no such thing, and a rejected role reappearing on the
    next scan's page is worse than its absence.
    """
    from .models import Job, Salary
    _ensure_columns(con)
    gone = settled_uids(con)
    out = []
    # The same three clauses `cmd_list` uses, and for the same reasons.
    #
    # This was `LIVE_SQL` alone, which disagreed with both other views of the
    # board in three separate directions. A role you had APPLIED to dropped
    # off the written page as soon as its posting aged past the live window,
    # while `serve` and `list` both kept it. So did a role carrying a CV that
    # `generate` had been paid to write. And a url-less row imported from the
    # old `state/seen.json` appeared ON the page, rendered with `href=""`,
    # which is the exact bug `ACTIONABLE_SQL` was written to stop and which
    # this file already describes: "103 of them on this database,
    # indistinguishable from a live vacancy until you clicked one".
    q = (f"SELECT r.* FROM roles r "
         f"LEFT JOIN role_state s ON s.uid = r.uid "
         f"WHERE ({LIVE_SQL} OR COALESCE(s.status,'new') <> 'new'"
         f" OR r.uid IN (SELECT DISTINCT uid FROM artifacts)) "
         f"AND ({ACTIONABLE_SQL})")
    for r in con.execute(q):
        if r["uid"] in gone:
            continue
        j = Job(
            company=r["company"] or "", title=r["title"] or "",
            url=r["url"] or "", platform=r["platform"] or "",
            location=r["location"] or "", city=r["city"] or "",
            country=r["country"] or None,
            work_mode=r["work_mode"] or "unstated",
            employment=(r["employment"] if "employment" in r.keys()
                        else None) or "unstated",
            sector=r["sector"] or None, department=r["department"] or None,
            posted_at=r["posted_at"], description=r["description"] or "",
            salary=Salary(min=r["salary_min"], max=r["salary_max"],
                          currency=r["salary_currency"],
                          period=r["salary_period"] or "year",
                          confirmed=bool(r["salary_confirmed"]),
                          raw=r["salary_label"]))
        # The id it is stored under, not one re-derived from the url. See
        # `Job.stored_uid`.
        j.stored_uid = r["uid"]
        j.score = r["score"] or 0
        # Shape as well as parse. `output/interactive._flags`, written for
        # this same column this morning, ends `return out if isinstance(out,
        # list) else []`, and the comment here claimed store.py already
        # guarded it. It did not: `flags='5'` parsed fine and handed back an
        # int, and `flags='null'` handed back None, and the renderer then
        # raised `'int' object is not iterable` on the way out. Nothing
        # in-tree writes either, so this is a hardening gap rather than a live
        # fault, but what it takes down is the whole of a scan's output.
        j.reasons = _as_list(r["reasons"])
        j.flags = _as_list(r["flags"])
        out.append(j)
    return out


def settled_uids(con) -> set[str]:
    q = ",".join("?" * len(SETTLED))
    return {r["uid"] for r in con.execute(
        f"SELECT uid FROM role_state WHERE status IN ({q})", tuple(SETTLED))}


# -------------------------------------------------------------- artifacts

# Text small enough to keep forever. A .docx is a container, so its bytes are
# not worth storing, but the markdown it was built from is.
_TEXT_KINDS = {"screen", "jd_snapshot", "cover_letter", "cv"}


def _read_text(path) -> str:
    """The document's text, for keeping in the database.

    A generated screen costs real money and takes a minute, and storing only
    the file path meant one `rm -rf`, one moved folder, or one cleaned temp
    directory and it had to be bought again. The text is a few kilobytes.
    """
    p = Path(str(path or ""))
    if not p.exists() or p.is_dir():
        return ""
    if p.suffix.lower() == ".docx":
        # Prefer the markdown it was rendered from; fall back to extraction.
        md = p.with_suffix(".md")
        if md.exists():
            p = md
        else:
            try:
                from .runner import docx_to_text
                return docx_to_text(p)[:200_000]
            except Exception:
                return ""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:200_000]
    except OSError:
        return ""


def add_artifact(con, uid, kind, path="", rating=None, summary="", gates=None,
                 body=None) -> int:
    _ensure_columns(con)
    if body is None:
        body = _read_text(path) if kind in _TEXT_KINDS else ""
    cur = con.execute(
        """INSERT INTO artifacts (uid,kind,path,rating,summary,gates,created_at,body)
        VALUES (?,?,?,?,?,?,?,?)""",
        (uid, kind, str(path), rating, summary, json.dumps(gates or {}),
         date.today().isoformat(), body))
    return cur.lastrowid


def repair_smartrecruiters_urls(con) -> int:
    """Rewrite stored SmartRecruiters links that were built with /postings/.

    The public path has no such segment, so every one of them 404s. A dead
    link is only discovered after someone has decided to apply, so leaving
    them for the next scan to overwrite is not good enough.
    """
    import re as _re
    n = 0
    for r in con.execute(
            "SELECT uid,url FROM roles WHERE url LIKE "
            "'%jobs.smartrecruiters.com/%/postings/%'").fetchall():
        m = _re.match(r"(https://jobs\.smartrecruiters\.com/[^/]+)/postings?/(\d+)",
                      r["url"])
        if m:
            con.execute("UPDATE roles SET url=? WHERE uid=?",
                        (f"{m.group(1)}/{m.group(2)}", r["uid"]))
            n += 1
    return n


def _keep_locations(con, keep, members, cfg=None) -> None:
    """Join the offices of the merged copies onto the row that survives.

    Both halves of the line come from `screen.merged_location`, so the row a
    merge leaves behind is written the same way as the row `dedupe` produces
    at scan time. The "posted in N locations" flag is added the same way too,
    and only once: this runs on every scan, and a flag appended per run would
    grow a list of identical strings on the dashboard.
    """
    from .screen import merged_location
    locations = [(m["location"] or "") for m in members]
    text, n_locs = merged_location(locations, cfg)
    if not text:
        return
    try:
        flags = json.loads(keep["flags"] or "[]")
        if not isinstance(flags, list):
            flags = []
    except (TypeError, ValueError):
        flags = []
    # Replace rather than append: a third copy arriving next week makes the
    # count from last week wrong, and two contradictory counts on one row is
    # worse than the one that is out of date.
    kept_flags = [f for f in flags
                  if not (isinstance(f, str) and f.startswith("posted in ")
                          and f.endswith(" locations"))]
    if n_locs > 1:
        kept_flags.append(f"posted in {n_locs} locations")
    if text == (keep["location"] or "") and kept_flags == flags:
        return
    con.execute("UPDATE roles SET location=?, flags=? WHERE uid=?",
                (text, json.dumps(kept_flags), keep["uid"]))


def merge_duplicates(con, cfg=None) -> int:
    """Collapse rows that are the same job from more than one source.

    `dedupe` runs over one scan's results and then those are written to the
    database, so it never sees a copy that was already stored from an earlier
    run. Wise's Risk API role arrived from LinkedIn on one run and from
    SmartRecruiters on a later one, under identical titles, and nothing was
    ever going to bring them back together.

    The employer's own board wins over a keyword search, because it is the
    employer speaking and it carries the description the search does not. Any
    status, note or generated document on the losing row moves across first:
    the whole point of the merge is that you keep what you did.

    The locations move across too, and that is a decision rather than a
    detail. Company plus title is deliberately the same key `dedupe` uses,
    which means it treats one role advertised in London and in New York as one
    role: several applicant tracking systems publish a posting per office, and
    keying the merge on location instead would put those six rows back on the
    board and would still not separate genuinely different vacancies, because
    "London", "London, UK" and "London, England" are three spellings of one
    place. So the same-job question is answered the same way in both passes,
    and the cities are joined onto the survivor exactly as `dedupe` joins
    them, by the same function. Deleting the losing row outright, which is
    what this used to do, was the one place the two passes disagreed: a role
    open in two cities kept both when the copies arrived in one scan and lost
    the second city when they arrived a day apart.

    The one thing company plus title does NOT settle is pay. Kraken posts every
    role on Ashby twice, once for the United States with a band and once for the
    rest of the world with none, so the merge joined twenty-one countries onto
    the US row and left a US band on a row reading as a UK vacancy, linking to
    the US application. `screen.pay_attributable_groups` splits a group that far
    and no further, and it is shared with the scan-time pass so both still
    answer the same-job question identically.
    """
    from .screen import (PayMember, directness, pay_attributable_groups,
                         pay_key)
    _ensure_columns(con)
    groups: dict[tuple, list] = {}
    for r in con.execute(
            "SELECT uid, company, title, platform, description, salary_confirmed, "
            "fit, fit_why, location, flags, salary_currency, salary_min, "
            "salary_max, salary_period FROM roles").fetchall():
        key = (r["company"].strip().lower(), r["title"].strip().lower())
        groups.setdefault(key, []).append(r)

    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        # Same question the scan-time pass asks, asked the same way, because
        # the two of them disagreeing is the older bug this function's
        # docstring describes. Kraken's US posting and its rest-of-world
        # posting are one company and one title and are not one vacancy: the
        # band belongs to the US row alone, and merging them wrote it onto a
        # row that names the United Kingdom first.
        subgroups = pay_attributable_groups([
            PayMember(rank=(directness(r["platform"]),
                            r["salary_confirmed"] or 0,
                            len(r["description"] or "")),
                      location=r["location"] or "",
                      pay=pay_key(r["salary_currency"], r["salary_min"],
                                  r["salary_max"], r["salary_period"]),
                      item=r)
            for r in group])
        for members in subgroups:
            if len(members) < 2:
                continue
            members.sort(key=lambda r: (directness(r["platform"]),
                                        r["salary_confirmed"] or 0,
                                        len(r["description"] or "")), reverse=True)
            keep, losers = members[0], members[1:]
            _keep_locations(con, keep, members, cfg)
            for lose in losers:
                st = con.execute("SELECT status,note FROM role_state WHERE uid=?",
                                 (lose["uid"],)).fetchone()
                if st and st["status"] != "new":
                    cur = con.execute("SELECT status,note FROM role_state WHERE uid=?",
                                      (keep["uid"],)).fetchone()
                    cur_s = cur["status"] if cur else "new"
                    # Carry the further-along status across, not merely any status
                    # onto a blank one. The old rule only copied when the keeper
                    # was "new", so merging a role you were interviewing for into
                    # one you had merely marked interested threw the interview
                    # away -- and drafting a CV sets a role to "interested", so one
                    # click was enough to arm it. This runs unattended on scan.
                    if PROGRESS.get(st["status"], 0) > PROGRESS.get(cur_s, 0):
                        set_status(con, keep["uid"], st["status"], st["note"] or None)
                    elif st["note"] and not (cur and cur["note"]):
                        set_status(con, keep["uid"], cur_s, st["note"])
                # The fit score moves for the same reason the artifacts do: it was
                # paid for. `rank` spends real money and real minutes on it, and
                # this function runs unattended on every scan, so a duplicate
                # arriving on Tuesday quietly deleted Monday's score. It did not
                # read as a loss either: the keeper's fit stays -1, and -1 means
                # "not yet judged", so the role the model had scored 91 came back
                # as unranked, indistinguishable from one that had never been
                # looked at -- and `rank` then charged for it a second time.
                #
                # Only ever into a gap. A keeper that already has a score keeps it:
                # its score was judged against its own description, which is the
                # longer one, which is why it is the keeper.
                if lose["fit"] is not None and lose["fit"] >= 0:
                    kept_fit = con.execute("SELECT fit FROM roles WHERE uid=?",
                                           (keep["uid"],)).fetchone()
                    # Spelled out rather than `kept_fit["fit"] or -1`: a genuine
                    # score of 0 is falsy, and treating it as "no score" would let
                    # the merge overwrite the one verdict `rank` calls terminal.
                    kf = kept_fit["fit"] if kept_fit is not None else None
                    if kf is None or kf < 0:
                        con.execute("UPDATE roles SET fit=?, fit_why=? WHERE uid=?",
                                    (lose["fit"], lose["fit_why"] or "", keep["uid"]))
                con.execute("UPDATE artifacts SET uid=? WHERE uid=?",
                            (keep["uid"], lose["uid"]))
                con.execute("DELETE FROM jobs WHERE uid=?", (lose["uid"],))
                con.execute("DELETE FROM role_state WHERE uid=?", (lose["uid"],))
                con.execute("DELETE FROM roles WHERE uid=?", (lose["uid"],))
                merged += 1
    return merged


def backfill_bodies(con) -> int:
    """Store the text of documents produced before there was a column for it."""
    _ensure_columns(con)
    n = 0
    for a in con.execute("SELECT id,kind,path FROM artifacts "
                         "WHERE COALESCE(body,'')='' ").fetchall():
        if a["kind"] not in _TEXT_KINDS:
            continue
        text = _read_text(a["path"])
        if text:
            con.execute("UPDATE artifacts SET body=? WHERE id=?", (text, a["id"]))
            n += 1
    return n


def artifacts_for(con, uid) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM artifacts WHERE uid=? ORDER BY created_at DESC, id DESC", (uid,))]


def has_artifact(con, uid, kind) -> bool:
    return con.execute("SELECT 1 FROM artifacts WHERE uid=? AND kind=? LIMIT 1",
                       (uid, kind)).fetchone() is not None


# ------------------------------------------------------------------- jobs

def typical_seconds(con, kind: str) -> int:
    """How long this kind of job has actually taken here, or 0.

    The median of the last completed runs rather than a number in the source.
    A drafting run is three agent calls in the worst case and the wall clock
    moves with the model, the machine and the length of the advert, so a
    hardcoded "about eight minutes" would be wrong within a release and wrong
    in the direction that matters: a reader told to expect two minutes at
    minute six concludes it has hung and kills a job that was working.

    Zero when there is nothing to go on, and the caller says nothing rather
    than guessing. An estimate built from one sample is a guess wearing a
    number.
    """
    rows = con.execute(
        "SELECT (julianday(replace(finished_at,'T',' ')) "
        "      - julianday(replace(started_at,'T',' '))) * 86400 AS secs "
        "FROM jobs WHERE kind=? AND state='done' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 10", (kind,)).fetchall()
    secs = sorted(int(r["secs"]) for r in rows if r["secs"] and r["secs"] > 0)
    if len(secs) < 2:
        return 0
    return secs[len(secs) // 2]


def enqueue(con, uid, kind) -> int:
    """Queue a generation job, unless one is already pending or running."""
    existing = con.execute(
        "SELECT id FROM jobs WHERE uid=? AND kind=? AND state IN ('pending','running')",
        (uid, kind)).fetchone()
    if existing:
        return existing["id"]
    cur = con.execute("INSERT INTO jobs (uid,kind,requested_at) VALUES (?,?,?)",
                      (uid, kind, _now()))
    return cur.lastrowid


def next_pending(con, exclude_uids=()):
    """The oldest queued job, oldest first, skipping roles already busy.

    `exclude_uids` is how the pump avoids handing out a second job for a role
    that already has one running: two runs write the same folder, which is the
    collision the per-role lock exists to stop, and taking the lock only to
    release it again would let one busy role block the whole queue behind it.
    """
    if not exclude_uids:
        return con.execute("SELECT * FROM jobs WHERE state='pending' "
                           "ORDER BY id LIMIT 1").fetchone()
    marks = ",".join("?" * len(exclude_uids))
    return con.execute(
        f"SELECT * FROM jobs WHERE state='pending' AND uid NOT IN ({marks}) "
        f"ORDER BY id LIMIT 1", tuple(exclude_uids)).fetchone()


def pending_count(con) -> int:
    return con.execute(
        "SELECT COUNT(*) c FROM jobs WHERE state='pending'").fetchone()["c"]


def busy_uids(con, prefix: str = "generate") -> list[str]:
    """The roles holding a run lock right now."""
    n = len(prefix) + 1
    return [r["name"][n:] for r in con.execute(
        "SELECT name FROM locks WHERE name LIKE ?", (f"{prefix}:%",))]


def mark_job(con, job_id, state, error="", log="") -> None:
    stamp = "started_at" if state == "running" else "finished_at"
    con.execute(f"""UPDATE jobs SET state=?, {stamp}=?, error=?,
        log=substr(?,1,4000), attempts=attempts+? WHERE id=?""",
        (state, _now(), error, log, 1 if state == "running" else 0, job_id))


def running_count(con) -> int:
    return con.execute("SELECT COUNT(*) c FROM jobs WHERE state='running'").fetchone()["c"]


def reap_orphans(con, timeout_s: int = 900, restarted: bool = True) -> int:
    """Fail jobs whose worker is gone, and say why.

    A generation runs on a daemon thread inside the server process, so it
    cannot outlive it. Restart the server mid-generation, or have it crash,
    and the row stays 'running' for ever: the button spins on the dashboard
    with nothing behind it, and because the queue guard is
    `running_count >= 1`, every later generation is refused too. One
    interrupted click silently disabled the whole feature.

    Anything still marked RUNNING when the server starts belongs to a process
    that no longer exists. Anything older than the generation timeout is dead
    whatever started it. Pending rows are left alone: nothing is running them
    yet, so there is nothing to orphan, and `runner.pump` starts them when a
    slot frees.

    The two sweeps have to run in that order, and did not. The blanket one
    went first and matched every running and pending row unconditionally, so
    the timeout below it could never match anything: it was dead code, and
    the `timeout_s` the server passes was inert. A job that had been running
    for forty minutes and one that started ten seconds ago were both told
    "the server restarted while this was running", which for the first is the
    wrong explanation and for a reader chasing a stuck generation is a
    misleading one.

    `restarted` is what separates them. At startup both are true and every
    unfinished row is orphaned. Called while the server is up, only the
    timeout applies, because a job that is genuinely running must not be
    killed by the sweep that exists to clean up after a crash.
    """
    stale = con.execute(
        "UPDATE jobs SET state='failed', finished_at=?, error=? "
        "WHERE state='running' AND "
        "replace(started_at,'T',' ') < datetime('now','localtime',?)",
        (_now(), f"gave up after {timeout_s // 60} minutes", f"-{timeout_s} seconds"),
    ).rowcount
    if not restarted:
        return stale
    # Running only. A pending row is a job that has NOT started: no thread,
    # no subprocess, nothing to orphan, and `runner.pump` will pick it up as
    # soon as a slot frees. Failing those was right when a click ran
    # immediately and there was no queue behind it; with a queue it destroys
    # exactly what the queue is for. Nine screens were asked for, three ran
    # and six sat pending, and a restart failed all nine with "the server
    # restarted while this was running" on six that never had.
    n = con.execute(
        "UPDATE jobs SET state='failed', finished_at=?, error=? "
        "WHERE state='running'",
        (_now(), "interrupted: the server restarted while this was running. "
                 "Nothing was charged for the unfinished part; click again."),
    ).rowcount
    return n + stale


# ------------------------------------------------------------------- meta

def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_meta(con, k, v) -> None:
    con.execute("INSERT INTO meta (k,v) VALUES (?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def bump_runs(con) -> int:
    """Add one to the run counter, in the database rather than in Python.

    This was `n = read() + 1; write(n)`, which is a read-modify-write and
    loses an increment whenever anything else bumps in between. Two scans
    overlapping is the ordinary case, not an exotic one -- a cron scan and a
    person running one by hand -- and both of them finishing left the counter
    at one past where it started rather than two. No error, no lock message,
    just a number that had quietly stopped counting runs.

    One statement, so sqlite serialises it and neither caller can read a value
    the other is about to overwrite. The read-back afterwards may see a number
    a third scan has since bumped again, which is why nothing keys newness off
    this return value: `cmd_scan` pins its own run number before it writes a
    single role.
    """
    con.execute("INSERT INTO meta (k,v) VALUES ('runs','1') "
                "ON CONFLICT(k) DO UPDATE SET "
                "v = CAST(CAST(meta.v AS INTEGER) + 1 AS TEXT)")
    set_meta(con, "last_run", _now())
    return int(get_meta(con, "runs", "0"))


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# -------------------------------------------------------------- migration

def _further_along(con, uid: str) -> tuple:
    """How much of somebody's work is attached to this row.

    Used to pick the survivor when two ids resolve to one. Status first,
    because that is the thing a person set by hand; then anything generated
    against it, which cost money; then a fit score, which cost money too.
    """
    st = con.execute("SELECT status FROM role_state WHERE uid=?",
                     (uid,)).fetchone()
    arts = con.execute("SELECT COUNT(*) c FROM artifacts WHERE uid=?",
                       (uid,)).fetchone()["c"]
    row = con.execute("SELECT fit FROM roles WHERE uid=?", (uid,)).fetchone()
    fit = (row["fit"] if row is not None else None) or -1
    return (PROGRESS.get(st["status"] if st else "new", 0), arts, max(fit, -1))


def _absorb_into(con, *, keep: str, lose: str) -> None:
    """Move everything worth keeping from one row onto another, then drop it.

    The same carry-over `merge_duplicates` does, in the one other place two
    rows become one. Status by progress, so interviewing is never overwritten
    by new; a note into a gap; artifacts and queued jobs re-pointed, because
    they were paid for and generated against this posting; and the fit score
    only into a gap, because -1 means "not judged" and losing a 91 makes
    `rank` charge for it again.
    """
    st = con.execute("SELECT status, note FROM role_state WHERE uid=?",
                     (lose,)).fetchone()
    if st:
        cur = con.execute("SELECT status, note FROM role_state WHERE uid=?",
                          (keep,)).fetchone()
        cur_s = cur["status"] if cur else "new"
        if PROGRESS.get(st["status"], 0) > PROGRESS.get(cur_s, 0):
            set_status(con, keep, st["status"], st["note"] or None)
        elif st["note"] and not (cur and cur["note"]):
            set_status(con, keep, cur_s, st["note"])
    for table in ("artifacts", "jobs"):
        try:
            con.execute(f"UPDATE {table} SET uid=? WHERE uid=?", (keep, lose))
        except Exception:
            pass
    lose_fit = con.execute("SELECT fit, fit_why FROM roles WHERE uid=?",
                           (lose,)).fetchone()
    if lose_fit is not None and (lose_fit["fit"] or -1) >= 0:
        kept = con.execute("SELECT fit FROM roles WHERE uid=?",
                           (keep,)).fetchone()
        kf = kept["fit"] if kept is not None else None
        if kf is None or kf < 0:
            con.execute("UPDATE roles SET fit=?, fit_why=? WHERE uid=?",
                        (lose_fit["fit"], lose_fit["fit_why"], keep))
    con.execute("DELETE FROM role_state WHERE uid=?", (lose,))
    con.execute("DELETE FROM roles WHERE uid=?", (lose,))


def rekey_uids(con) -> int:
    """Recompute every role's id after the id rule changed. Returns how many.

    `Job.uid` used to throw the whole query string away before hashing, so an
    employer running Greenhouse behind their own careers page had every one of
    their postings collapse into a single role: `?gh_jid=111` and `?gh_jid=999`
    were the same id, and only whichever arrived first was ever stored.

    Without this, fixing that rule would make every affected role look new on
    the next scan, and would strand its status, its notes and any CV written
    against it under an id nothing refers to any more. On the database this
    was written against that is 384 of 3,692 roles, all of them carrying a
    status.

    Idempotent: a role whose id already matches the current rule is left
    alone, so running it twice costs one pass and changes nothing.
    """
    from .models import Job
    _ensure_columns(con)
    # The work list is read INSIDE the transaction. It used to be read before
    # `BEGIN`, so a writer inserting in that window made the rename hit the
    # primary key it had just checked was free: `IntegrityError: UNIQUE
    # constraint failed: roles.uid`, with the try/except wrapping only the
    # COMMIT, so nothing rolled back and the connection came back inside an
    # open write transaction holding a half-finished rename.
    previous = con.isolation_level
    con.isolation_level = None
    con.execute("BEGIN IMMEDIATE")
    con.execute("PRAGMA defer_foreign_keys=ON")
    try:
        return _rekey_inside(con, Job)
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.isolation_level = previous


def _rekey_inside(con, Job) -> int:
    """The body of `rekey_uids`, with its transaction already open."""
    rows = con.execute("SELECT uid, url, company, title, location "
                       "FROM roles").fetchall()
    moves = []
    for r in rows:
        # Only rows whose id came from a URL, because only that rule changed.
        #
        # A row with no URL is one `migrate` imported from the old
        # `state/seen.json`, and its id is whatever key that file used. It was
        # never derived from anything, so "recomputing" it invents a new one,
        # and then the legacy import no longer finds the id it wrote and
        # imports the same role again on the next scan. That turned a
        # migration meant to preserve history into one that duplicated a row
        # every time anybody scanned. Caught by
        # `test_migration_is_idempotent`, which is exactly what it is for.
        if not (r["url"] or "").strip():
            continue
        fresh = Job(company=r["company"] or "", title=r["title"] or "",
                    url=r["url"] or "", platform="",
                    location=r["location"] or "").uid
        if fresh != r["uid"]:
            moves.append((r["uid"], fresh))
    if not moves:
        con.execute("COMMIT")
        return 0

    # `role_state`, `artifacts` and `jobs` all reference `roles(uid)`, and
    # SQLite has no ON UPDATE CASCADE, so whichever side moves first points at
    # a row that does not exist yet. The caller defers the check to the commit
    # so both sides move inside one transaction and are still checked at the
    # end, which is stricter than switching foreign keys off and forgetting to
    # switch them back. The pragma is per-transaction and resets at every
    # commit, which is why it is set beside an explicit BEGIN rather than left
    # to the driver.
    taken = {r["uid"] for r in rows}
    done = 0
    for old, new in moves:
        if new in taken and new != old:
            # The row with the history survives, not whichever the SELECT
            # happened to return first. `taken` is seeded with every original
            # id, so "keep the one already under the new id" was decided by
            # rowid order, and the copy carrying an `applied` status was as
            # likely to be the one deleted as kept.
            if _further_along(con, old) > _further_along(con, new):
                old, new = new, old
            # Two ids resolving to one: the same posting reached by two URLs
            # differing only in a tracking parameter.
            #
            # This used to be a bare `DELETE FROM roles`, and `roles(uid)` is
            # referenced ON DELETE CASCADE by role_state, artifacts and jobs
            # with foreign keys ON. So one line took the status, the note,
            # every generated CV and every queued job with it, and `done` was
            # not incremented either, so `migrate` reported `rekeyed: 0` on a
            # run that had just destroyed a role somebody was interviewing
            # for. `merge_duplicates` solves the identical problem correctly
            # 240 lines above and this ignored it.
            #
            # Reachable two ways, both seen: `repair_smartrecruiters_urls`
            # rewrites a stored url at the end of every scan without
            # rekeying, so the next scan finds the pair; and a writer racing
            # the migration can leave one.
            _absorb_into(con, keep=new, lose=old)
            done += 1
            continue
        for table in ("role_state", "artifacts", "jobs"):
            try:
                con.execute(f"UPDATE {table} SET uid=? WHERE uid=?", (new, old))
            except Exception:
                # A table an older schema does not have. The roles row still
                # has to move, so this is not a reason to stop.
                pass
        con.execute("UPDATE roles SET uid=? WHERE uid=?", (new, old))
        taken.add(new)
        done += 1
    con.execute("COMMIT")
    return done


def migrate(con, state_path="state/seen.json", apps_path=None) -> dict:
    """Import the old stores. Idempotent: safe to run on every start.

    Runs only against rows that are not already present, so a second call
    changes nothing. That matters because this is called on every `scan`.

    Both paths resolve against the current working directory, which is the
    right default and was the wrong behaviour for anyone passing `--db`. A
    scan run from the repo with `--db /tmp/scratch.db` still read this
    directory's `state/seen.json` and `applications.local.yaml` and copied
    1,526 roles and a real application history into the scratch database.
    Nothing was written back, but `--db` reads as isolation and was not, and
    the copy is somebody's job search sitting in a temp directory.

    Pass `""` for either path to skip that import entirely, which is what a
    database outside the configured one now gets.
    """
    from .applications import Tracker

    done = get_meta(con, "migrated")
    out = {"roles": 0, "statuses": 0, "already": bool(done)}

    # Before anything else, and on every start rather than once, because the
    # rule it repairs is in `Job.uid` and a database can be older than any
    # marker we could set. It is a no-op the moment every row already agrees
    # with the current rule, so the cost of running it always is one pass.
    out["rekeyed"] = rekey_uids(con)

    sp = Path(state_path) if state_path else None
    if sp is not None and sp.exists():
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        for uid, e in (data.get("seen") or {}).items():
            if con.execute("SELECT 1 FROM roles WHERE uid=?", (uid,)).fetchone():
                continue
            con.execute("""INSERT INTO roles (uid,company,title,first_seen,last_seen)
                VALUES (?,?,?,?,?)""",
                (uid, e.get("company", ""), e.get("title", ""),
                 e.get("first_seen", date.today().isoformat()),
                 e.get("last_seen", date.today().isoformat())))
            con.execute("INSERT OR IGNORE INTO role_state (uid,status,updated_at) "
                        "VALUES (?,'new',?)", (uid, date.today().isoformat()))
            out["roles"] += 1
        if not get_meta(con, "runs"):
            set_meta(con, "runs", data.get("runs", 0))

    # `Tracker.load(None)` searches the working directory, which is what the
    # default wants. `""` is the explicit "there is no history to import here".
    tracker = Tracker() if apps_path == "" else Tracker.load(apps_path)
    for app in tracker.apps:
        rows = con.execute("SELECT uid, company, title FROM roles").fetchall()
        for r in rows:
            fake = type("J", (), {"company": r["company"], "title": r["title"],
                                  "url": ""})()
            if app.matches(fake):
                cur = con.execute("SELECT status FROM role_state WHERE uid=?",
                                  (r["uid"],)).fetchone()
                if cur and cur["status"] != "new":
                    continue
                set_status(con, r["uid"], app.status, app.note or None)
                out["statuses"] += 1
    set_meta(con, "migrated", _now())
    return out
