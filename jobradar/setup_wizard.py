"""Interactive config builder.

Deliberately a plain CLI wizard rather than something clever, because the
people this tool is meant to widen to are exactly the ones who will not
hand-edit YAML. The `/job-radar setup` skill wraps this same writer with a
conversational layer; both end at `write_config`, so the two front doors
cannot drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

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


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
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

    cvq = _q(answers.get("cv_path") or "")
    body = f"""# job-radar config
# Everything the tool does is decided here. Edit freely; re-running
# `job-radar setup` updates this file rather than replacing it.

titles:
  # Roles you want. Matched against the posting title.
  include:{ylist(answers.get('titles_include'))}
  # Titles to never show you, even if they match above.
  exclude:{ylist(answers.get('titles_exclude'))}

locations:
  countries:{ylist(answers.get('countries'))}
  remote_ok: {str(answers.get('remote_ok', True)).lower()}
  # Places you would move to. Scored lower than home, but still shown.
  relocate_to:{ylist(answers.get('relocate_to'))}
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

sources:
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
  concurrency: {answers.get('concurrency', 4)}
  timeout: 20
  retries: 2
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# Deliberately empty. Filling these with the author's own job titles and
# calling them "defaults" is how a nurse ended up running eight NHS searches
# for "engineering manager". Titles also drive the keyword-based sources now,
# so a wrong guess here is not a mild inconvenience.
DEFAULTS = {
    "titles_include": [],
    "titles_exclude": [],
    "countries": ["UK"],
    "remote_ok": True,
    "relocate_to": [],
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
    "concurrency": 4,
    "cv_path": "",
}


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

    print("\nRunning your first scan now. It reads about three hundred job")
    print("boards at four requests at a time, so give it two or three minutes.")
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

    try:
        rc = cli.cmd_scan(_Args())
    except KeyboardInterrupt:
        print("\nStopped. Run `job-radar scan` whenever you like.")
        return 0
    except Exception as e:                       # a first run must not traceback
        print(f"\nThe scan did not finish: {e}")
        print("Your config is written. Try `job-radar scan` to see the error.")
        return 0

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
        titles: str | None = None, scan: bool = False) -> int:
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
        write_config(path, a)
        print(f"Wrote a default config to {path}.")
        if scan:
            return first_scan(path)
        print("Edit it, then run `job-radar scan`.")
        return 0

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
    a["relocate_to"] = _ask_list("   Countries you would relocate to", [])
    a["exclude_locations"] = _ask_list("   Places to always exclude", [])

    # 3. salary
    print("\n3. Salary")
    print("   Roles with a stated figure below this are hidden.")
    print("   Roles with no stated figure are always shown and marked.")
    floor = _ask("   Minimum acceptable (blank for none)", "")
    a["salary_floor"] = int(re.sub(r"[^\d]", "", floor)) if re.search(r"\d", floor) else None
    a["salary_currency"] = _ask("   Currency", "GBP").upper()

    # 4. dealbreakers
    print("\n4. Dealbreakers. A match in the job description hides the role.")
    chosen = {}
    for name, pat in COMMON_DEALBREAKERS.items():
        if _ask_yn(f"   Hide roles mentioning {name}", name == "coding round"):
            chosen[name] = pat
    own = _ask_list("   Anything else (plain words are fine)", [])
    for w in own:
        chosen[w] = re.escape(w)
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

    # 7. politeness
    print("\n7. Fetch settings")
    a["concurrency"] = int(_ask("   Parallel requests (be kind, 4 is plenty)", "4") or 4)

    write_config(path, a)
    print(f"\nWrote {path}")
    return first_scan(path)
