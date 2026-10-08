"""What an applicant tracking system will see: the text layer, not the page.

An ATS does not look at a PDF, it reads the text inside it. A CV can look
perfect and carry an email address drawn as an icon, a phone number set in an
icon font, or text outlined into shapes, and every one of those reads as a
blank to the parser. These are pure functions over the extracted text so they
can be tested without a PDF; `cvcheck` runs them on a real one.

Keyword coverage is a mirror held up, not a target. A term the posting uses
and the CV does not is shown as a gap and nothing more: adding a word to a CV
that its author cannot back up is the opposite of what this tool is for, so
the check never fails on a gap and never suggests filling one.
"""

from __future__ import annotations

import re

# Private-use code points are where icon fonts put their pictures, and U+FFFD is
# what a decoder writes for a byte it could not read. Both are glyphs a parser
# cannot turn into a word.
_PRIVATE = re.compile("[-�]")
GARBLED_SHARE = 0.08


def garbled(text: str) -> tuple[bool, str]:
    if not text:
        return True, "no text"
    bad = len(_PRIVATE.findall(text)) + sum(
        1 for c in text if ord(c) < 32 and c not in "\n\r\t")
    share = bad / max(1, len(text))
    return share > GARBLED_SHARE, f"{share:.0%} of characters are private-use or control"


def contact_check(text: str, email: str | None) -> tuple[bool, str]:
    if not email:
        return False, "no --contact-email given"       # caller maps this to UNMEASURED
    found = email.lower() in text.lower()
    return found, ("email found as literal text" if found
                   else "email not found as literal text")


# Words that appear in every posting and say nothing about this one. Kept short
# on purpose: a long list starts removing real skills ("support", "management").
_STOP = set("""
a about above across after all also am an and any are as at be been being both but by
can could do does each etc for from get give had has have having how i if in including
into is it its join just like looking may more most must need needs new no not of on one
only or other our out over own per role roles so some such than that the their them then
there these they this those through to too under up us use using very via was we well
were what when where which while who will with within without would you your
ability able across strong skills skill experience experienced years year work working
works team teams company opportunity candidate candidates requirements requirement
responsibilities responsibility required preferred essential desirable plus excellent
good great high including ideal knowledge understanding proven demonstrated successful
related relevant similar day days time role join benefits salary apply application
""".split())

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[+#]+|(?:[.\-/][A-Za-z0-9]+)*)")
_SENTENCE_START = re.compile(r"(?:^|[.!?:\n•*\-])\s*$")


def jd_terms(jd: str | None, limit: int = 40) -> list[str]:
    """Distinctive terms in the posting, most telling first.

    Capitalised words that are not merely starting a sentence, all-capitals
    (AWS, CI/CD), and anything with a symbol in it (C++, Node.js) come first,
    because that is what names a tool. Then the words the posting repeats.
    Everything is minus a stop list, and each term is returned once, spelled as
    the posting first spelled it.
    """
    if not jd:
        return []
    seen: dict[str, dict] = {}
    for i, m in enumerate(_TOKEN.finditer(jd)):
        tok = m.group(0)
        key = tok.lower()
        if key in _STOP or len(tok) < 2:
            continue
        if len(tok) == 2 and not tok.isupper():
            continue                          # "Go" or "we": too short to be told apart
        start = bool(_SENTENCE_START.search(jd[max(0, m.start() - 3): m.start()]))
        distinctive = (tok.isupper() and len(tok) >= 2
                       or any(c in tok for c in "+#./-0123456789")
                       or (tok[0].isupper() and not start))
        rec = seen.setdefault(key, {"term": tok, "n": 0, "d": False, "first": i})
        rec["n"] += 1
        rec["d"] = rec["d"] or distinctive
    ranked = sorted(seen.values(), key=lambda r: (not r["d"], -r["n"], r["first"]))
    return [r["term"] for r in ranked[:limit]]


def _present(term: str, text: str) -> bool:
    # Whole tokens, so "Go" is not found in "going" and "Rust" not in "trust".
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text,
                     re.I) is not None


def keyword_coverage(cv: str, jd: str) -> tuple[list[str], list[str]]:
    terms = jd_terms(jd)
    return ([t for t in terms if _present(t, cv)],
            [t for t in terms if not _present(t, cv)])
