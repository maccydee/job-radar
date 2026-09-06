"""Interactive config builder.

Deliberately a plain CLI wizard rather than something clever, because the
people this tool is meant to widen to are exactly the ones who will not
hand-edit YAML. The `/job-radar setup` skill wraps this same writer with a
conversational layer; both end at `write_config`, so the two front doors
cannot drift apart.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .state import atomic_write_text

COMMON_DEALBREAKERS = {
    "coding round": r"take.?home|live coding|coding (?:test|assessment|challenge|exercise)|"
                    r"pair.?program\w* (?:interview|round)|technical assessment",
    "hands-on / player-coach": r"player.?coach|hands.on cod|writes? code|still cod|"
                               r"contribut\w+ to (?:the )?code|roll up your sleeves",
    "on-call": r"on.?call rota|24/7 on.?call|carry the pager",
    "shift work": r"shift (?:work|pattern)|night shift|rotating shifts",
    "travel heavy": r"travel (?:up to )?(?:5\d|[6-9]\d)%|frequent(?:ly)? travel|extensive travel",
    "pre-sales": r"pre.?sales|solutions? (?:architect|engineer)|forward deployed",
    "manages managers": r"managing managers|manager of managers|people managers report",
}

# Must match the tags actually used in sources/sources.json. Offering
# "manufacturing" and "transport", which do not exist, while omitting
# "industry", "travel", "telecoms" and "charity", which do, meant picking your
# own sector could silently reduce you to the keyword searches alone.
# Kept in step with the tags actually present in the bundled source list; a
# test fails if the two drift. Offering a tag with no employers behind it sends
# someone away with an empty scan, and not offering one that exists hides a
# sector from the person it was added for.
SECTORS = [
    "technology", "finance", "healthcare", "public-sector", "education",
    "retail", "industry", "professional-services", "media", "travel",
    "telecoms", "charity", "hospitality", "legal", "security",
    "energy",
    "construction",
    "transport",
]

# Job titles as they appear on a real CV: usually followed by an employer, a
# date range, or both, on the same line. Requiring the title to be alone on
# its line returned nothing for every CV tested.
_ROLE_WORD = (r"manager|director|lead|head|engineer|analyst|architect|consultant|"
              r"specialist|officer|administrator|coordinator|designer|scientist|"
              r"nurse|teacher|accountant|partner|advisor|adviser|controller|"
              r"practitioner|educator|developer|technician|supervisor|assistant")
_TITLE_HINT = re.compile(
    rf"(?:^|\n)[ \t]*((?:[A-Z][\w/&.'-]*[ \t]+){{0,4}}(?:{_ROLE_WORD})"
    rf"(?:\s+(?:of|for|-)\s+[A-Z][\w/&.'-]*)?)"
    rf"(?=\s*(?:$|[,|\u2013\u2014-]|\t|\s{{2,}}|\bat\b|\())",
    re.I | re.M,
)


# The sources that cannot work without a key, said out loud in the wizard
# rather than discovered from an error message later.
#
# `why` is the reason to bother, with a number, because "Reed needs a key" is
# not an argument and "418 contract roles against 19" is.
API_KEY_SOURCES = (
    ("Reed", "https://www.reed.co.uk/developers/jobseeker",
     "UK jobs, and the only realistic route to UK contract and day-rate work. "
     "Measured 6 Sept 2026: 418 contract listings for 'engineering manager', "
     "against 19 across every unkeyed board in this tool.",
     (("reed_api_key", "API key"),)),
    ("Adzuna", "https://developer.adzuna.com/signup",
     "19 national job indexes, so the one source here that can watch more "
     "than one country from the same config.",
     # Two values, not one. Asking for a single "Adzuna key" would collect
     # half a credential and fail at the first fetch with a 401 that says
     # nothing about which half is missing.
     (("adzuna_app_id", "app id"), ("adzuna_app_key", "app key"))),
)


def _word_pattern(word: str) -> str:
    """A plain word the user typed, as a regex that matches that word only.

    The prompt says "plain words are fine" and these become HARD
    dealbreakers, so the pattern has to mean what the typist meant. Bare
    `re.escape` does not: it is unanchored, so "Java" hid every posting
    mentioning JavaScript, "Go" hid "Golang" and "we are going to", and a
    single letter hid the entire result set. The user is then looking at an
    empty first scan with nothing on screen saying which word emptied it.

    Boundaries are added only where the edge is a word character, because
    "C++" and ".NET" end and begin on punctuation and `\b` after a "+" would
    never match at all -- which is the same fault in the other direction.
    """
    esc = re.escape(word)
    lead = r"\b" if word[:1].isalnum() or word[:1] == "_" else ""
    tail = r"\b" if word[-1:].isalnum() or word[-1:] == "_" else ""
    return f"{lead}{esc}{tail}"


class NoInput(Exception):
    """stdin ended, so there is nobody there to answer the next question."""


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        # Returning the default here looked kind and was not. Once stdin is
        # at EOF every later input() raises immediately, so the two questions
        # that loop until they get an answer (the CV, and the job titles)
        # never got one and never stopped asking: `job-radar setup
        # < /dev/null` produced 474MB of output in 25 seconds. The questions
        # that do not loop were no better, they silently accepted every
        # default and wrote a config the user never saw.
        raise NoInput from None
    return v or default


def _ask_list(prompt: str, default: list[str] | None = None) -> list[str]:
    d = ", ".join(default or [])
    v = _ask(f"{prompt} (comma separated)", d)
    return [x.strip() for x in v.split(",") if x.strip()]


def _ask_yn(prompt: str, default: bool = True) -> bool:
    v = _ask(f"{prompt} (y/n)", "y" if default else "n").lower()
    return v.startswith("y")


def titles_from_cv(text: str, limit: int = 12) -> list[str]:
    """Pull plausible job titles out of pasted CV text.

    Crude on purpose: it reads lines that look like job titles and returns the
    common ones. It is a starting point the user edits, not a recommendation,
    and it says so. The skill layer does this properly with a model.
    """
    hits: dict[str, int] = {}
    for m in _TITLE_HINT.finditer(text):
        t = " ".join(m.group(1).split()).lower().strip(" ,.-")
        # Drop leading filler that reads as part of the title on a CV line.
        t = re.sub(r"^(?:senior|junior|lead|principal|interim|acting)\s+(?=\w)", "", t)
        if 3 <= len(t) <= 45 and not t.startswith(("and ", "the ", "a ")):
            hits[t] = hits.get(t, 0) + 1
    ranked = sorted(hits.items(), key=lambda x: (-x[1], len(x[0])))
    return [t for t, _ in ranked[:limit]]


def write_config(path: Path, answers: dict) -> Path:
    """The single place a config file is written. Keeps comments, because the
    file is meant to stay hand-editable after the wizard has run.
    """
    def ylist(items, indent="    "):
        if not items:
            return " []"
        return "\n" + "\n".join(f"{indent}- {_q(i)}" for i in items)

    def _q(v):
        """Quote for YAML using SINGLE quotes.

        This matters more than it looks. Dealbreakers are regexes, and YAML
        processes backslash escapes inside double-quoted scalars, so a pattern
        containing \\w or \\b is a parse error the moment the file is read
        back. Single-quoted YAML takes the string literally; the only escaping
        needed is doubling an internal quote.
        """
        s = str(v)
        if re.search(r"[:#{}\[\],&*?|>'\"%@`\\]|^\s|\s$", s):
            return "'" + s.replace("'", "''") + "'"
        return s

    dealbreakers = answers.get("dealbreakers") or {}
    db_block = "\n".join(
        f"  - name: {name}\n    pattern: {_q(pat)}\n    hard: true"
        for name, pat in dealbreakers.items()
    ) or "  []"

    extra = answers.get("extra_sources") or []
    extra_block = "\n".join(
        f"    - company: {_q(s.get('company'))}\n      url: {_q(s.get('url'))}"
        f"\n      platform: {s.get('platform','')}" for s in extra
    )
    # An empty list has to go on the `extra:` line itself. Written as a
    # separate `    []` line it is still valid YAML, but `discover --add`
    # appended a sequence underneath it and produced a file that no command
    # could load. The wizard is the documented first step, so this broke the
    # documented second step every time.
    extra_key = f"  extra:\n{extra_block}" if extra_block else "  extra: []"

    # Keys, written only when there is one. An empty `reed_api_key:` line is
    # not harmless: it reads as a configured source that is failing rather
    # than one nobody has turned on.
    key_lines = []
    for _label, _url, _why, _fields in API_KEY_SOURCES:
        for _field, _ in _fields:
            if answers.get(_field):
                key_lines.append(f"  {_field}: {_q(answers[_field])}")
    keys_block = ("\n  # Keys you gave at setup. This file holds a credential,"
                  "\n  # so keep it out of version control.\n"
                  + "\n".join(key_lines)) if key_lines else ""

    cvq = _q(answers.get("cv_path") or "")
    body = f"""# job-radar config
# Everything the tool does is decided here. Edit freely; re-running
# `job-radar setup` rewrites this file, comments and all.

titles:
  # Roles you want. Matched against the posting title.
  include:{ylist(answers.get('titles_include'))}
  # Titles to never show you, even if they match above.
  exclude:{ylist(answers.get('titles_exclude'))}

locations:
  countries:{ylist(answers.get('countries'))}
  remote_ok: {str(answers.get('remote_ok', True)).lower()}
  # Keep only these arrangements: remote, hybrid, office. Empty means all.
  # A posting that does not state one is always kept, and flagged, because
  # half of them do not say and "we cannot tell" is not "not remote".
  work_modes:{ylist(answers.get('work_modes'))}
  # Places you would move to. Scored lower than home, but still shown.
  relocate_to:{ylist(answers.get('relocate_to'))}
  # Countries where you would need a visa. Roles there that state they will
  # not sponsor are hidden; everywhere else the fact is only reported.
  need_sponsorship:{ylist(answers.get('need_sponsorship'))}
  # Never show roles in these places.
  exclude:{ylist(answers.get('exclude_locations'))}

cv:
  # Your current CV. Required: everything that writes a document works from
  # it, and without it the tool would be inventing your career rather than
  # tailoring it.
  path: {cvq}

salary:
  # A role whose STATED pay is below this is hidden.
  # A role with NO stated pay is always shown, marked "unconfirmed salary",
  # because most employers still do not publish a figure and hiding those
  # would throw away most of the market.
  floor: {answers.get('salary_floor') or 'null'}
  currency: {answers.get('salary_currency', 'GBP')}

# Read against the job description. A `hard` match hides the role.
dealbreakers:
{db_block}

# Which employers to watch. Empty means all of them.
sectors:{ylist(answers.get('sectors'), indent="  ")}

sources:{keys_block}
  use_bundled: {str(answers.get('use_bundled', True)).lower()}
  # Limit the bundled list to these countries. Empty means all.
  countries:{ylist(answers.get('source_countries'), indent="    ")}
  # Boards added by `job-radar discover --add`.
{extra_key}

output:
  formats: [html, json]
  dir: out

fetch:
  # Other people's servers. Keep this low.
  concurrency: {answers.get('concurrency', 16)}
  timeout: 20
  retries: 2
"""
    # Atomic. `setup` rewrites an existing config in place, so an
    # interruption here would leave the user with a half-written config.yaml
    # and no copy of the answers they had already given.
    return atomic_write_text(path, body)


# Deliberately empty. Filling these with the author's own job titles and
# calling them "defaults" is how a nurse ended up running eight NHS searches
# for "engineering manager". Titles also drive the keyword-based sources now,
# so a wrong guess here is not a mild inconvenience.
DEFAULTS = {
    "titles_include": [],
    "titles_exclude": [],
    "countries": ["UK"],
    "remote_ok": True,
    # Empty, not absent. `--defaults` builds its answers from this dict, and a
    # key that is missing here is a key the generated config does not carry,
    # which is how `need_sponsorship` and `source_countries` were silently
    # absent from every scripted setup until they were added.
    "work_modes": [],
    "relocate_to": [],
    "need_sponsorship": [],
    "exclude_locations": [],
    "salary_floor": None,
    "salary_currency": "GBP",
    # Also empty, for the same reason as the titles above. A coding-round
    # dealbreaker shipped as a default filtered a solicitor's and a marketing
    # manager's results on an engineering artefact, and the whole value of a
    # dealbreaker is that the person wrote it.
    "dealbreakers": {},
    "sectors": [],
    "source_countries": [],
    "use_bundled": True,
    "extra_sources": [],
    "concurrency": 16,
    "cv_path": "",
}


def _sources_it_will_read(config_path: Path) -> int:
    """How many sources the scan about to start will actually fetch.

    The same call `cmd_scan` makes, so the sentence announcing the scan and
    the scan's own first line cannot disagree. Returns 0 rather than raising:
    this is one sentence of a progress message, and a config that cannot be
    loaded here is about to be reported properly by the scan itself.
    """
    try:
        from .config import load as _load
        from . import sources as _src
        return len(_src.load(_load(str(config_path))))
    except Exception:
        return 0


# Where the published shard set lives. One place, so the README, the docs and
# this cannot drift apart.
SEED_URL = "https://github.com/maccydee/job-radar/releases/download/seed-latest"


def _open_board(config_path: Path) -> str:
    """Start the dashboard and point a browser at it. Returns the URL or "".

    Never raises. A dashboard that could not be started is not a reason to
    stop a setup that has just stored several hundred roles; the reader is
    told the command instead.
    """
    from . import serve as serve_mod
    home = config_path.expanduser().resolve().parent
    db = str(home / "data" / "job-radar.db")
    # The next free port, rather than a sentence explaining why there is no
    # dashboard. 8765 is busy whenever another config is already being served,
    # which on any machine that has run this twice is most of the time, and
    # the reader has just been handed several hundred roles: they should get a
    # page, not homework.
    #
    # A handful of ports, not a search: if five in a row are taken, something
    # is wrong that starting a sixth server will not fix.
    url = None
    for port in range(8765, 8770):
        try:
            url = serve_mod.open_in_background(
                db_path=db, config_path=str(config_path), port=port)
        except Exception:
            url = None
        if url:
            break
    if not url:
        # Two different reasons, and telling them apart matters. Nothing
        # started because a dashboard is ALREADY up is not a failure, and
        # "see them with `job-radar serve`" would send the reader at a command
        # that then refuses the port.
        #
        # It does not claim the running one holds THEIR roles. All that is
        # known is that something answers on the port; it may be a dashboard
        # on somebody else's database, which is a mistake this file has made
        # before and will not make by implication.
        print("\nYour roles are ready. Could not start a dashboard "
              "(ports 8765 to 8769 are all busy).")
        print("See them with `job-radar serve --port <free port>`.")
        return ""
    print(f"\nYour dashboard is open at {url}")
    print("It stays up while the scan runs and fills in underneath you.")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    return url


def seed_first(config_path: Path) -> int:
    """Fetch the published seed before the first scan. Returns roles stored.

    A new user's first scan takes over an hour, because `apply.workable.com`
    is 2,094 boards paced at 0.7 requests a second and that is fifty minutes
    whatever else happens. The seed is those passes, already fetched, and it
    lands in about thirty seconds. Setup did not mention it, so the fast path
    existed and the only people using it were the ones who had read the
    README to the end.

    It is announced rather than asked. The scan that follows takes an hour and
    downloads far more than this does; a question here would be one keypress
    guarding the cheaper half of the same operation. `--no-seed` turns it off,
    and the size is printed before anything is fetched.

    Failure is not fatal. The seed is a shortcut, and a shortcut that is
    unavailable leaves the scan behind it doing the whole job.
    """
    from . import cli

    home = config_path.expanduser().resolve().parent
    print("\nFetching the published seed first.")
    print("It holds the slow three quarters of a scan, already read, so you")
    print("have a dashboard in about a minute rather than in an hour. Only")
    print("the shards for your countries are downloaded. Your own scan runs")
    print("straight afterwards and its answer wins on every field.\n")

    class _Args:
        config = str(config_path)
        path = SEED_URL
        keep = str(home / "seed")
        db = str(home / "data" / "job-radar.db")
        dry_run = False

    try:
        rc = cli.cmd_seed_load(_Args())
    except KeyboardInterrupt:
        print("\nStopped. The scan below still does the whole job.")
        return 1
    except Exception as exc:
        # Named, not swallowed. A reader who never sees why the fast path was
        # skipped assumes it does not exist.
        print(f"\nCould not fetch the seed ({exc}).")
        print("Not a problem: the scan below reads everything anyway, it")
        print("just takes longer. You can try again later with")
        print(f"  job-radar seed load {SEED_URL}")
        return 1

    # Opened here, not after the scan's first pass.
    #
    # The scan already opens one five minutes in, which is when it first has
    # something worth looking at. With a seed there is something worth looking
    # at BEFORE the scan starts, and the alternative was a line of prose
    # telling somebody to open a second terminal and type a command, which is
    # the tool asking a person to do the tool's job.
    #
    # `open_in_background` returns None when a dashboard is already up, and
    # the scan's own attempt later says so rather than starting a second one
    # that would contend for the same database.
    _open_board(config_path)
    return rc


def _span(minutes: float) -> str:
    """How long something took, in words somebody would use."""
    if minutes < 1:
        return "in well under a minute"
    if minutes < 90:
        return f"in about {minutes:.0f} minute{'s' if round(minutes) != 1 else ''}"
    return f"in about {minutes / 60:.1f} hours"


def _roles_already_stored(config_path: Path) -> int:
    """How many roles are on the board. 0 if there is no board yet.

    Never raises: this decorates a message, and a first run must not end in a
    traceback because a count could not be read.
    """
    try:
        from . import store
        home = config_path.expanduser().resolve().parent
        db = home / "data" / "job-radar.db"
        if not db.exists():
            return 0
        con = store.connect(str(db))
        try:
            return int(con.execute("SELECT COUNT(*) FROM roles").fetchone()[0])
        finally:
            con.close()
    except Exception:
        return 0


def first_scan(config_path: Path) -> int:
    """Scan immediately after setup, and hand over both ways of using it.

    Ending setup with "run `job-radar scan` when you are ready" leaves someone
    holding a config file and no evidence any of it works. The first scan is
    also the one most likely to reveal a mistake worth fixing now: titles that
    match nothing, a sector tag with no employers behind it, a floor that hides
    everything. Doing it here means the wizard is the thing that finds those,
    while the person is still sitting in front of it.

    It is announced before it starts, because it takes a couple of minutes and
    silence looks like a hang.
    """
    from . import cli

    # The bundled list went from a few hundred boards to 17,807, so the old
    # "two or three minutes" promise was out by a factor of twenty and it was
    # the first thing a new user was told. The replacement was wrong too: it
    # said "four requests at a time" long after the default became 16, and
    # derived 40 minutes from the four. The real floor is not set by the
    # concurrency at all, it is set by the slowest host's own pacing. Workable
    # holds 2,094 of these boards and is read at 0.7 requests a second, which
    # is 50 minutes on its own however wide the pool is.
    #
    # Counted, not quoted. "17,807" was a literal in this sentence, and the
    # answer is only that for someone who set no sectors and no source
    # countries. The wizard has just walked its reader through picking both,
    # so the sectors question is a normal one to have answered -- and then
    # this line said 17,807 immediately before cmd_scan printed "Fetching
    # 13,440 sources" on the next line. Two numbers, four thousand apart, in
    # consecutive sentences, on the first thing a new user ever runs. It also
    # went stale on its own every time the list was regrown upstream.
    #
    # "sources" rather than "boards", because that is the word the very next
    # line uses ("Fetching 13,440 sources at concurrency 16"). Same number,
    # same noun, so the two lines cannot be read as describing two things.
    # The scan prints its own pass breakdown and its own estimate, derived
    # from the rates it will actually be paced at. Repeating a number here is
    # how this line came to promise forty minutes while the next line said
    # something else, so it promises nothing and lets the scan speak.
    n = _sources_it_will_read(config_path)
    reads = f"It reads {n:,} sources" if n else "It reads the bundled list"
    have = _roles_already_stored(config_path)
    print(f"\nRunning your first scan now. {reads}, in passes, fastest first.")
    print("It prints the total time before it starts, and the time for each")
    print("pass before that pass begins.")
    # Said only when the seed actually landed, and with the number, because
    # "you already have some" is not a thing anybody can act on. Without this
    # the scan announced an hour to somebody who had just been handed a
    # working dashboard and had no idea they could use it now.
    if have:
        print()
        print(f"You already have {have:,} roles from the seed, on the")
        print("dashboard that is open now. This scan refreshes those and adds")
        print("everything the seed does not carry, which is the fast half of")
        print("the sources and anything posted since it was built. The page")
        print("fills in underneath you; you do not have to wait for it.")
    print()
    print("You do not have to wait for the end: the dashboard is worth opening")
    print("after the first pass and the rest fill in behind it. Your machine")
    print("is held awake while it runs, though closing the lid will still")
    print("stop it.")
    print("Nothing is generated and nothing is sent anywhere; this only reads.\n")

    # Paths follow the config, not the working directory.
    #
    # `--db None` means "data/job-radar.db relative to wherever you happen to
    # be standing", which is right for `job-radar scan` run inside a checkout
    # and wrong here: `job-radar -c /somewhere/else/c.yaml setup` wrote its
    # roles, its output and its seen-set into the current directory's
    # database. Run from another project's checkout, a first-time user's scan
    # lands in someone else's data.
    home = config_path.expanduser().resolve().parent

    class _Args:
        config = str(config_path)
        db = str(home / "data" / "job-radar.db")
        state = str(home / "state" / "seen.json")
        out = str(home / "out")
        docs = None
        limit = 0
        dry_run = False
        # Every attribute `cmd_scan` reads has to be set here, because this
        # namespace is built by hand rather than by argparse. `--no-enrich`
        # was added to the parser and not to this class, so the first scan a
        # new user ran raised AttributeError after every board had been
        # fetched and before a single row was written. tests/
        # test_three_silent_faults.py compares the two lists so the next flag
        # added fails there instead of in a stranger's first run.
        no_enrich = False
        # False, so the first scan DOES hold the machine awake. It is
        # the longest run this tool ever does and the one most likely
        # to be started and walked away from.
        no_caffeine = False
        # False, so a brand new user gets the dashboard opened for them
        # five minutes in rather than being left at a counter for an
        # hour. This is the run where it matters most.
        no_open = False

    import time as _time
    began = _time.monotonic()
    try:
        rc = cli.cmd_scan(_Args())
    except KeyboardInterrupt:
        print("\nStopped. Run `job-radar scan` whenever you like.")
        return 0
    except Exception as e:                       # a first run must not traceback
        print(f"\nThe scan did not finish: {e}")
        print("Your config is written. Try `job-radar scan` to see the error.")
        return 0

    # An explicit ending, with what it cost and what it changed.
    #
    # The handover below reads exactly the same whether the scan took three
    # seconds or eighty minutes, so somebody who walked away came back to a
    # wall of instructions and no statement that the thing they were waiting
    # for had finished. The scan reports its own counts as it goes; what was
    # missing was the line saying it is over.
    took = (_time.monotonic() - began) / 60
    now = _roles_already_stored(config_path)
    print()
    print(f"Scan finished, {_span(took)}." + (
        f" Your board went from {have:,} roles to {now:,}."
        if have and now else f" {now:,} roles on your board." if now else ""))
    print()
    print("Two ways to use this, and they are the same data either way:")
    print()
    print("  job-radar serve      the dashboard, at http://127.0.0.1:8765")
    print("                       filter, screen, draft, and mark what you applied to")
    print()
    print("  job-radar list       the same thing as text")
    print("  job-radar list --new only what arrived since the last scan")
    print()
    print("The dashboard is optional. Everything it does has a command behind")
    print("it, so if you would rather stay in the terminal, nothing is missing.")
    print()
    print("Once you have applied to something, record it with")
    print("`job-radar applied <url>`. Settled roles stop coming back.")
    return rc


def ask_cv(existing: str = "") -> str:
    """Ask until we get a path to a file that actually exists.

    Required rather than optional: every document this tool writes is built
    from the real CV, and a missing one does not degrade the output, it
    invents it.
    """
    print("\n0. Your current CV  (required)")
    print("   Everything that drafts a CV or a cover letter works from this.")
    print("   .docx, .pdf, .md or .txt all fine. Drag the file in if easier.")
    while True:
        raw = _ask("   Path to your CV", existing)
        if not raw:
            print("   Needed, sorry: without it the tool would be writing a CV")
            print("   for someone whose record it has never seen.")
            continue
        p = Path(raw.strip().strip('"').strip("'")).expanduser()
        if p.exists() and p.is_file():
            return str(p.resolve())
        print(f"   Nothing at {p}. Check the path and try again.")


def run(path: Path, non_interactive: bool = False, cv: str | None = None,
        titles: str | None = None, scan: bool = False,
        countries: list | None = None, currency: str | None = None,
        seed: bool = True) -> int:
    """Build a config, by asking or from flags.

    `countries` and `currency` exist because `--defaults` is the only path
    that works without a terminal, and it wrote `countries: [UK]` and
    `currency: GBP` into the config of anyone who used it. There was no flag
    to say otherwise, so a scripted setup in Austin produced a UK job search
    and nothing said so. Same failure as the coding-round dealbreaker and the
    nurse running NHS searches for "engineering manager": a default that is
    one person's answer, presented as everybody's.
    """
    if non_interactive:
        if not cv:
            print("A CV is required. Re-run with --cv /path/to/your-cv.docx")
            return 1
        if not titles:
            print("Job titles are required. Re-run with, for example:")
            print("  --titles 'practice educator,clinical educator'")
            print("They drive more than the filter: NHS Jobs and LinkedIn are")
            print("searched with these words.")
            return 1
        p = Path(cv).expanduser()
        if not p.exists():
            print(f"No file at {p}")
            return 1
        a = dict(DEFAULTS)
        a["cv_path"] = str(p.resolve())
        a["titles_include"] = [x.strip() for x in titles.split(",") if x.strip()]
        if countries:
            a["countries"] = [str(c).strip().upper() for c in countries
                              if str(c).strip()]
        if currency:
            a["salary_currency"] = str(currency).strip().upper()
        write_config(path, a)
        print(f"Wrote a default config to {path}.")
        if scan:
            if seed:
                seed_first(path)
            return first_scan(path)
        if seed:
            seed_first(path)
        print("Edit it, then run `job-radar scan` for everything else.")
        return 0

    if not sys.stdin.isatty():
        print("`job-radar setup` asks questions, so it needs a terminal.")
        print("Piped or redirected input cannot answer them. For scripts:")
        print("  job-radar setup --defaults --cv /path/to/cv.docx \\")
        print("      --titles 'engineering manager,head of engineering'")
        return 1

    print("\njob-radar setup\n" + "-" * 40)
    print("A few questions. Everything is editable afterwards.\n")
    a = dict(DEFAULTS)
    a["cv_path"] = ask_cv()

    # 1. titles
    print("1. What roles are you looking for?")
    print("   Not sure? Paste your CV instead and press Ctrl-D on a blank line.")
    first = _ask("   Job titles (or press enter to read them from your CV)", "")
    if not first or first.lower() == "cv":
        # It asked for a path at step 0, validated it, then asked you to paste
        # the same document. Read the file.
        text = ""
        cv = Path(a.get("cv_path") or "")
        if cv.exists():
            try:
                if cv.suffix.lower() == ".docx":
                    import sys as _s
                    _s.path.insert(0, str(Path(__file__).resolve().parent.parent))
                    from jobradar.runner import docx_to_text
                    text = docx_to_text(cv)
                else:
                    text = cv.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                text = ""
        if not text:
            print("   Paste your CV, then Ctrl-D:")
            try:
                text = "".join(iter(input, "\x00"))
            except EOFError:
                text = ""
        guessed = titles_from_cv(text)
        if guessed:
            print(f"   Found these titles in your CV: {', '.join(guessed)}")
            print("   These are a starting point, not advice. Edit them.")
            a["titles_include"] = _ask_list("   Use which", guessed[:6])
        else:
            a["titles_include"] = _ask_list("   Could not read any. Job titles",
                                            DEFAULTS["titles_include"])
    elif first:
        a["titles_include"] = [x.strip() for x in first.split(",") if x.strip()]
    while not a["titles_include"]:
        print("   At least one is needed: these words are what NHS Jobs and")
        print("   LinkedIn are searched with, not just what gets filtered.")
        a["titles_include"] = _ask_list("   Job titles", [])
    a["titles_exclude"] = _ask_list("   Titles to never show", [])

    # 2. location
    print("\n2. Where?")
    a["countries"] = _ask_list("   Country codes you live in / can work in", ["UK"])
    a["remote_ok"] = _ask_yn("   Include fully remote roles", True)
    # Asked separately because the question above is a boolean answering a
    # three-way question: yes shows remote AND everything else, no hides
    # remote, and neither says "hide anything that is not remote". A
    # remote-only reader had no way to express the one thing their search is
    # about.
    if a["remote_ok"] and _ask_yn("   Remote ONLY (hide office and hybrid)",
                                  False):
        a["work_modes"] = ["remote"]
        print("   Postings that do not state an arrangement are still shown, "
              "and flagged.")
    a["relocate_to"] = _ask_list("   Countries you would relocate to", [])
    # Never asked, and never written, though config.example.yaml documents it.
    # Somebody who needs a visa for the places they are searching had no way
    # to say so short of hand-editing the file, and the roles that will not
    # sponsor them look exactly like the roles that will.
    ask_sponsor = [c for c in
                   list(a.get("countries") or []) + list(a["relocate_to"])]
    if ask_sponsor:
        print("   Sponsorship: roles stating they will not sponsor are "
              "flagged, and hidden for the countries you name here.")
        a["need_sponsorship"] = _ask_list(
            "   Countries where you would need visa sponsorship", [])
    a["exclude_locations"] = _ask_list("   Places to always exclude", [])

    # Asked because it is the one setting that changes how long a scan takes,
    # and the wizard never mentioned it. A Dutch user's first run reads all
    # 17,811 sources for about seventy-seven minutes when 160 are tagged NL.
    # Boards tagged for no country, and multinationals, are kept whatever is
    # answered here, so narrowing it cannot hide an employer that has a
    # vacancy where you are.
    print("   Most boards are not tagged with a country, and those are always")
    print("   read. Naming countries here only skips boards tagged for")
    print("   somewhere else, which makes a scan shorter.")
    a["source_countries"] = _ask_list(
        "   Only read boards tagged for these countries (blank for all)", [])

    # 3. salary
    print("\n3. Salary")
    print("   Roles with a stated figure below this are hidden.")
    print("   Roles with no stated figure are always shown and marked.")
    # Read with the same parser the config file uses, and re-asked when it
    # cannot be read.
    #
    # This stripped every non-digit and kept whatever was left, so "40 LPA"
    # became a floor of 40, "70k" became 70 and "4 million" became 4. A floor
    # of 40 hides nothing, so the filter was off and the config said it was
    # on. `config._num` already reads "70k" correctly and already refuses
    # "40 LPA" with a sentence saying how to write it, which made the
    # interactive path strictly worse than editing the file by hand.
    from .config import ConfigError, _num
    while True:
        floor = _ask("   Minimum acceptable (blank for none)", "")
        if not floor.strip():
            a["salary_floor"] = None
            break
        try:
            a["salary_floor"] = int(_num(floor, "salary.floor"))
            break
        except (ConfigError, TypeError, ValueError) as exc:
            print(f"   {exc}")
    a["salary_currency"] = _ask("   Currency", "GBP").upper()

    # 4. dealbreakers
    print("\n4. Dealbreakers. A match in the job description hides the role.")
    chosen = {}
    for name, pat in COMMON_DEALBREAKERS.items():
        # Every one defaults to no, including this one. `DEFAULTS` above says
        # why in full and the reason applies here twice over: a dealbreaker
        # shipped on by default is one the person did not write, and this one
        # is `hard`, so it hides roles silently rather than flagging them.
        # "coding round" defaulted to yes here long after the same mistake was
        # taken out of `--defaults`, so the fix reached the scripted path and
        # never reached the wizard almost everybody actually runs. Pressing
        # enter through the questions -- the most ordinary thing a first-time
        # user does -- wrote a hard pattern matching "technical assessment"
        # and "take home" into a project manager's config, and every posting
        # it hit disappeared without a line saying so.
        if _ask_yn(f"   Hide roles mentioning {name}", False):
            chosen[name] = pat
    own = _ask_list("   Anything else (plain words are fine)", [])
    for w in own:
        chosen[w] = _word_pattern(w)
    a["dealbreakers"] = chosen

    # 5. sectors
    print("\n5. Sectors. Blank means all of them.")
    print(f"   Options: {', '.join(SECTORS)}")
    a["sectors"] = _ask_list("   Sectors", [])

    # 6. specific companies
    print("\n6. Any companies you specifically want watched?")
    print("   Give names or careers page URLs. I will find their job board.")
    wanted = _ask_list("   Companies", [])
    if wanted:
        from .discover import discover as run_discover
        for w in wanted:
            print(f"   looking for {w}...")
            found = [f for f in run_discover(w) if f.live_jobs > 0 and f.identity != "mismatch"]
            if found:
                f = found[0]
                print(f"     found {f.platform}, {f.live_jobs} live roles")
                a["extra_sources"].append(f.to_source().to_dict())
            else:
                print("     not found. Add the careers URL later with "
                      "`job-radar discover <url> --add`.")

    # 8. keys for the sources that need one
    #
    # Two of the adapters in this tool are keyed, and until now nothing in the
    # documented first step ever mentioned them, so the only way to find out
    # was to add a source by hand and read the error. Reed is the one that
    # matters for UK contract work: measured on 6 September 2026, "engineering
    # manager" returns 418 contract listings there, against 19 across all
    # 17,810 unkeyed boards.
    #
    # Asked, never guessed at, and never stored anywhere but this file, which
    # is gitignored. If the answer is blank the tool works exactly as before.
    print("\n7. API keys  (optional, skip with enter)")
    print("   Two sources need a free key. Everything else works without one.")
    for label, url, why, fields in API_KEY_SOURCES:
        print(f"\n   {label}: {why}")
        print(f"   Free, no card: {url}")
        got = {}
        for field, prompt in fields:
            v = _ask(f"   {label} {prompt} (enter to skip)", "").strip()
            if not v:
                break
            got[field] = v
        # All of them or none. A half-entered credential is a 401 later whose
        # message cannot say which half is missing.
        if len(got) == len(fields):
            a.update(got)
        elif got:
            print(f"   Skipping {label}: it needs all "
                  f"{len(fields)} values to work.")

    # 8. politeness
    print("\n8. Fetch settings")
    a["concurrency"] = int(
        _ask("   How many different boards to read at once "
             "(each host is paced separately, so 16 is kind)", "16") or 16)

    write_config(path, a)
    print(f"\nWrote {path}")
    if seed:
        seed_first(path)
    return first_scan(path)
