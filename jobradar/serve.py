"""A small local server so the dashboard can be worked from, not just read.

Standard library only: `http.server` and `sqlite3`. It binds to 127.0.0.1 and
runs while you are triaging, then stops. It is not a daemon and it is not
something to expose.

`scan` still writes the static file. This renders the same data with the
buttons live, from the same database, so the two cannot disagree.
"""

from __future__ import annotations

import errno
import json
import re
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html as _h
from pathlib import Path
from urllib.parse import quote, urlparse

from . import rank, runner, store
from .output import interactive

# Nothing this dashboard posts is large: the biggest body is a status plus a
# note. A Content-Length beyond this is a mistake, and reading it would be
# blocking on bytes that are not coming.
MAX_BODY = 1 << 20


# The most roles one bulk click will take.
#
# Not a technical limit: `MAX_RUNNING` is what bounds what actually runs, and
# the rest queue. This is a guard against a mis-click on "select all" over a
# four thousand row board turning into four thousand paid agent runs.
_MIME = {".pdf": "application/pdf", ".md": "text/markdown; charset=utf-8",
         ".txt": "text/plain; charset=utf-8",
         ".docx": ("application/vnd.openxmlformats-officedocument"
                   ".wordprocessingml.document")}


def _download_name(company: str, title: str, kind: str, suffix: str) -> str:
    """A filename you can find in a folder of downloads.

    Every generated CV is called CV.pdf on disk, which is fine in a directory
    named after the role and useless in ~/Downloads, where three of them
    become CV.pdf, CV (1).pdf and CV (2).pdf. The employer and the role go in
    front so the picker's own list is enough.
    """
    what = {"cv": "CV", "cover_letter": "Cover-letter",
            "screen": "Screening"}.get(kind, kind)
    def slug(s, n):
        s = re.sub(r"[^\w\s-]", "", s or "").strip()
        s = re.sub(r"[\s_]+", "-", s)
        return s[:n].strip("-")
    parts = [p for p in (slug(company, 28), slug(title, 40), what) if p]
    return "-".join(parts) + suffix


BULK_LIMIT = 40


class Handler(BaseHTTPRequestHandler):
    db_path = None
    docs_base = None
    config_path = None
    # The host `serve()` was asked to bind to, so `_expected_hosts` can accept
    # the name the browser will actually send. Empty means loopback only,
    # which is what every test and the default both want.
    bind_host = ""
    # `salary.currency` from the config, so the salary sort groups by the
    # currency the FLOOR is in rather than by whichever one happens to be
    # commonest on the board. Read once at startup: it cannot change while
    # the process runs, and re-reading it per request would put a file read
    # on the page load.
    home_currency = ""

    # ------------------------------------------------------------- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self._answered = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode("utf-8")
        self._answered = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """The posted JSON object, or {} if there is not one.

        Everything here is defensive for the same reason: an exception thrown
        out of a handler is not an error message, it is a dropped connection.
        The browser's `fetch` rejects, the click does nothing, and the page
        says nothing, which is indistinguishable from a button that is not
        wired up. So a body that is not an object, or a Content-Length that is
        not a number, becomes {} and the handler's own validation answers with
        a sentence.
        """
        raw = self.headers.get("Content-Length") or "0"
        try:
            n = int(raw)
        except ValueError:
            return {}
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return {}
        # `json.loads('[]')` and `json.loads('"hi"')` are both valid JSON and
        # neither has `.get`. Reaching `data.get("uid")` on one raised
        # AttributeError inside the handler and dropped the connection.
        return data if isinstance(data, dict) else {}

    def log_message(self, *a):
        pass                      # the scan output is the interesting log

    # ------------------------------------------------------------- routing
    #
    # Every request runs inside this. Four separate defects here all had the
    # same shape and the same symptom -- a malformed body, an unhashable
    # `kind`, a note that was not a string, and a write that lost a race with
    # a scan -- and each one killed the connection instead of answering:
    # http.server writes no status line when a handler raises, so the
    # browser's `fetch` rejected and the page said nothing at all. A click
    # that did nothing and a click that failed looked identical, and the one
    # that failed had usually just lost a status change.
    #
    # Wrapped here rather than around each `do_*` so there is one place that
    # cannot be forgotten, and so a handler added later gets it for free.
    def handle_one_request(self):
        self._answered = False
        try:
            super().handle_one_request()
        except Exception as e:
            if getattr(self, "_answered", False):
                raise            # too late to answer; let the server log it
            locked = (isinstance(e, sqlite3.OperationalError)
                      and ("locked" in str(e).lower() or "busy" in str(e).lower()))
            if locked:
                # The one failure here with an obvious cause and an obvious
                # thing to do about it: a scan holds the write lock while it
                # updates the board, and 15 seconds was not long enough.
                msg = ("the database is busy, most likely a scan is writing "
                       "to it. Nothing was saved. Try again in a moment.")
                code = 503
            else:
                msg = f"{type(e).__name__}: {e}"[:300]
                code = 500
            self.close_connection = True
            try:
                self._json({"ok": False, "error": msg}, code)
            except OSError:
                pass             # the client went away mid-answer

    def do_GET(self):
        # Reads were exempt from this and should not have been.
        #
        # The Host check exists to stop DNS rebinding, and it was only on the
        # POSTs and on /open. So a page on evil.example that had rebound its
        # own name to 127.0.0.1 could not WRITE anything -- but it could ask
        # for `/` and `/api/jobs` same-origin, and read back the whole board:
        # every employer, every application status, the private note on each
        # role, the fit scores, the text of every screening, and the paths of
        # the generated documents under ~/job-applications. For someone job
        # hunting out of a current job that is the most sensitive thing this
        # tool holds, and it was the one page not behind the check.
        #
        # Verified by sending `Host: evil.example:PORT` to `/`: 200 and the
        # full dashboard before this, 403 after.
        if not self._same_origin():
            return self._json(
                {"ok": False, "error": "cross-origin request refused"}, 403)
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            con = store.connect(self.db_path)
            try:
                return self._html(
                    interactive.render(con, self.home_currency))
            finally:
                con.close()
        if path == "/api/rank":
            # The estimate, plus whatever a run in progress has finished. The
            # click has to be able to show a cost before spending anything.
            con = store.connect(self.db_path)
            try:
                from urllib.parse import parse_qs
                from . import rank as rank_mod
                # The estimate follows the country the board is filtered to,
                # so the cost the click shows is the cost of what it will rank.
                countries = [c for c in parse_qs(urlparse(self.path).query)
                             .get("country", []) if c]
                rows = rank_mod.candidates(con, countries=countries or None)
                batches, tokens = rank_mod.estimate(rows)
                return self._json({
                    "pending": len(rows), "batches": batches, "tokens": tokens,
                    "countries": countries,
                    "unplaced": rank_mod.unplaced(con) if countries else 0,
                    "screen_tokens": len(rows) * rank.SCREEN_TOKENS,
                    "state": store.get_meta(con, "rank_state", "idle"),
                    "done": max(
                        int(store.get_meta(con, "rank_done", "0") or 0),
                        con.execute("SELECT COUNT(*) c FROM roles WHERE fit>=0")
                           .fetchone()["c"]
                        - int(store.get_meta(con, "rank_base", "0") or 0)),
                    "total": int(store.get_meta(con, "rank_total", "0") or 0),
                    "batch_size": rank_mod.BATCH,
                    "elapsed": _rank_elapsed(con),
                    "stopping": store.get_meta(con, "rank_cancel", "") == "1",
                    "error": store.get_meta(con, "rank_error", "") or "",
                    "scored": con.execute(
                        "SELECT COUNT(*) c FROM roles WHERE fit>=0").fetchone()["c"],
                    "unrankable": rank_mod.unrankable(con),
                })
            finally:
                con.close()

        if path == "/api/jobs":
            con = store.connect(self.db_path)
            try:
                # Two things were wrong with the obvious version of this
                # comparison, and together they made a failed job re-assert
                # its error onto the row for the rest of the day. So after a
                # fix, the dashboard kept showing the old failure and it
                # looked like nothing had been fixed.
                #
                #   * finished_at is written by Python as "...T13:59:13" while
                #     SQLite's datetime() returns "... 13:59:13". Comparing
                #     them as strings compares "T" against " ", and "T" always
                #     wins, so every job finished today looked recent.
                #   * datetime('now') is UTC; the stored value is local.
                rows = con.execute(
                    # `started_at` travels so the browser can show elapsed
                    # time. A spinner with no clock on an eight minute job is
                    # the same failure as a page that paints nothing for a
                    # second: it reads as hung, and the reader kills work that
                    # was going fine.
                    "SELECT id,uid,kind,state,error,started_at FROM jobs "
                    "WHERE state IN ('pending','running') OR "
                    "replace(finished_at,'T',' ') > "
                    "datetime('now','localtime','-2 minutes')").fetchall()
                arts = con.execute(
                    "SELECT uid,kind,path,rating,summary FROM artifacts").fetchall()
                states = con.execute("SELECT uid,status FROM role_state").fetchall()
                return self._json({
                    "jobs": [dict(r) for r in rows],
                    # Per kind, and only the kinds actually in flight, so a
                    # quiet poll stays a small answer.
                    "typical": {k: store.typical_seconds(con, k)
                                for k in {r["kind"] for r in rows}},
                    "now": store._now(),
                    "artifacts": [dict(r) for r in arts],
                    "states": {r["uid"]: r["status"] for r in states},
                })
            finally:
                con.close()
        if path.startswith("/download"):
            # Serve the document as a download rather than revealing it in
            # Finder.
            #
            # `open -R` puts a Finder window in front of you, which is the
            # right answer when you want to look at the file and the wrong one
            # when you are about to attach it: Chrome's upload dialog does not
            # know about that window, so you are hunting through ninety
            # `2026-09-01-company-role-hash` folders for `CV.pdf`. A download
            # lands in ~/Downloads, which is where the picker already opens.
            #
            # The name matters as much as the route. Every generated CV is
            # called CV.pdf, so downloading three of them gives you CV.pdf,
            # CV (1).pdf and CV (2).pdf, and the picker's most useful column
            # tells you nothing. The employer and the role go in the filename.
            if not self._same_origin():
                return self._json(
                    {"ok": False, "error": "cross-origin request refused"}, 403)
            from urllib.parse import parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            p = Path(unquote((q.get("path") or [""])[0] or ""))
            con = store.connect(self.db_path)
            try:
                row = con.execute(
                    "SELECT a.kind, r.company, r.title FROM artifacts a "
                    "JOIN roles r ON r.uid = a.uid WHERE a.path=? LIMIT 1",
                    (str(p),)).fetchone()
            finally:
                con.close()
            # The same allowlist by construction as /open: the path has to be
            # one already recorded in `artifacts`, so no amount of traversal
            # in the query string reaches anything else on the disk.
            if not row:
                return self._json(
                    {"ok": False,
                     "error": "that is not a document this tool made"}, 403)
            if not p.exists():
                return self._json({"ok": False, "error": "not found"}, 404)
            try:
                blob = p.read_bytes()
            except OSError as e:
                return self._json({"ok": False, "error": str(e)}, 500)
            name = _download_name(row["company"], row["title"], row["kind"],
                                  p.suffix)
            self.send_response(200)
            self.send_header("Content-Type", _MIME.get(p.suffix.lower(),
                                                       "application/octet-stream"))
            # ASCII only in the quoted form: a header is latin-1 and an
            # employer called "Nestlé" would raise inside the handler and drop
            # the connection. RFC 5987 carries the real one beside it.
            ascii_name = name.encode("ascii", "ignore").decode() or "document"
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(name)}")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return

        if path.startswith("/open"):
            # Checked like a POST. It is a GET, but it runs `open -R` on a
            # path from the query string, so any page in the browser could
            # have pointed it at anything on the disk.
            if not self._same_origin():
                return self._json(
                    {"ok": False, "error": "cross-origin request refused"}, 403)
            # Reveal a generated document in Finder rather than serving it.
            import subprocess
            from urllib.parse import parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            target = (q.get("path") or [""])[0]
            p = Path(unquote(target)) if target else None
            # Only documents this tool made. An allowlist by construction:
            # the path has to be one already recorded in `artifacts`, so no
            # amount of traversal in the query string reaches anything else.
            if p is not None:
                con = store.connect(self.db_path)
                try:
                    known = con.execute(
                        "SELECT 1 FROM artifacts WHERE path=? LIMIT 1",
                        (str(p),)).fetchone()
                finally:
                    con.close()
                if not known:
                    return self._json(
                        {"ok": False,
                         "error": "that is not a document this tool made"}, 403)
            if p and not p.exists():
                # The file moved or was deleted. The text is in the database,
                # so show that rather than telling someone a document they
                # paid for is gone.
                con = store.connect(self.db_path)
                try:
                    row = con.execute(
                        "SELECT body,kind FROM artifacts WHERE path=? AND "
                        "COALESCE(body,'')<>'' ORDER BY id DESC LIMIT 1",
                        (str(p),)).fetchone()
                finally:
                    con.close()
                if row:
                    return self._html(
                        "<pre style='white-space:pre-wrap;font:14px/1.6 "
                        "ui-monospace,monospace;max-width:44rem;margin:3rem auto;"
                        "padding:0 1.5rem'>"
                        f"<b>{_h.escape(p.name)} is no longer on disk. This is "
                        f"the copy kept in the database.</b>\n\n"
                        f"{_h.escape(row['body'])}</pre>")
            if p and p.exists():
                # check=False does not suppress FileNotFoundError, so on a
                # machine without `open` this raised inside the handler and
                # dropped the connection.
                import sys as _sys
                cmds = ([["open", "-R", str(p)]] if _sys.platform == "darwin"
                        else [["xdg-open", str(p.parent)], ["explorer", str(p.parent)]])
                for c in cmds:
                    try:
                        subprocess.run(c, check=False)
                        return self._json({"ok": True})
                    except (FileNotFoundError, OSError):
                        continue
                return self._json({"ok": False,
                                   "error": f"could not reveal it; the file is at {p}"})
            return self._json({"ok": False, "error": "not found"}, 404)
        self.send_error(404)

    def _expected_hosts(self) -> set[str]:
        """The names this server is allowed to be reached by.

        The loopback names, plus whatever `--host` was actually given. Without
        the last one, `job-radar serve --host 0.0.0.0` opened the browser at
        `http://0.0.0.0:8765/`, and every write from that page was refused with
        "cross-origin request refused" -- a flag that silently broke the
        buttons. It is not a hole: a rebinding attack needs the ATTACKER's
        name in Host, and that is a name the person running this never typed.
        """
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if self.bind_host:
            allowed.add(f"{self.bind_host}:{port}")
        return allowed

    def _same_origin(self) -> bool:
        """Reject cross-site posts, and reject rebinding.

        This server spends money. Without a check, any page open in the same
        browser could POST to /api/generate, and a text/plain body is a simple
        request so there is no preflight to stop it.

        Comparing Origin to Host was not enough. Both headers are attacker
        controlled together: a page on evil.example that has rebound its own
        DNS to 127.0.0.1 sends Origin: http://evil.example and Host:
        evil.example, they match, and the request went through to
        /api/generate. So Host is checked against what this server actually
        bound to, and Origin against the same set.
        """
        host = self.headers.get("Host", "")
        allowed = self._expected_hosts()
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True                      # curl and same-origin form posts
        return origin.split("//")[-1] in allowed

    def _start_generation(self, con, uid, kind):
        """Queue one role. Returns (job_id, "") or (None, reason).

        Shared by the single button and the bulk one so a role cannot be
        accepted by one path and refused by the other. Every check here is a
        refusal that costs nothing; the thing being guarded costs money.
        """
        if kind == "cover_letter" and not store.has_artifact(con, uid, "cv"):
            return None, ("draft the CV first: the letter is checked against "
                          "it for repeated phrasing")
        if kind in ("screen", "cv", "cover_letter"):
            # Not screen alone. A CV drafted against "_No description
            # available from this source._" is a full agent run, a rating, and
            # a document tailored to nothing.
            row = con.execute("SELECT description FROM roles WHERE uid=?",
                              (uid,)).fetchone()
            if row is None:
                return None, "no such role"
            if len((row["description"] or "").strip()) < 200:
                return None, ("this posting has no description, so there is "
                              "nothing to screen. Open the advert instead.")
        # Queue it, then let the pump decide what runs. The cap belongs on
        # what is RUNNING, not on what may be asked for: it used to sit here,
        # so nine selected roles became three started and six refused while
        # the dialog that took the click promised the rest would queue.
        #
        # `enqueue` is idempotent per role and kind, so a double-click returns
        # the same job rather than a second one writing the same folder.
        job_id = store.enqueue(con, uid, kind)
        con.commit()
        runner.pump(db_path=self.db_path, base=self.docs_base,
                    config_path=self.config_path)
        return job_id, ""

    def do_POST(self):
        if not self._same_origin():
            return self._json({"ok": False, "error": "cross-origin request refused"}, 403)
        path = urlparse(self.path).path
        data = self._body()

        if path == "/api/pull":
            # `git pull` on the checkout the server is running from. Read-only
            # in the sense that matters: it fetches and fast-forwards, and it
            # is refused outright if the working tree has changes, because a
            # button that silently merges over someone's edits is worse than
            # no button. Nothing is pushed and no history is rewritten.
            import subprocess
            repo = Path(__file__).resolve().parent.parent

            def git(*a, timeout=90):
                return subprocess.run(["git", "-C", str(repo), *a],
                                      capture_output=True, text=True, encoding="utf-8",
                                      timeout=timeout)

            if not (repo / ".git").exists():
                return self._json(
                    {"ok": False,
                     "error": "this is not a git checkout, so there is nothing "
                              "to pull. Re-download the source list by hand."}, 409)
            dirty = git("status", "--porcelain").stdout.strip()
            if dirty:
                return self._json(
                    {"ok": False,
                     "error": "you have uncommitted changes here, so this will "
                              "not merge over them. Commit or stash, then pull "
                              "from a terminal."}, 409)
            before = git("rev-parse", "HEAD").stdout.strip()
            r = git("pull", "--ff-only")
            if r.returncode:
                return self._json(
                    {"ok": False,
                     "error": (r.stderr or r.stdout).strip()[:300] or "pull failed"},
                    409)
            after = git("rev-parse", "HEAD").stdout.strip()
            from . import sources as src_mod
            if before == after:
                return self._json({"ok": True, "changed": False,
                                   "message": "already up to date"})
            n = git("diff", "--shortstat", before, after, "--",
                    "sources/sources.json").stdout.strip()
            return self._json({
                "ok": True, "changed": True,
                "message": f"pulled. source list {n or 'unchanged'}",
                "age": src_mod.age_days()})

        if path == "/api/rank/stop":
            con = store.connect(self.db_path)
            try:
                if store.get_meta(con, "rank_state", "idle") != "running":
                    return self._json({"ok": False, "error": "not running"}, 409)
                store.set_meta(con, "rank_cancel", "1")
            finally:
                con.close()
            # It stops between batches, not mid-request, so say so rather than
            # letting the button imply it halted instantly.
            return self._json({"ok": True,
                               "message": "stopping after the current batch"})

        if path == "/api/rank":
            # Not per-role, so it does not carry a uid and must be handled
            # before the uid check below.
            con = store.connect(self.db_path)
            try:
                # An atomic claim, not a read-then-decide: three parallel
                # requests all passed the old check and started three full
                # runs, which on a 300-role board is triple the spend.
                if not store.claim(con, "rank"):
                    return self._json(
                        {"ok": False, "error": "already ranking"}, 429)
                # Everything from here to the spawn has to give the lock back
                # if it throws. `candidates` is a query and the `set_meta`
                # calls are writes, so any of them can lose a race with a scan
                # and raise "database is locked" -- and a lock left taken
                # refuses every later rank with "already ranking" until the
                # server is restarted, which is the exact wedge `clear_locks`
                # exists to undo.
                try:
                    from . import rank as rank_mod
                    countries = data.get("countries") or None
                    if countries is not None and not (
                            isinstance(countries, list)
                            and all(isinstance(c, str) for c in countries)):
                        store.release(con, "rank")
                        return self._json(
                            {"ok": False, "error": "bad countries"}, 400)
                    rows = rank_mod.candidates(
                        con, refresh=bool(data.get("refresh")),
                        countries=countries)
                    if not rows:
                        store.release(con, "rank")
                        return self._json(
                            {"ok": False,
                             "error": "every role with a description already "
                                      "has a fit score"}, 409)
                    from datetime import datetime
                    store.set_meta(con, "rank_state", "running")
                    store.set_meta(con, "rank_total", str(len(rows)))
                    store.set_meta(con, "rank_done", "0")
                    store.set_meta(con, "rank_cancel", "")
                    store.set_meta(con, "rank_error", "")
                    # Where the scored count stood before this run, so progress
                    # can be read live off the database rather than only
                    # advancing when a batch finishes.
                    store.set_meta(con, "rank_base", str(con.execute(
                        "SELECT COUNT(*) c FROM roles WHERE fit>=0")
                        .fetchone()["c"]))
                    store.set_meta(con, "rank_started",
                                   datetime.now().isoformat(timespec="seconds"))
                except BaseException:
                    _abandon_rank(con)
                    raise
            finally:
                con.close()
            _spawn_rank(self.db_path, self.config_path,
                        refresh=bool(data.get("refresh")), countries=countries)
            return self._json({"ok": True, "roles": len(rows)})

        if path == "/api/generate/bulk":
            con = store.connect(self.db_path)
            try:
                # One request, many roles, and a per-role answer. A bulk
                # button that returns a single ok/failed is unusable: a
                # shortlist where two roles have no description and one is
                # already running has to say WHICH, or the reader has to open
                # forty rows to find out what actually started.
                kind = data.get("kind")
                if not isinstance(kind, str) or kind not in runner.KINDS:
                    return self._json({"ok": False, "error": "bad kind"}, 400)
                uids = data.get("uids")
                if not isinstance(uids, list) or not uids:
                    return self._json(
                        {"ok": False, "error": "no roles selected"}, 400)
                if len(uids) > BULK_LIMIT:
                    return self._json(
                        {"ok": False,
                         "error": f"{len(uids)} roles asked for at once; "
                                  f"{BULK_LIMIT} is the most this will take "
                                  f"in one go"}, 400)
                accepted, skipped = [], []
                for one in uids:
                    if not isinstance(one, str):
                        continue
                    job, why = self._start_generation(con, one, kind)
                    if job:
                        accepted.append(one)
                    else:
                        skipped.append({"uid": one, "why": why})
                running = len(store.busy_uids(con))
                return self._json({"ok": True, "kind": kind,
                                   "queued": accepted, "started": accepted,
                                   "running": running,
                                   "skipped": skipped})
            finally:
                con.close()

        uid = data.get("uid")
        # An unknown uid used to raise inside the handler and drop the
        # connection, so the browser's fetch rejected and the click silently
        # did nothing. A missing uid inserted a NULL row, because SQL foreign
        # keys ignore NULL.
        if not isinstance(uid, str) or not uid:
            return self._json({"ok": False, "error": "a role id is required"}, 400)
        con = store.connect(self.db_path)
        try:
            if not con.execute("SELECT 1 FROM roles WHERE uid=?", (uid,)).fetchone():
                return self._json({"ok": False, "error": "no such role"}, 404)
            if path == "/api/status":
                status = data.get("status", "")
                if not isinstance(status, str) or status not in store.STATUSES:
                    return self._json({"ok": False, "error": "bad status"}, 400)
                # None means "not supplied, keep the note that is there"; a
                # string means "use this one, even if it is empty". Anything
                # else is neither, and sqlite refused to bind it: the request
                # died with no response and the click did nothing.
                note = data.get("note")
                if note is not None and not isinstance(note, str):
                    return self._json(
                        {"ok": False, "error": "a note has to be text"}, 400)
                store.set_status(con, uid, status, note)
                return self._json({"ok": True, "uid": uid, "status": status})

            if path == "/api/generate":
                kind = data.get("kind")
                # `kind not in runner.KINDS` hashes `kind`, so a list or a
                # dict raised TypeError rather than answering "bad kind".
                if not isinstance(kind, str) or kind not in runner.KINDS:
                    return self._json({"ok": False, "error": "bad kind"}, 400)
                # The cover letter needs the CV to check itself against.
                ok, why = self._start_generation(con, uid, kind)
                if not ok:
                    return self._json({"ok": False, "error": why},
                                      429 if "running" in why else 409)
                return self._json({"ok": True, "job": ok, "kind": kind})
        finally:
            con.close()
        self.send_error(404)


def _rank_elapsed(con) -> int:
    """Seconds since the current rank run started, or 0.

    The counter only moves once per batch, about two minutes apart, so without
    a clock beside it a run in progress is indistinguishable from one that has
    hung.
    """
    from datetime import datetime
    stamp = store.get_meta(con, "rank_started", "")
    if not stamp:
        return 0
    try:
        return max(0, int((datetime.now() - datetime.fromisoformat(stamp)).total_seconds()))
    except ValueError:
        return 0


def _abandon_rank(con, error: str = "") -> None:
    """Put the rank button back, from whatever state a failure left it in.

    Called on every path that gives up after the lock was taken. Each write is
    separately guarded because this runs when something is already wrong with
    the database, and a second failure here must not stop the remaining
    writes: leaving `rank_state` on "running" or the lock in place is a wedge
    that lasts until the next restart.
    """
    for k, v in (("rank_state", "idle"), ("rank_cancel", ""),
                 ("rank_error", error[:300] if error else None)):
        if v is None:
            continue
        try:
            store.set_meta(con, k, v)
        except Exception:
            pass
    try:
        store.release(con, "rank")
    except Exception:
        pass


def _spawn_rank(db_path, config_path, refresh: bool = False,
                countries=None) -> None:
    """Rank on a background thread so the click returns at once.

    Progress goes in `meta` rather than a job row: ranking is about the board
    rather than one role, and the jobs table has a foreign key to a role.
    """
    import threading

    def work():
        from . import rank as rank_mod
        from .config import load as load_cfg
        try:
            con = store.connect(db_path)
        except Exception as e:
            # This sat outside the try below, so a connection that failed --
            # a scan holding the write lock, a full disk -- killed the thread
            # before any of the recovery underneath could run. `rank_state`
            # stayed "running" with no error text and the "rank" lock stayed
            # taken, so the dashboard showed a run in progress that would
            # never move and refused every later click with "already ranking"
            # for the life of the server. Same failure, one line higher up.
            try:
                with store.open_db(db_path) as c2:
                    _abandon_rank(c2, f"could not open the database: {e}")
            except Exception:
                pass
            raise
        try:
            cfg = load_cfg(config_path) if config_path else load_cfg()
            # The same filter the click was priced with. Without it the button
            # would show the UK count and then rank the whole board.
            rows = rank_mod.candidates(con, refresh=refresh, countries=countries)
            rank_mod.rank(
                con, cfg, rows,
                on_batch=lambda done, total, scored:
                    store.set_meta(con, "rank_done", str(done)),
                should_stop=lambda: store.get_meta(con, "rank_cancel", "") == "1")
            store.set_meta(con, "rank_state", "idle")
            store.set_meta(con, "rank_cancel", "")
        except BaseException as e:
            # Never leave it stuck on "running": the button would spin for
            # ever and refuse every later attempt.
            #
            # `BaseException`, not `Exception`, and the comment above is the
            # reason. `rank._call` raises SystemExit when the `claude` binary
            # is missing, SystemExit is a BaseException, and it walked straight
            # past the handler that exists to stop exactly this. `rank_state`
            # then stayed "running" for the life of the database, the button
            # was disabled for ever, and no error text ever reached the page:
            # a missing binary bricked the dashboard. Whatever a worker raises,
            # the state gets cleared and the reader gets a sentence.
            from .runner import LimitReached
            store.set_meta(con, "rank_state", "idle")
            # A cancel flag left set would stop the next run before its first
            # batch, which reads as the button doing nothing at all.
            store.set_meta(con, "rank_cancel", "")
            # What was actually written, which is not always nothing. The
            # connection is in autocommit, so a failure part way through the
            # apply loop leaves real scores in the database, and saying "the
            # roles are unchanged" about them sends someone looking for a bug
            # that is not there. `rank` stamps the count onto the exception.
            saved = int(getattr(e, "scored", 0) or 0)
            if isinstance(e, LimitReached):
                msg = (f"stopped: out of credit or rate limited ({e}). "
                       f"Everything scored so far is saved; run it again when "
                       f"the limit resets and it picks up where it left off.")
            else:
                what = (f"{saved} role(s) were scored and saved before it "
                        f"stopped." if saved else
                        "Nothing was scored; the roles are unchanged.")
                msg = f"ranking failed: {e}. {what}"
            store.set_meta(con, "rank_error", msg[:300])
            # Cleared first, then honoured. A KeyboardInterrupt or a
            # SystemExit still means what it says; `finally` below releases the
            # lock and closes the connection on the way out, and this runs on a
            # daemon thread where re-raising ends the thread and nothing else.
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            store.release(con, "rank")
            con.close()

    threading.Thread(target=work, daemon=True).start()


def already_serving(host: str = "127.0.0.1", port: int = 8765) -> bool:
    """Whether something is already listening there.

    Asked before starting a dashboard rather than after, because binding a
    port that is taken raises out of `serve` before it prints anything useful,
    and the second copy would be racing the first for the same database.
    """
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.4)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def open_in_background(db_path=None, host: str = "127.0.0.1", port: int = 8765,
                       docs_base=None, config_path=None) -> str | None:
    """Start a dashboard that outlives this process, and return its URL.

    A scan takes over an hour and the first pass is usable after five minutes,
    so the dashboard should be there when it becomes worth looking at rather
    than when the scan happens to end. Detached, because the scan has another
    seventy minutes to run and the person wants to click things now.

    Returns None and starts nothing if a dashboard is already up: they would
    contend for the same database, and the one that lost would print a SQLite
    traceback at somebody who had done nothing wrong.
    """
    import subprocess
    import sys

    if already_serving(host, port):
        return None
    cmd = [sys.executable, "-m", "jobradar.cli"]
    if config_path:
        cmd += ["-c", str(config_path)]
    cmd += ["serve", "--no-browser", "--host", host, "--port", str(port)]
    if db_path:
        cmd += ["--db", str(db_path)]
    if docs_base:
        cmd += ["--docs", str(docs_base)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return None
    # Wait for the bind rather than guessing at it, so the browser is not
    # opened at a port nothing is answering yet.
    import time
    for _ in range(40):
        if already_serving(host, port):
            return f"http://{host}:{port}/"
        time.sleep(0.25)
    return None


def serve(db_path=None, host="127.0.0.1", port=8765, open_browser=True,
          docs_base=None, config_path=None) -> int:
    # Take the port FIRST, before touching the database.
    #
    # Everything below assumes "I am starting, therefore no other server is
    # running", and clears interrupted generations, locks and rank state on
    # the strength of it. A launchd job with KeepAlive breaks that assumption
    # in the worst possible way: it retries every few seconds, loses the bind
    # to the server that is already up, and on the way to losing it reaps the
    # healthy server's work. Seven queued screenings were killed four seconds
    # after they started, by a process that never served a single request.
    #
    # A failed bind is the only reliable way to know another server owns this
    # database. So bind first, and if the port is taken, leave everything
    # alone and say so.
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        # A second `serve` in another window, or the last one still running,
        # is the ordinary way to hit this, and it came out as a nine-frame
        # socketserver traceback ending in "Address already in use", which
        # reads as a broken tool rather than as a port that is taken.
        if getattr(exc, "errno", None) in (errno.EADDRINUSE, errno.EACCES):
            why = ("is already in use, most likely by a `job-radar serve` "
                   "that is still running"
                   if exc.errno == errno.EADDRINUSE
                   else "needs privileges this process does not have")
            print(f"Port {port} {why}. Either stop that one, or start this "
                  f"one somewhere else with `--port {port + 1}`.", flush=True)
            return 1
        raise

    # Gates are recomputed on start, so a fixed check corrects the rows it got
    # wrong rather than only applying to future runs.
    con = store.connect(db_path)
    try:
        # Documents made before there was a column for their text.
        store.backfill_bodies(con)
        # A generation cannot outlive the process that spawned it, so anything
        # still "running" here is from a server that is gone.
        orphans = store.reap_orphans(con, runner.TIMEOUT)
        if orphans:
            print(f"  cleared {orphans} interrupted generation(s)")
        # A rank runs on a thread inside this process, so one marked running
        # here belongs to a server that is gone. Left alone it wedges the
        # button for ever, because starting a rank refuses while one is
        # "running". Everything already scored is kept.
        if store.get_meta(con, "rank_state", "idle") == "running":
            store.set_meta(con, "rank_state", "idle")
            store.set_meta(con, "rank_cancel", "")
            print("  cleared an interrupted ranking run")
        # A lock outlives the process that took it, so a crash mid-run would
        # otherwise refuse every generation and every rank for ever.
        if store.clear_locks(con):
            print("  released locks held by a previous run")
        # Housekeeping, and housekeeping must never be the reason the
        # dashboard will not start. `regate` rewrites the quality gates on
        # every stored document, so it writes, and a write loses to anything
        # else holding the database: a scan, a rank, a second window. That
        # raised straight out of `serve` and the server never reached its
        # bind, so a scan running in another terminal meant the dashboard
        # simply would not open, with a SQLite traceback as the explanation.
        #
        # Observed doing exactly that. The gates it refreshes are already
        # correct on disk from when each document was written; re-checking
        # them is an upgrade path for documents written before the gate was
        # fixed, and that can wait for the next start.
        try:
            n = runner.regate(con)
            if n:
                print(f"  rechecked {n} document(s)", flush=True)
        except sqlite3.OperationalError as e:
            print(f"  skipped re-checking documents: {e}. "
                  f"Something else is using the database.", flush=True)
    finally:
        con.close()

    Handler.db_path = db_path
    Handler.docs_base = docs_base
    Handler.bind_host = host or ""
    # Without this the runner resolved a config from its working directory, so
    # generation used whatever config.yaml happened to be in cwd rather than
    # the one passed on the command line.
    Handler.config_path = config_path
    # A config that will not load must not stop the dashboard starting: the
    # board and everything already on it are in the database, not the config.
    # Losing the sort grouping is a fair price; losing the page is not.
    try:
        from .config import load as _load_cfg
        Handler.home_currency = (
            _load_cfg(config_path).salary_currency or "").upper()
    except Exception:
        Handler.home_currency = ""
    url = f"http://{host}:{port}/"
    print(f"job-radar is at {url}", flush=True)
    print("  buttons: screen, CV, cover letter, apply, skip", flush=True)
    # Flushed, all three. Python block-buffers stdout when it is not a
    # terminal, so `job-radar serve > serve.log &` -- which is how anyone
    # running it in the background or under a supervisor starts it -- wrote
    # an empty file and kept it empty for the life of the process. A server
    # that is up and silent is indistinguishable from one that hung on the
    # bind, and the URL is the one thing the reader came for.
    print("  nothing generates unless you click it. Ctrl-C to stop.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
