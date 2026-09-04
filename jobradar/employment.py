"""Permanent, contract, or the posting did not say.

Contract and interim work is a different market with different money, a
different notice period and a different reason to take it, and until this
module existed the board could not tell you which one you were looking at. A
six month fixed-term Engineering Manager and a permanent one rendered as the
same row.

The whole of the difficulty is in one word. "Contract" is ordinary English in
a software advert and almost none of its uses mean the employment type. Every
line below is a real description from this database, and every one of them is
a permanent job:

    "consistently ranked among the top 400 Contractors list"
    "enterprise contracts, reseller or partner billing"
    "we write contracts before logic, test against real systems"
    "defining extension contracts, managing inbound requests"
    "Federal Government contract labor categories"

A first attempt read those words anywhere in the posting and classified 773 of
5,474 roles as contract work. Sampling them found the true figure was around
20: the word "contract", "contractor" or "contracts" in a description is
essentially never about the reader's employment type. The same went for
"temporary", which in US postings is benefits boilerplate ("Temporary
employees are eligible for paid sick time"), for "per day", which is
throughput ("3 trillion events per day"), for "interim" ("in the interim",
"interim top secret clearance"), for "fractional" ("fractional GPUs") and for
"statement of work", which is defence and agency vocabulary.

So the rule this module is built on: **the loose words are read from the title
only.** A description earns a contract classification solely through phrases
with no innocent reading left in them, and each one below was checked against
the whole corpus before it was allowed in. `FTC` is the sharpest example. In a
title it is always a fixed-term contract; in a description every single
occurrence was the Federal Trade Commission, in the anti-recruitment-fraud
paragraph that links to consumer.ftc.gov.

Three values, and the third is not a synonym for the first. `unstated` means
the advert did not say, which is the overwhelming majority of postings, and it
must never be rendered or filtered as "permanent". A reader hunting contract
work who is shown only the rows proven permanent has had the entire unstated
middle of the market hidden from them, and hiding looks exactly like absence.
"""

from __future__ import annotations

import re

PERMANENT = "permanent"
CONTRACT = "contract"
UNSTATED = "unstated"

VALUES = (PERMANENT, CONTRACT, UNSTATED)


# ---------------------------------------------------------------------------
# Decisive markers. Safe to read anywhere in the posting.
#
# Each was run against all 5,474 stored descriptions and its hits read before
# it was added here. Anything that produced a false positive is either
# tightened below or is not here at all: bare "fixed-term", bare "FTC", bare
# "per day", "statement of work", "interim" and "fractional" were all tried
# and all failed on real text.
# ---------------------------------------------------------------------------
_DECISIVE = re.compile(
    # UK off-payroll tax law. It applies to nothing except contract work, and
    # every occurrence in the corpus, title or description, was genuine.
    r"\bir\s?35\b"

    # "Fixed Term Contract", "on a fixed-term basis". The bare hyphenated word
    # is not enough: one posting listed "(INTERNS, APPRENTICES, FIXED-TERM..)"
    # while describing somebody else's terms.
    r"|\bfixed[\s-]?term\s+(?:contract|appointment|role|position|basis|employment)\b"
    r"|\bon a fixed[\s-]?term\b"

    # A duration attached to the engagement.
    r"|\b(?:three|six|nine|twelve|eighteen|twenty[\s-]?four|3|6|9|12|18|24)"
    r"[\s-]?months?[\s-]?(?:contract|assignment|engagement|ftc|fixed[\s-]?term)\b"
    r"|\bcontract (?:length|duration)\b"

    # A day rate with money next to it. "Rate: Up to GBP750/day",
    # "GBP550/day". The bare phrase "per day" is throughput and was dropped.
    r"|\b(?:day|daily)\s?rate\b"
    r"|[£$€]\s?\d[\d,.]*\s*(?:k\b)?\s*(?:per\s+day|/\s?day|a day|p\.?d\.?\b)"

    # The employer answering the question in a field.
    r"|\b(?:contract|employment|engagement)\s*type\s*:?\s*"
    r"(?:contract|fixed|temporary|interim|freelance|b2b)"

    # The advert calling this role a contract in so many words. The noun
    # matters: "contract position" is about the reader, while a bare
    # "contract" is about a customer, a defence programme or a test suite.
    # Six in the corpus, all genuine. Deliberately NOT "independent
    # contractor", which is 20 hits of which 19 are the privacy-policy
    # sentence "evaluate your application for employment or an independent
    # contractor role" and one is a real engagement.
    r"|\bcontract\s+(?:position|role|basis|assignment|opportunit\w*|"
    r"engagement|hire|placement)\b"
    r"|\bengagement\s*:?\s*independent contractor\b"
    r"|\brolling contract\b|\bumbrella (?:company|payroll)\b"

    r"|\bmaternity cover\b|\bpaternity cover\b|\bparental leave cover\b"
    r"|\bsecondment\b"

    # FTC next to a duration is unambiguous even in a description. FTC on its
    # own is the Federal Trade Commission and is handled in the title only.
    r"|\bmonths?\s+ftc\b|\bftc\b(?=\s*[\)\],])",
    re.I)

# ---------------------------------------------------------------------------
# Title-only markers.
#
# These are reliable in a title, where the employer is naming the job, and
# worthless in a description, where they are ordinary English. Nothing here is
# ever read from the body of a posting.
# ---------------------------------------------------------------------------
_TITLE_ONLY = re.compile(
    r"\bftc\b"
    r"|\binterim\b"
    r"|\bfractional\b"
    r"|\bfixed[\s-]?term\b"
    r"|\bcontract(?:or)?\b"
    r"|\bfreelance(?:r)?\b"
    r"|\btemp(?:orary)?\b"
    r"|\bday[\s-]?rate\b",
    re.I)

# Even in a title these are not the employment type. "Engineering Manager,
# Contracts Platform" is a permanent job about contracts, and "Manager,
# Contract Testing" is a permanent job about Pact. A plural is the giveaway:
# an employer offering contract work writes "Contract", never "Contracts".
_TITLE_INNOCENT = re.compile(
    r"\bcontracts\b"
    r"|smart contract"
    r"|contract test|contract[\s-]?first|api contract"
    r"|contract (?:manage|negotiat|law|admin|analyst|lifecycle)"
    r"|(?:vendor|customer|commercial|government|defen[cs]e) contract",
    re.I)

# ---------------------------------------------------------------------------
# Permanent markers.
#
# "Permanent" is itself a trap in a description: the commonest use by a wide
# margin is immigration boilerplate ("US citizen or permanent resident"),
# which says nothing about the role. Those are struck out first.
# ---------------------------------------------------------------------------
_NOT_ABOUT_THE_ROLE = re.compile(
    r"permanent resident\w*"
    r"|permanent (?:work )?(?:authoris|authoriz)\w*"
    # "legally authorized to work in the United States on a permanent basis"
    # is about the reader's immigration status, not the employer's offer.
    r"|(?:authoris|authoriz)\w*[^.]{0,60}?on a permanent basis"
    r"|permanent (?:record|damage|marker|magnet|storage|deletion)"
    # "may convert to permanent" says the role is NOT permanent today, which
    # is the opposite of what the word alone suggests.
    r"|(?:convert|move|transition|going|leading)\s+(?:in)?to\s+(?:a\s+)?permanent"
    r"|with a view to (?:a )?permanent"
    r"|potential(?:ly)?\s+(?:to go )?permanent"
    r"|possibility of (?:a )?permanent"
    r"|option to (?:go|become) permanent",
    re.I)

# The bare word is not enough even after the strike-outs above. "identify and
# drive temporary and permanent equipment repairs" is a maintenance role
# describing repairs, and calling it permanent employment is a mislabel with a
# cost: a reader filtering for contract work plus the unstated middle would
# lose a role this module had no business having an opinion about. So
# "permanent" only counts next to a word that makes it about the job.
#
# Being conservative here is close to free. A missed permanent falls to
# `unstated`, which is honest, and `unstated` is shown to everybody.
_PERMANENT = re.compile(
    r"\bpermanent\s+(?:position|role|contract|employee|employment|hire|"
    r"placement|staff|basis|appointment|full[\s-]?time|opportunity)\b"
    r"|\b(?:this is a|is a|offering a|on a|for a)\s+permanent\b"
    r"|\b(?:employment|contract|position|role)\s*type\s*:?\s*"
    r"(?:permanent|full[\s-]?time)\b"
    r"|\bduration of assignment\s*:?\s*permanent\b"
    r"|\bpermanent\s*[,/]\s*full[\s-]?time\b",
    re.I)


def _title_hit(title: str) -> str:
    """The first title word meaning contract work, or "".

    A hit is discarded when the surrounding title explains it away, which is
    checked against the whole title rather than a window: titles are short
    enough that the whole string is the context.
    """
    if _TITLE_INNOCENT.search(title):
        return ""
    m = _TITLE_ONLY.search(title)
    return m.group(0).strip() if m else ""


def classify(title: str, description: str = "") -> tuple[str, str]:
    """Return (value, evidence).

    `evidence` is the phrase the answer was read from, so a wrong call can be
    seen rather than merely suspected. It is empty for `unstated`, which has
    no evidence by definition: nothing was said.

    The title outranks the description throughout. An employer who writes
    "Engineering Manager (12 Month FTC)" has answered the question in the one
    place they were certain it would be read, and a benefits paragraph
    mentioning permanent staff later on does not undo it.
    """
    title = title or ""
    desc = description or ""

    # 1. The title, which is where an employer says this if they say it at all.
    m = _DECISIVE.search(title)
    if m:
        return CONTRACT, m.group(0).strip()
    hit = _title_hit(title)
    if hit:
        return CONTRACT, hit
    if _PERMANENT.search(_NOT_ABOUT_THE_ROLE.sub(" ", title)):
        return PERMANENT, "permanent"

    # 2. The description, and only through phrases that survived the corpus.
    m = _DECISIVE.search(desc)
    if m:
        return CONTRACT, m.group(0).strip()
    if _PERMANENT.search(_NOT_ABOUT_THE_ROLE.sub(" ", desc)):
        return PERMANENT, "permanent"

    # 3. Nobody said. Not permanent: unsaid.
    return UNSTATED, ""


def flag(value: str, evidence: str) -> str:
    """The line shown on the role, or "" when there is nothing to say.

    Only contract roles get one. A flag on every permanent role would be a
    caption on most of the board, and `unstated` has nothing to report beyond
    an absence the dashboard already shows as its own facet.
    """
    if value != CONTRACT:
        return ""
    return f"contract or interim ({evidence})" if evidence else "contract or interim"


# ---------------------------------------------------------------------------
# What the employer said, in the platform's own field.
#
# Six of the platforms in the bundled list carry an explicit employment-type
# field and nothing read any of them, so 7,927 of 17,811 boards had the answer
# sitting in the payload while this module guessed at it from prose. Measured
# on 4 September 2026 by fetching one live board per platform:
#
#   ashby            employmentType          2,607 boards   FullTime
#   workable         employment_type         2,094 boards   Contract, Full-time
#   personio         employmentType          1,258 boards   permanent, intern
#   recruitee        employment_type_code      993 boards   fulltime_permanent
#   smartrecruiters  typeOfEmployment          910 boards   permanent, intern
#   lever            categories.commitment      65 boards   Full time, Part time
#
# The first Workable board tried had 22 postings and 10 of them were typed
# "Contract". Every one was stored as `unstated`.
#
# THE TRAP, and it is the reason this is a table rather than a one-liner:
# these fields answer two different questions with one value. "Full-time" is a
# SCHEDULE. A six month contract can be full time, and on the platforms whose
# vocabulary mixes the two there is no way to tell a full-time permanent role
# from a full-time contract that was typed by its hours. So a schedule word
# resolves to `unstated`, not to `permanent`.
#
# That is deliberately lossy in the safe direction. A missed `permanent` costs
# nothing, because `unstated` is shown to everybody and permanent is the label
# nobody is hunting for. A wrong `permanent` on a contract role hides it from
# the one reader who wanted it, and hidden is indistinguishable from absent.
# ---------------------------------------------------------------------------
_PLATFORM_VALUES = {
    # Said outright. Nothing here has a second reading.
    "contract": CONTRACT,
    "contractor": CONTRACT,
    "temporary": CONTRACT,
    "temp": CONTRACT,
    "freelance": CONTRACT,
    "fixed_term": CONTRACT,
    "fixedterm": CONTRACT,
    "fixed_term_contract": CONTRACT,
    "interim": CONTRACT,
    "seasonal": CONTRACT,
    "contract_to_hire": CONTRACT,

    "permanent": PERMANENT,
    "fulltime_permanent": PERMANENT,
    "parttime_permanent": PERMANENT,
    "regular": PERMANENT,

    # Schedules, not contract types. See the paragraph above: these are the
    # values that must NOT become `permanent`.
    "fulltime": UNSTATED,
    "full_time": UNSTATED,
    "parttime": UNSTATED,
    "part_time": UNSTATED,

    # Real fixed terms, and still not what somebody hunting contract work
    # means. Putting an internship in the contract facet would fill it with
    # roles nobody searching it wants, which is its own kind of wrong answer.
    "intern": UNSTATED,
    "internship": UNSTATED,
    "trainee": UNSTATED,
    "apprentice": UNSTATED,
    "apprenticeship": UNSTATED,
    "working_student": UNSTATED,
    "volunteer": UNSTATED,
    "other": UNSTATED,
}

# Lever's `commitment` is free text an employer types, so it arrives as
# "Full time days" and "Full time, Part time, and Weekend shifts available."
# as well as the tidy values. Substring, not equality, and contract wins on a
# tie because a field mentioning contract at all is the employer raising it.
_PLATFORM_SUBSTRINGS = (
    ("fixed term", CONTRACT), ("fixed-term", CONTRACT),
    ("contract", CONTRACT), ("freelance", CONTRACT),
    ("temporary", CONTRACT), ("interim", CONTRACT),
    ("permanent", PERMANENT),
)


def from_platform(value) -> str:
    """The employer's own employment-type value, normalised.

    Returns `unstated` for anything unrecognised, which is the honest answer
    for a vocabulary this table has not seen: a platform that adds a value
    must not have it silently read as one of the two that matter.
    """
    if isinstance(value, dict):
        # SmartRecruiters sends {"id": "permanent", "label": "Full-time"}.
        # The id is the machine value and the label is display text that has
        # already been through a translation table, so the id is what to read.
        value = value.get("id") or value.get("label") or ""
    if not isinstance(value, str):
        return UNSTATED
    # "FullTime" (Ashby) and "Full-time" (Workable) and "full time" (Lever)
    # and "fulltime_permanent" (Recruitee) are four spellings of two words.
    # Split the camel case first, then flatten every separator to one.
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value.strip())
    key = re.sub(r"[\s\-_]+", "_", spaced.lower())
    if key in _PLATFORM_VALUES:
        return _PLATFORM_VALUES[key]
    low = value.lower()
    for needle, verdict in _PLATFORM_SUBSTRINGS:
        if needle in low:
            return verdict
    return UNSTATED
