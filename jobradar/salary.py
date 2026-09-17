"""Salary parsing and the one filter rule that matters.

The rule, in full:

  * A posting with a **stated** figure below your floor is dropped. You know
    it is too low, so it is noise.
  * A posting with **no** stated figure is kept and labelled "unconfirmed
    salary". Most of the market does not publish pay; filtering those out
    silently bins the majority of real roles.

Only `confirmed=True` salaries can ever disqualify a posting.
"""

from __future__ import annotations

import re

from .models import Salary

# What a bare number most likely means, given where the job is.
#
# `enrich` used to pass the READER's configured floor currency as the default
# for any figure with no symbol on it, anywhere in the world. So an Indian
# posting reading "Annual salary: 900,000 to 1,100,000" was stored, confirmed,
# as "$900k - $1,100k" for a reader whose floor was in dollars: about 8,500
# pounds presented as most of a million, and `confirmed=True`, so it could
# clear a floor it comes nowhere near.
#
# The reader's own currency is the one thing that cannot be evidence here. The
# job's country can. Where that is unknown the answer is to say nothing: an
# unconfirmed salary is shown to the reader and labelled, and can never
# disqualify a role, which is the safe direction. A figure that carries its
# own symbol never reaches this at all.
#
# Only the countries the bundled boards actually produce. A country missing
# from here is not a bug, it is an unconfirmed salary.
CURRENCY_OF_COUNTRY = {
    "UK": "GBP", "GB": "GBP", "IE": "EUR", "US": "USD", "CA": "CAD",
    "AU": "AUD", "NZ": "NZD", "IN": "INR", "SG": "SGD", "AE": "AED",
    "JP": "JPY", "CN": "CNY", "HK": "HKD", "CH": "CHF", "SE": "SEK",
    "NO": "NOK", "DK": "DKK", "PL": "PLN", "CZ": "CZK", "BR": "BRL",
    "MX": "MXN", "ZA": "ZAR", "IL": "ILS", "TR": "TRY", "KR": "KRW",
    "PH": "PHP", "MY": "MYR", "TH": "THB", "ID": "IDR", "VN": "VND",
    "AR": "ARS", "CL": "CLP", "CO": "COP", "NG": "NGN", "KE": "KES",
    "EG": "EGP", "SA": "SAR", "QA": "QAR", "PK": "PKR", "BD": "BDT",
    "UA": "UAH", "RO": "RON", "HU": "HUF", "BG": "BGN", "RS": "RSD",
    "IS": "ISK",
}
# The euro, spelled out so the map above stays one line per fact.
for _cc in ("AT", "BE", "CY", "DE", "EE", "ES", "FI", "FR", "GR", "HR",
            "IT", "LT", "LU", "LV", "MT", "NL", "PT", "SI", "SK"):
    CURRENCY_OF_COUNTRY[_cc] = "EUR"


def currency_of_country(country: str | None) -> str | None:
    """The currency a bare number in that country probably means. None when
    we do not know, which is not a failure: it means "do not guess"."""
    if not country:
        return None
    return CURRENCY_OF_COUNTRY.get(country.strip().upper())


# What a currency mark in an advert means.
#
# The class used to be `[£$€]` and nothing else. So "S$120,000 - S$160,000"
# came back as `$120k`, confirmed: the leading S broke the RANGE match, the
# single-value pattern then found "$120,000" sitting inside it, and the
# posting was stored with the wrong currency AND with the top of its band
# silently deleted. SGD is about 0.74 USD, so that is a Singapore role priced
# a third high, on a `confirmed=True` figure, which is the only kind allowed
# to disqualify a role against a floor. C$, A$, NZ$, HK$ and R$ all did it.
#
# The rupee sign was the next one along, and it cost a whole market. INR
# floors were enabled the day this was found, and on the published seed of
# 2026-08-28 there are 101 Indian adverts writing pay with a "\u20b9" and not one
# of them parsed: Litmos state "Salary Range: \u20b922,00,000 to \u20b928,00,000" and
# came back unconfirmed, so the figure could neither hide the role nor
# clear the floor. Worse in the range case, because "\u20b9" is not whitespace:
# Airbnb's "\u20b93,080,000 \u2014 \u20b94,400,000 INR" could not match as a range at all
# and fell through to the single-value pattern on the trailing ISO code,
# which reports the TOP of the band as though it were the whole of it.
_SYMBOL_CUR = {
    "£": "GBP", "$": "USD", "€": "EUR", "\u20b9": "INR",
    "us$": "USD", "c$": "CAD", "ca$": "CAD", "a$": "AUD", "au$": "AUD",
    "nz$": "NZD", "s$": "SGD", "hk$": "HKD", "r$": "BRL",
}

# Every currency code this module can put on a Salary. Built from the two
# tables above rather than written out again, because `config.VALID_CURRENCIES`
# is built from THIS and a hand-kept second copy is a list that goes stale: a
# floor the parser can never match is a floor that silently stops filtering.
KNOWN_CURRENCIES = frozenset(_SYMBOL_CUR.values()) | frozenset(CURRENCY_OF_COUNTRY.values())

# `_CUR` already mapped "gbp", "usd" and "eur", and no pattern in this file
# ever accepted a letter, so those three entries were dead code and every
# "USD 150,000", "SGD 120,000" and "INR 4,000,000" came back unconfirmed. US
# and Asian boards write the code far more often than they write a symbol.
_CUR = {**_SYMBOL_CUR, **{c.lower(): c for c in KNOWN_CURRENCIES}}

# An ISO code is read only in CAPITALS, because these patterns are
# case-insensitive and several codes are ordinary English words: "try", "cop"
# and "sar" would each turn a sentence about effort or policing into a
# confirmed salary, and confirmed is the only kind that can delete a role.
_ISO = rf"(?=[A-Z]{{3}})(?:{'|'.join(sorted(KNOWN_CURRENCIES))})"

# A mark in FRONT of the number. The letter-led forms carry a lookbehind so a
# word that happens to end in one of those letters does not lend them to the
# currency, and the ISO form allows a trailing "$" for the "AUD$100,000"
# spelling that would otherwise have matched on its bare "$" and read as USD.
#
# The two lookaheads are load-bearing for speed, not for meaning. `parse_text`
# runs over `desc[:1500]` for EVERY posting EVERY adapter yields, not just the
# ones that survive the title gate, so this pattern is attempted at something
# like a thousand positions per posting and a scan reads hundreds of thousands
# of postings. Written as a bare alternation the engine tries forty-odd
# literals at each of those positions: measured on a 1,500 character
# description that states no pay, which is the common case, `parse_text` went
# from 0.21ms to 0.92ms. `(?-i:(?=[A-Z£$€]))` throws out every position that
# is not a capital or a symbol in one comparison, and `(?=[A-Z]{3})` inside
# `_ISO` throws out most of the rest, which brings it back to 0.36ms.
#
# The price of the outer gate is that the country-dollar marks are read only
# in capitals, which is how anybody writes S$ or HK$ anyway, and the ISO codes
# were capitals-only already for a different reason.
_CUR_PRE = (rf"(?-i:(?=[A-Z£$€\u20b9]))"
            rf"(?:(?<![A-Za-z])(?:(?-i:{_ISO})\$?(?![A-Za-z])|"
            rf"US\$|CA\$|AU\$|NZ\$|HK\$|S\$|C\$|A\$|R\$)|[£$€\u20b9])")

# A code AFTER the number. "150,000 USD" is as common as "USD 150,000" and
# neither was read.
_CUR_SUF = rf"(?<![A-Za-z])(?-i:{_ISO})(?![A-Za-z])"

# 120,000 / 120000 / 120k / 120.5k / 189.6K, and the European spelling
# 60.000 / 1.234.567 / 1.500,50.
#
# `_NUM` understood only the comma, so "€ 60.000 - € 75.000 per jaar" and
# "EUR 65.000" were unconfirmed on every Dutch and German advert, and that is
# how most of that market writes pay.
#
# The rule for a dot, and it is the whole rule: a dot is a thousands separator
# only when EXACTLY three digits follow it and no digit follows those. So
# "60.000" is sixty thousand, "60.00" is two digits and stays sixty, and "1.5"
# is one digit and stays one and a half. Nobody quotes pay to three decimal
# places, which is what makes the three-digit case have one honest reading.
#
# The Indian grouping is the third spelling, and it is the one that bites
# rather than merely going missing. India writes a lakh as "16,50,000": two
# digits, then groups of TWO, then a final group of three. Read with the
# Anglo rule the whole figure fails to match at its first digit, the scan
# walks forward, and the tail "50,000" matches on its own. So DualEntry's
# "India: \u20b916,50,000 - \u20b921,78,000 INR", which is in the published seed
# verbatim, parsed as a confirmed 78,000 INR: twenty-eight times too small,
# confirmed, and therefore deleted outright by a 4,000,000 floor. Reading a
# figure wrong is worse than not reading it, because only a confirmed figure
# is allowed to disqualify a role.
#
# It sits after the Anglo alternative and the two are disjoint, so no number
# that reads today changes: the Anglo form needs every group after the first
# to be three digits and this one needs at least one group of two, and the
# engine only reaches this branch at a position where the Anglo one failed.
_NUM = (r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"
        r"|\d{1,2}(?:,\d{2})+,\d{3}(?!\d)"
        r"|\d{1,3}(?:\.\d{3})+(?!\d)(?:,\d+)?"
        r"|\d+(?:\.\d+)?\s?[kK]\b"
        r"|\d{4,}(?:\.\d+)?")

_RANGE = re.compile(
    rf"(?P<c1>{_CUR_PRE})?\s?(?P<lo>{_NUM})\s*(?:-|–|—|to|up to)\s*"
    rf"(?P<c2>{_CUR_PRE})?\s?(?P<hi>{_NUM})(?:\s?(?P<c3>{_CUR_SUF}))?",
    re.I,
)
_SINGLE = re.compile(
    rf"(?P<c>{_CUR_PRE})\s?(?P<v>{_NUM})"
    rf"|(?P<v2>{_NUM})\s?(?P<c2>{_CUR_SUF})", re.I)


def _cur_code(tok: str | None) -> str | None:
    """The currency one mark means. "A$", "AU$" and "AUD$" are one answer."""
    t = (tok or "").strip().lower()
    if not t:
        return None
    return _CUR.get(t) or _CUR.get(t.rstrip("$"))


def _match_currency(m) -> str | None:
    """The currency named anywhere in one match, the front of it first.

    A match can carry the mark in three places now: in front of the low
    figure, in front of the high one, or as a code trailing the whole range.
    Reading only the first two is what left "150,000 - 160,000 USD" as an
    unsymbolled range needing pay context to be believed at all.
    """
    g = m.groupdict()
    for name in ("c", "c1", "c2", "c3"):
        code = _cur_code(g.get(name))
        if code:
            return code
    return None


def _match_value(m) -> str | None:
    """The single figure in a match, whichever side its currency sat on."""
    g = m.groupdict()
    return g.get("v") or g.get("v2")

# Day and hour rates are small numbers, so the annual patterns above skip them
# on purpose: a bare "600" in a job description is far more likely to be a
# headcount than a salary. Once the text says "per day", a small number is
# meaningful and these looser patterns take over.
#
# The trailing lookahead is what stops this eating a thousands separator. The
# rate patterns run FIRST whenever the block says "per day" or "per hour", and
# without the guard "$1,200 per day" matched as "$1": four digits at most, and
# the comma ends the number. That is a 264,000-a-year contract stored as one
# dollar a day, and then silently dropped by any floor at all. Refusing to
# match a number that is followed by more digits or by a comma hands
# "$1,200" back to the annual patterns above, which read the separator.
#
# The guard covers a dot separator and a "k" as well as a comma, because both
# fail the same way and both were live. "\u20ac 1.500,50 per day" matched as
# "\u20ac 1" -- the optional decimal group backtracked away and the lookahead
# had nothing to say about a dot -- and "\u00a31.5k per day" matched as
# "\u00a31.5", which is a 1,500 a day contract stored as one pound fifty and
# then dropped by any floor at all. Refusing both hands the string to the
# annual patterns, which read "1.500,50" and "1.5k" correctly and keep the
# day period that the words around them state.
_NUM_RATE = r"\d{1,4}(?:\.\d+)?(?![\d,kK]|\.\d)"
# The same currency marks as the annual patterns. A day rate is quoted in
# C$ and S$ exactly as often as a salary is, and reading "C$800 per day"
# as USD is the same wrong-currency confirmation. No trailing-code form
# here though: a rate number is four digits at most, so "2024 USD" in a
# sentence would be read as a day rate.
# The TOP of a rate band is allowed the full number pattern as well, tried
# second so nothing that reads today changes. `_NUM_RATE` refuses a number
# followed by a separator, which is right for a lone figure and wrong for the
# far end of a range: TechnologyAdvice publish "Hourly pay range \u20b9500 \u2014
# \u20b91,000 INR", the whole range failed to match on the "1,000", and
# `_SINGLE_RATE` then reported the \u20b9500 as the entire band. Half a rate is
# half an annualised figure, and the floor deletes on that.
_RANGE_RATE = re.compile(
    rf"(?P<c1>{_CUR_PRE})\s?(?P<lo>{_NUM_RATE})\s*(?:-|–|—|to)\s*"
    rf"(?P<c2>{_CUR_PRE})?\s?(?P<hi>{_NUM_RATE}|{_NUM})",
    re.I,
)
_SINGLE_RATE = re.compile(rf"(?P<c>{_CUR_PRE})\s?(?P<v>{_NUM_RATE})", re.I)

_PER_DAY = re.compile(r"\b(per|a|/)\s?day\b|\bday rate\b|\bdaily\b|\bpd\b", re.I)
_PER_HOUR = re.compile(r"\b(per|an|/)\s?h(ou)?r\b|\bhourly\b", re.I)
# Needed as a first-class answer, not just as "no rate word found". A figure
# can sit between a rate word and a year word -- "$19-$27 per hour (~$39,000 -
# $56,000 annually)" -- and whichever is NEARER is the one describing it.
# Dutch and German month words are in here because this file now reads their
# number format. "EUR 4.500 bruto per maand" was invisible before, so it was
# harmless; once it parses, a monthly figure with no month word is confirmed
# as 4,500 A YEAR and then dropped by a floor of any size at all. Teaching the
# number format without the period word would have turned an unconfirmed
# salary into a deleted role, which is the worse of the two.
# Every way an advert says "a month", because the ones it does not know are
# the ones that hurt.
#
# `_scan` deliberately refuses a figure it can see is monthly, so a spelling
# in this list is safe. A spelling MISSING from it is read as an annual
# figure and confirmed, and then the floor deletes the role: "€8.000 im
# Monat" is a 96,000 a year job stored as 8,000 and hidden with the reason
# "stated pay below floor", which states the opposite of the advert.
#
# That could not happen while European decimals were unreadable. It started
# the moment they became readable this afternoon, which is why the list has
# to cover the languages that write them.
_PER_MONTH = re.compile(
    r"\bper month\b|\ba month\b|\bmonthly\b|\bpcm\b|\bper calendar month\b|"
    r"/\s?month\b|\bper maand\b|\bp/m\b|\bpro monat\b|\bmonatlich\b"
    # Dutch, German
    r"|/\s?maand\b|\bmaandelijks\b|\bper mnd\b|/\s?mnd\b"
    r"|/\s?monat\b|\bim monat\b|\bmonatlich(?:es|er|e)?\b"
    # French, Spanish, Italian, Portuguese
    r"|\bpar mois\b|/\s?mois\b|\bmensuel(?:le)?\b"
    r"|\bal mes\b|\bpor mes\b|\bmensual(?:es)?\b"
    r"|\bal mese\b|\bmensile\b|\bao m[eê]s\b|\bmensal\b"
    # Greek, Polish, Romanian, Nordic
    r"|\u03bc\u03b7\u03bd\u03b9\u03b1\u03af\u03c9\u03c2\b|\u03c4\u03bf\u03bd \u03bc\u03ae\u03bd\u03b1\b"
    r"|\bmiesi\u0119cznie\b|\bpe lun\u0103\b|\blunar\b"
    r"|\bper m\u00e5ned\b|\bper m\u00e5nad\b|\bkuukaudessa\b"
    # The short forms, which are the commonest of all
    r"|/\s?mo\b|\bp\.m\.|\bper 4 weken\b",
    re.I)
_PER_YEAR = re.compile(
    r"\bper annum\b|\bannually\b|\bannualized\b|\bannualised\b|\bper year\b|"
    r"\ba year\b|\byearly\b|/\s?(?:year|yr|annum)\b|\bp\.?a\.?\b|"
    r"\bannual (?:base )?(?:salary|pay|compensation)\b|"
    r"\bper jaar\b|\bpro jahr\b|\bj\u00e4hrlich\b", re.I)

# How far either side of a figure a period word still describes it.
#
# `_period` used to answer from the whole chunk it was given, and the chunk
# handed to the second pass is up to 19,600 characters. So one "daily" in a
# benefits paragraph re-read a whole annual salary as a day rate: four
# postings in a 13,588-posting sample had their period set by a word more
# than 300 characters from the figure, and five Bezos Academy adverts stating
# "$19-$27 per hour" came out as $19 a DAY because `_PER_DAY` is tested
# before `_PER_HOUR` and "a day" appeared elsewhere in the body.
_PERIOD_WINDOW = 60


def _period_near(text: str, start: int, end: int) -> str | None:
    """The period named CLOSEST to a figure, or None when none is close.

    Distance decides, not pattern order. Reading day before hour turned
    "per hour" into a day rate whenever the body happened to say "a day"
    somewhere else, and a day rate is 8x an hour rate, so the annualised
    figure that the floor then compares against is wrong by that much in
    whichever direction happens to hurt.
    """
    lead = text[max(0, start - _PERIOD_WINDOW):start]
    trail = text[end:end + _PERIOD_WINDOW]
    best: tuple[int, str] | None = None
    for period, pat in (("day", _PER_DAY), ("hour", _PER_HOUR),
                        ("month", _PER_MONTH), ("year", _PER_YEAR)):
        for m in pat.finditer(lead):
            gap = len(lead) - m.end()
            if best is None or gap < best[0]:
                best = (gap, period)
        m = pat.search(trail)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), period)
    return best[1] if best else None


# HTML entities that survive into a description and break a pay range apart.
#
# Greenhouse double-encodes: the adapter's single `html.unescape` turns
# `&amp;nbsp;` into `&nbsp;` and stops, so "£60,000 to &nbsp; £70,000" reaches
# the parser with a literal entity sitting in the middle of the range. `\s*`
# does not match it, the range collapses to its first figure, and the posting
# is filed £10,000 lower than it pays.
_ENTITY = re.compile(r"&(?:nbsp|#160|#xa0);|&(?:mdash|#8212|#x2014);|"
                     r"&(?:ndash|#8211|#x2013);|&(?:amp|#38);", re.I)
_ENTITY_TEXT = {"nbsp": " ", "#160": " ", "#xa0": " ",
                "mdash": "—", "#8212": "—", "#x2014": "—",
                "ndash": "–", "#8211": "–", "#x2013": "–",
                "amp": "&", "#38": "&"}


def _de_entity(text: str) -> str:
    return _ENTITY.sub(
        lambda m: _ENTITY_TEXT[m.group(0).strip("&;").lower()], text)


# A figure whose very next words name it as a bonus, an allowance or equity is
# not base pay, and treating it as pay is the worst kind of parse: IVC's "Head
# Veterinary Nurse ... we are also offering up to £3,000 welcome bonus" was
# stored as a £3,000 SALARY and then dropped by any floor at all, on a posting
# that stated no salary. Three postings in a 13,588 sample, and every one of
# them a role deleted rather than shown.
#
# Only the words IMMEDIATELY after the number count. "Salary up to £75,000 DOE
# Welcome Bonus up to £5,000" states a real salary and must keep it, so
# anything between the figure and the bonus word means the bonus word is
# describing a different number.
_IS_BONUS = re.compile(
    r"^\s*\)?\s*(?:welcome|signing|sign.?on|sign.?up|joining|referral|"
    r"retention|golden hello|relocation|cpd|training|learning|wellness|"
    r"home ?office|equipment|travel|car|lunch|book|study|kit|wfh)\s*"
    r"(?:bonus|allowance|budget|stipend|support|voucher|fund)"
    r"|^\s*\)?\s*(?:in\s+)?(?:equity|stock|shares|share options|stock options)\b",
    re.I)
# ...unless the figure was introduced as pay in the same breath.
_PAY_LEAD = re.compile(
    r"(?:salary|salaries|compensation|base(?:\s+pay)?|\bpay\b|package|"
    r"remuneration|earnings|wage|ote|rate|per annum|p\.?a\.?)\W{0,15}$", re.I)
# A bonus phrase carrying its OWN amount is describing that amount, not the
# figure in front of it. "Up to £65,000 Welcome bonus of up to £5,000" and
# "£3,000 welcome bonus for this position" are the same shape to a regex and
# opposite in meaning, and this is what separates them. Without it the first
# one was read as a £5,000 salary, which is worse than the bug being fixed.
_BONUS_OWN_AMOUNT = re.compile(r"^[^.]{0,24}?[£$€]\s?\d")

# The same question from the other side: "relocation allowance of up to
# £2,000" names the figure that FOLLOWS it. Nearest label wins, so a real
# "competitive relocation package and a salary of £70,000" still reads as pay.
_BONUS_LEAD = re.compile(
    r"\b(?:welcome|signing|sign.?on|sign.?up|joining|referral|retention|"
    r"golden hello|relocation|cpd|training|learning|wellness|home ?office|"
    r"equipment|travel|car|study|kit)\s+"
    r"(?:bonus|allowance|budget|stipend|support|package|voucher|fund)"
    r"|\bequity\b|\bstock options?\b|\bshare options?\b", re.I)
_BONUS_WINDOW = 60


def _is_bonus_figure(text: str, start: int, end: int) -> bool:
    """Is this number a bonus, an allowance or equity rather than pay?"""
    after = text[end:end + 40]
    m = _IS_BONUS.match(after)
    if m and not _BONUS_OWN_AMOUNT.match(after[m.end():]) \
            and not _PAY_LEAD.search(text[max(0, start - 40):start]):
        return True
    lead = text[max(0, start - _BONUS_WINDOW):start]
    bonus = max((x.end() for x in _BONUS_LEAD.finditer(lead)), default=-1)
    if bonus < 0:
        return False
    pay = max((x.end() for x in _PAY_WORD.finditer(lead)), default=-1)
    return bonus > pay


_PAY_WORD = re.compile(
    r"\b(?:salary|salaries|compensation|base pay|base salary|pay|package|"
    r"remuneration|earnings|wage|ote|rate|per annum|pay range|salary range)\b",
    re.I)

# Text that means "we are not telling you", not "zero"
_NOISE = re.compile(r"competitive|doe|depending on experience|negotiable", re.I)
# ...and what counts as "but it also states a figure". This was a literal
# `[£$€]\s?\d`, so "Competitive, up to SGD 180,000" took the early exit and
# threw the figure away before any pattern saw it.
_HAS_FIGURE = re.compile(rf"(?:{_CUR_PRE})\s?\d|\d\s?(?:{_CUR_SUF})", re.I)


# The European thousands spelling, and only that. Exactly three digits after
# each dot, optionally a comma decimal after them: "60.000", "1.234.567",
# "1.500,50". Anything else keeps the Anglo reading, so "60.00" is sixty and
# "1.5" is one and a half rather than fifteen hundred.
_EURO_THOUSANDS = re.compile(r"^\d{1,3}(?:\.\d{3})+(?:,\d+)?$")


def _to_float(tok: str) -> float | None:
    tok = tok.strip()
    mult = 1.0
    if tok.lower().endswith("k"):
        mult = 1000.0
        tok = tok[:-1].strip()
    if _EURO_THOUSANDS.match(tok):
        tok = tok.replace(".", "").replace(",", ".")
    else:
        tok = tok.replace(",", "")
    try:
        return float(tok) * mult
    except ValueError:
        return None


def _period(text: str) -> str:
    if _PER_DAY.search(text):
        return "day"
    if _PER_HOUR.search(text):
        return "hour"
    return "year"


# India states pay in units as often as it states it in digits: "\u20b913-16 LPA"
# is thirteen to sixteen LAKH per annum, and a lakh is a hundred thousand.
#
# This exists because teaching the parser the "\u20b9" mark without it made things
# WORSE rather than better. Eulerity's "Compensation: \u20b913-16 LPA" started
# matching, as a day rate of thirteen rupees, confirmed, and a confirmed
# figure is the only kind allowed to delete a role: a 1.3M-1.6M salary was
# about to be hidden behind a 4,000,000 floor it comfortably fails, which is
# a worse answer than the unconfirmed one it gave before.
#
# "LPA" and "CPA" carry their own period, so they override whatever period
# word happens to sit nearby -- that is the whole of what the "PA" means. A
# bare "lakh", "crore" or "L" does not, so those keep the ordinary period
# rules and "\u20b91 crore in monthly revenue" stays the monthly figure it says
# it is, which is to say discarded.
_LAKH_UNIT = re.compile(
    r"^[\s\u00a0]{0,3}(?:(?P<pa>lpa|cpa)|(?P<lakh>lakhs?|lacs?|L)"
    r"|(?P<crore>crores?|cr))\b", re.I)
_LAKH_MULT = {"lakh": 100_000.0, "crore": 10_000_000.0}


def _lakh_unit(text: str, end: int) -> tuple[float, bool]:
    """(multiplier, does the unit state its own period) for the unit, if any,
    written immediately after a figure. (1.0, False) when there is none."""
    m = _LAKH_UNIT.match(text[end:end + 12])
    if not m:
        return 1.0, False
    if m.group("pa"):
        return 100_000.0, True
    return _LAKH_MULT["crore" if m.group("crore") else "lakh"], False


# A number range in prose is not a salary. "40,000 to 120,000 requests per
# second" and "grew from 25,000 to 90,000 members" both parse as money if the
# currency symbol is optional, and because this runs over the first 1500
# characters of a description for most adapters, a confirmed-but-wrong figure
# below the floor silently DROPS a real role. So an unsymbolled range has to
# be introduced by something that means pay.
_PAY_CONTEXT = re.compile(
    r"\b(salary|salaries|compensation|comp\b|pay|paid|package|remuneration|"
    r"base|earnings|wage|ote|bonus|range for this role|pay range|"
    r"salary range|annum|per year|pa\b)\b", re.I)


def parse_text(text: str | None, default_currency: str | None = None) -> Salary:
    """Best-effort parse of a free-text pay string.

    Returns an unconfirmed Salary when nothing usable is found, which is the
    common case and not an error.
    """
    if not text:
        return Salary()
    full = " ".join(_de_entity(text).split())
    t = full[:400]
    if _NOISE.search(t) and not _HAS_FIGURE.search(t):
        return Salary(raw=t.strip()[:120])

    got = _scan(full, 0, 400, default_currency, need_context=False)
    if got.confirmed:
        return got
    # Most adapters hand this the whole description, and employers who do
    # publish a figure usually put it at the bottom: "Base salary range:
    # £48,000 - £72,000" sat at character 4,109 of a GoCardless posting and
    # 9% of a real scan came back "unconfirmed" while stating a range in the
    # body. Beyond the opening block the risk of reading a funding round or a
    # customer count as pay goes up, so out there a figure only counts when
    # pay is being discussed right next to it, symbol or no symbol.
    if len(full) > 400:
        got = _scan(full, 400, 20_000, default_currency, need_context=True)
        if got.confirmed:
            return got
    return got


def _scan(full: str, begin: int, stop: int, default_currency: str | None, *,
          need_context: bool) -> Salary:
    """Read one block of text for a pay figure.

    The annual-shaped patterns run first because they are the specific ones:
    `_SINGLE_RATE` reads "£45,000" as "£45" (its number is at most four
    digits and it stops at the comma), so letting the rate family go first
    turns a real salary into a rate one time in ten.

    The period is then taken from the words nearest THAT figure rather than
    from anywhere in the block, and a figure the text calls a bonus is
    skipped rather than filed as pay.
    """
    # The window is a slice of `full`, but the period and bonus questions are
    # asked of `full` at absolute positions. Asking them of the slice loses
    # any label that straddles the cut: "$10,000+ per month" sat at character
    # 383 and the block ended at 400, so "per mont" did not match "per month"
    # and a monthly figure was confirmed as an annual salary.
    t = full[begin:stop]
    chunk_period = _period(t)
    families = [(_RANGE, _SINGLE, False), (_RANGE_RATE, _SINGLE_RATE, True)]
    if chunk_period != "year":
        # What the shipped code did: a block that says "per hour" is read with
        # the rate patterns. Keeping that order matters, because the annual
        # patterns would otherwise reach past an hourly base to a commission
        # figure further down and report that as the salary.
        families.reverse()

    for rng, single, rate in families:
        for m in rng.finditer(t):
            lo, hi = _to_float(m.group("lo")), _to_float(m.group("hi"))
            if lo is None or hi is None or hi < lo:
                continue
            if _is_bonus_figure(full, begin + m.start(), begin + m.end()):
                continue
            marked = _match_currency(m)
            cur = marked or default_currency
            if not marked or need_context:
                # Only believe it if pay is being discussed within the
                # preceding stretch of text. Required always for an
                # unsymbolled range, and everywhere once we are past the
                # opening block.
                #
                # `continue`, not `return`. Abandoning the block on the first
                # number that fails this gate meant one "40,000 to 120,000
                # requests per second" high up in an advert deleted the real,
                # symbolled, pay-labelled range further down, and the second
                # pass applies the gate to EVERY match, so out there any
                # unlabelled number at all ended the search. Measured on the
                # published seed of 2026-08-28: 400 adverts of 33,918 came
                # back "unconfirmed salary" while plainly stating one, among
                # them Cohere's "$180,000 - $325,000" and DualEntry's
                # "$140,000 - $250,000". Nothing is loosened by this; every
                # match still has to pass the same gate to be believed.
                lead = t[max(0, m.start() - 120):m.start()]
                if not _PAY_CONTEXT.search(lead):
                    continue
            period = _period_near(full, begin + m.start(), begin + m.end()) or (
                chunk_period if rate or len(t) <= 400 else "year")
            mult, own_period = _lakh_unit(t, m.end()) if cur == "INR" else (1.0, False)
            if mult > 1.0 and not _PAY_CONTEXT.search(
                    t[max(0, m.start() - 120):m.start()]):
                # A unit multiplies by up to ten million, so it is believed
                # only where pay is being discussed. 2070Health's advert says
                # the business is "generating approximately \u20b91 crore in
                # monthly revenue", and taking that for the salary would put a
                # 10,000,000 figure on a role that states none.
                mult, own_period = 1.0, False
            if own_period:
                period = "year"
            if period == "month" or (rate and period == "year" and mult == 1.0):
                # The rate patterns exist only to read day and hour rates:
                # their number is four digits at most, so believing one as an
                # annual figure files a 13.45 an hour job as 13.45 a YEAR and
                # then hides it behind any floor at all. A monthly figure has
                # no period to be stored in, and reading it as annual is the
                # same mistake twelve times over.
                #
                # A unit is the exception: "\u20b913-16 LPA" is caught by the RATE
                # patterns, because thirteen is a rate-shaped number, and it
                # is an annual salary of 1.3 million all the same.
                continue
            return Salary(min=lo * mult, max=hi * mult, currency=cur,
                          period=period, raw=m.group(0).strip(),
                          confirmed=True)

        for m in single.finditer(t):
            v = _to_float(_match_value(m) or "")
            if v is None or v <= 0:
                continue
            if _is_bonus_figure(full, begin + m.start(), begin + m.end()):
                continue
            if need_context and not _PAY_CONTEXT.search(
                    t[max(0, m.start() - 120):m.start()]):
                # Same reason as the range loop above: skip this figure, do
                # not give up on the block.
                continue
            period = _period_near(full, begin + m.start(), begin + m.end()) or (
                chunk_period if rate or len(t) <= 400 else "year")
            cur = _match_currency(m) or default_currency
            mult, own_period = _lakh_unit(t, m.end()) if cur == "INR" else (1.0, False)
            if mult > 1.0 and not _PAY_CONTEXT.search(
                    t[max(0, m.start() - 120):m.start()]):
                mult, own_period = 1.0, False
            if own_period:
                period = "year"
            if period == "month" or (rate and period == "year" and mult == 1.0):
                continue
            return Salary(min=v * mult, max=v * mult, currency=cur,
                          period=period, raw=m.group(0).strip(),
                          confirmed=True)

    return Salary()


def from_ashby(comp: dict | None) -> Salary:
    """Ashby returns structured pay when the board is fetched with
    `includeCompensation=true`. Without that param the field is absent entirely.
    """
    if not comp:
        return Salary()
    raw = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
    s = parse_text(raw)
    if s.confirmed:
        s.raw = (raw or s.raw or "").strip()[:120]
    return s


def from_greenhouse(ranges: list | None) -> Salary:
    """Greenhouse exposes `pay_input_ranges` only when the board is fetched
    with `pay_transparency=true`. `content=true` does NOT trigger it; they are
    separate parameters, which is an easy one to miss.
    """
    if not ranges:
        return Salary()
    r = ranges[0] if isinstance(ranges, list) else ranges
    if not isinstance(r, dict):
        return Salary()

    def _amt(*keys):
        for k in keys:
            v = r.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return float(v) / 100.0 if k.endswith("_cents") else float(v)
        return None

    lo = _amt("min_cents", "min_value", "min")
    hi = _amt("max_cents", "max_value", "max")
    cur = r.get("currency_type") or r.get("currency")
    if lo is None and hi is None:
        return parse_text(r.get("title"))

    # Greenhouse states no period anywhere in `pay_input_ranges`, so this used
    # to assume "year" for every figure in it. Databricks publish hourly rates
    # through the same field and label them in the title: "SF Bay Area Hourly
    # Rate", min_cents 5400. Read as annual that is a salary of $54, which is
    # not a rounding error, it is a role that gets discarded by any floor.
    title = str(r.get("title") or "")
    period = "year"
    if re.search(r"\bhourly\b|\bper hour\b|/\s*hr\b|\bhour rate\b", title, re.I):
        period = "hour"
    elif re.search(r"\bdaily\b|\bper day\b|/\s*day\b|\bday rate\b", title, re.I):
        period = "day"
    elif (hi or lo or 0) < 2000:
        # Unlabelled and far too small to be a year's pay. Databricks send
        # 17040 cents under the generic title "Local Pay Range", which is
        # $170.40 and is plainly a rate, but nothing in the payload says which
        # kind. This follows the rule the Reed adapter already states: an
        # unlabelled figure below 2,000 is left unconfirmed rather than
        # asserted as an annual salary, because only a confirmed figure can
        # disqualify a role and a wrong one disqualifies it wrongly.
        return Salary(min=lo, max=hi or lo, currency=cur, period="year",
                      raw=title or None, confirmed=False)

    return Salary(
        min=lo, max=hi or lo, currency=cur, period=period,
        raw=(title or f"{lo:,.0f}-{hi:,.0f}" if lo and hi else None),
        confirmed=True,
    )


def from_pinpoint(posting: dict | None) -> Salary:
    """Pinpoint publishes pay as real numbers, not as a sentence to parse.

    `compensation_visible` is the employer's own switch. When it is off the
    minimum and maximum are still present but must not be shown or filtered
    on, so this treats a hidden figure as no figure at all rather than as a
    confirmed one. Getting that backwards would drop roles on a floor the
    employer never published.

    Frequencies seen live are `year`, `hour` and `week`. Salary only models
    year, day and hour, so weekly and monthly rates are annualised here rather
    than dropped: a rate this tool cannot express is a rate the floor cannot
    act on, which quietly loses the role.
    """
    if not isinstance(posting, dict) or not posting.get("compensation_visible"):
        return Salary()

    def _amt(key):
        v = posting.get(key)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None

    lo, hi = _amt("compensation_minimum"), _amt("compensation_maximum")
    raw = (posting.get("compensation") or "").strip()
    if lo is None and hi is None:
        return parse_text(raw)

    freq = (posting.get("compensation_frequency") or "year").lower()
    mult = {"week": 52.0, "month": 12.0, "annual": 1.0, "year": 1.0}.get(freq)
    if mult is not None:
        period = "year"
        lo = lo * mult if lo is not None else None
        hi = hi * mult if hi is not None else None
    else:
        period = freq if freq in ("day", "hour") else "year"

    return Salary(
        min=lo, max=hi if hi is not None else lo,
        currency=(posting.get("compensation_currency") or "").upper() or None,
        period=period, raw=raw[:120] or None, confirmed=True,
    )


# What Reed's `salaryType` says, in the vocabulary Salary understands.
_REED_PERIOD = {
    "per annum": "year", "annum": "year", "annual": "year", "annually": "year",
    "per year": "year", "year": "year", "yearly": "year",
    "per day": "day", "day": "day", "daily": "day",
    "per hour": "hour", "hour": "hour", "hourly": "hour",
}
# Salary models only year, day and hour, so weekly and monthly rates are
# annualised rather than dropped, exactly as from_pinpoint does: a rate this
# tool cannot express is a rate the floor cannot act on, which quietly loses
# the role.
_REED_ANNUALISE = {"per week": 52.0, "week": 52.0, "weekly": 52.0,
                   "per month": 12.0, "month": 12.0, "monthly": 12.0}

# Reed's SEARCH endpoint returns `minimumSalary` and `maximumSalary` as bare
# numbers with no period at all; only the per-job details endpoint carries
# `salaryType`. So a figure off the search endpoint has to be read for what it
# plainly is. Nothing below this can be a UK annual salary (the National
# Minimum Wage alone puts a full-time year several times higher, and even a few
# hours a week runs to thousands), while senior contract day rates top out
# somewhere near 1,500. So a number under this is a rate Reed has not labelled,
# and treating it as an annual figure would read a 650 a day contract as 650 a
# year and bin it against any floor at all.
_REED_MIN_ANNUAL = 2000.0


def from_reed(job: dict | None) -> Salary:
    """Reed publishes pay as numbers, but which numbers depends on the endpoint.

    The details endpoint states `salaryType` ("per annum", "per day", "per
    hour" were the values seen on 17 Sept 2026) next to `minimumSalary` and
    `maximumSalary`, and that is what is read: the figure in the unit the
    employer wrote it in. The search endpoint states no period at all, so an
    unlabelled figure is only trusted as annual when it is big enough to be
    one.

    `yearlyMinimumSalary` / `yearlyMaximumSalary` are deliberately ignored.
    They used to win, and they are Reed's own annualisation on Reed's own day
    count: SF Partners' 700 to 1,250 per day came back as 182,000 to 325,000
    a year (260 days) and a 45 to 50 per hour contract as 87,750 to 97,500
    (1,950 hours). Stored that way the dashboard said "per year" about a day
    rate, and the floor compared a number `Salary.annualised` would have
    computed differently for every other platform's day rates.

    A `salaryType` this table does not know comes back UNCONFIRMED, with the
    figures kept for the reader. Guessing "year" for it is how a day rate
    becomes a salary of 700 that any floor bins.

    An unlabelled rate comes back unconfirmed for the same reason: an
    unconfirmed salary is shown to the reader and can never disqualify a
    role, whereas a wrongly annualised one silently deletes it. `parse_reed`
    then gets a second go at it from the advert text, which may say "per
    day".

    Reed also lets an employer hide the salary, in which case the figures are
    null even though `salaryType` is still set. That is "the employer
    published no figure", not a parse failure, and it must stay unconfirmed.
    """
    if not isinstance(job, dict):
        return Salary()

    def _amt(key):
        v = job.get(key)
        try:
            return float(v) if v is not None and float(v) > 0 else None
        except (TypeError, ValueError):
            return None

    raw = str(job.get("salary") or "").strip()[:120] or None
    currency = str(job.get("currency") or "").strip().upper() or None

    lo, hi = _amt("minimumSalary"), _amt("maximumSalary")
    if lo is None and hi is None:
        return parse_text(raw)

    period = "year"
    stype = str(job.get("salaryType") or "").strip().lower()
    mult = _REED_ANNUALISE.get(stype)
    if mult is not None:
        lo = lo * mult if lo is not None else None
        hi = hi * mult if hi is not None else None
    elif stype in _REED_PERIOD:
        period = _REED_PERIOD[stype]
    elif stype:
        return Salary(min=lo, max=hi if hi is not None else lo,
                      currency=currency, period="year", raw=raw,
                      confirmed=False)
    else:
        top = hi if hi is not None else lo
        if top is not None and top < _REED_MIN_ANNUAL:
            return Salary(min=lo, max=hi if hi is not None else lo,
                          currency=currency, period="year", raw=raw,
                          confirmed=False)

    return Salary(
        min=lo, max=hi if hi is not None else lo, currency=currency,
        period=period, raw=raw, confirmed=True,
    )


def clears_floor(sal: Salary, floor: float | None, currency: str | None = None) -> tuple[bool, str]:
    """Apply the rule. Returns (keep, why).

    Unconfirmed salaries always pass. Cross-currency comparison is refused
    rather than guessed at, because a wrong FX assumption silently drops real
    roles; those are kept and flagged instead.
    """
    if not floor:
        return True, ""
    if not sal.confirmed:
        return True, "unconfirmed salary"
    if currency and sal.currency and sal.currency != currency:
        return True, f"salary in {sal.currency}, floor in {currency}, not compared"
    if currency and not sal.currency:
        # A figure with no currency mark used to be compared against the floor
        # as a bare number, so an unsymbolled 2,500,000 zloty cleared a
        # 55,000 euro floor and an unsymbolled 45,000 of anything failed it.
        # Neither outcome was flagged.
        return True, f"salary has no currency, floor in {currency}, not compared"
    top = sal.annualised()
    if top is None:
        return True, "unconfirmed salary"
    if top < floor:
        shown = sal.raw or f"{top:,.0f}"
        return False, f"stated pay {shown} below floor"
    return True, ""


# Adzuna publishes a figure for almost every advert, but only some of them come
# from the employer. `salary_is_predicted` is "1" when the number is Adzuna's
# own Jobsworth estimate, produced by a model from the title and location, and
# "0" when the advertiser stated it. Treating an estimate as a stated figure is
# the worst available outcome in both directions: a low estimate silently DROPS
# a real role against the floor, and a high one promotes a role that pays
# nothing like it. Only `confirmed=True` may disqualify a posting, so an
# estimate has to come back unconfirmed.
#
# The currency is not in the payload at all. It follows from which country
# endpoint was called, because Adzuna runs one index per country and states
# pay "in the local currency", so `parse_adzuna` passes it in from the URL.
_ADZUNA_MIN_ANNUAL = 2000.0


def from_adzuna(job: dict | None, currency: str | None = None) -> Salary:
    """Adzuna's pay fields, with the predicted ones refused.

    Three ways this comes back unconfirmed, and all three are correct:

      * `salary_is_predicted == "1"`. A Jobsworth estimate is a model output,
        not something the employer wrote down.
      * Neither figure is present. The advertiser published no pay.
      * The figure is too small to be an annual salary. Adzuna's own filters
        are annual and it normalises rates upward, but a feed that arrives
        unnormalised would put a bare `650` day rate in the same field, and
        reading that as a year's pay bins the role against any floor at all.
        Same threshold and same reasoning as `from_reed`.

    The numbers are kept on the unconfirmed Salary rather than thrown away, so
    the reader still sees what Adzuna thought.
    """
    if not isinstance(job, dict):
        return Salary()

    def _amt(key):
        v = job.get(key)
        try:
            return float(v) if v is not None and float(v) > 0 else None
        except (TypeError, ValueError):
            return None

    lo, hi = _amt("salary_min"), _amt("salary_max")
    if lo is None and hi is None:
        return Salary(currency=currency)

    top = hi if hi is not None else lo
    predicted = str(job.get("salary_is_predicted") or "").strip() == "1"
    plausible = top is not None and top >= _ADZUNA_MIN_ANNUAL
    return Salary(
        min=lo, max=hi if hi is not None else lo, currency=currency,
        period="year",
        raw=("estimated by Adzuna" if predicted else None),
        confirmed=not predicted and plausible,
    )
