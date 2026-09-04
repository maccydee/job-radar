"""Filtering and scoring.

Two stages, deliberately separate:

  * `match` decides whether a posting is even the kind of job you want, on
    title and location. Cheap, runs on everything.
  * `screen` reads the description for your dealbreakers. This is the part
    that catches the roles that look right in a search result and are wrong
    in the detail.

Nothing here is a black box. Every kept role carries the reasons it scored
what it did, and every dropped one carries why.
"""

from __future__ import annotations

import itertools
import re
import unicodedata

from . import employment
from .config import Config
from .models import Job
from .salary import clears_floor

# Working out which country a posting is in is the single most load-bearing
# thing in this file, and it is harder than it looks, because city names are
# not unique across countries. "New York City" contains "york"; there is a
# Cambridge in Massachusetts, a Birmingham in Alabama, a Manchester in New
# Hampshire and a Newcastle in Australia. Matching city names against a flat
# list and taking the first hit marked 59 of 296 American roles as British.
#
# So the signals are tiered. An explicit country name always beats a city
# name, and a US state code beats both, because ", NY" is unambiguous in a way
# that "york" never is.

# Tier 1: the location says which country outright.
_COUNTRY_MARKERS = {
    # "New England" is not England and "New South Wales" is not Wales. The
    # word-boundary form claimed both: "Sydney, New South Wales" resolved to
    # the UK, and so did "New England, Texas". For a UK user that is a US and
    # an Australian role shown as British; for an Australian one it is a
    # Sydney job filed abroad and dropped.
    "UK": r"united kingdom|\buk\b|\bg\.?b\.?\b|(?<!new )\bengland\b|\bscotland\b|"
          r"(?<!new south )\bwales\b|northern ireland|\bbritain\b",
    # The old form required the trailing "a", so "U.S. Remote" and "Remote
    # U.S." matched nothing at all and 170 postings written that way arrived
    # with no country. "U.S." on its own has to be enough.
    "US": r"united states|\bu\.\s?s\.?(?:a\.?)?|\busa?\b|\bamericas?\b",
    "IE": r"\bireland\b(?!,? *north)",
    # "Deutschlandweit" is how a German employer writes "anywhere in Germany",
    # and the word-boundary form did not match it.
    "DE": r"\bgermany\b|\bdeutschland", "FR": r"\bfrance\b", "ES": r"\bspain\b|\bespaña\b",
    # "Nederland" is what Dutch boards write, and it was absent: 2,110
    # postings said it. Guarded because Nederland TX and Nederland CO exist.
    # Lowercase in the guard because the markers are matched against a
    # lowercased string; spelling it "TX" made the guard dead and filed
    # Nederland, Texas in the Netherlands.
    "NL": r"netherlands|\bnederland\b(?!,?\s*(?:tx|co|texas|colorado)\b)",
    "CA": r"\bcanada\b", "AU": r"\baustralia\b",
    "NZ": r"new zealand", "AE": r"\buae\b|united arab emirates",
    "SG": r"\bsingapore\b", "HK": r"hong kong", "IN": r"\bindia\b",
    "JP": r"\bjapan\b", "CN": r"\bchina\b", "PL": r"\bpoland\b",
    "PT": r"\bportugal\b", "SE": r"\bsweden\b", "CH": r"switzerland",
    "IL": r"\bisrael\b", "BR": r"\bbrazil\b|\bbrasil\b",
    # Guarded against New Mexico, which is a US state and not a country.
    # This tier runs before the US state tier, so the unguarded form won
    # outright: "Albuquerque, New Mexico" and "New Mexico - Remote" both
    # resolved to MX, and the only ones that escaped were the few that also
    # spelled out "United States". Same shape as the Nederland/Paris guards.
    "MX": r"(?<!new )\bmexico\b",
    "ZA": r"south africa", "ID": r"\bindonesia\b", "TH": r"\bthailand\b",
    "MY": r"\bmalaysia\b", "PH": r"philippines", "IT": r"\bitaly\b",
    "BE": r"\bbelgium\b", "AT": r"\baustria\b", "DK": r"\bdenmark\b",
    "NO": r"\bnorway\b", "FI": r"\bfinland\b", "CZ": r"czech",
    "RO": r"\bromania\b", "TR": r"\bturkey\b", "AR": r"\bargentina\b",
    "VN": r"\bvietnam\b", "KR": r"south korea",
}

# Tier 2: a US state code after a comma ("San Francisco, CA"). Case-sensitive
# on purpose, so it cannot fire on the word "ca" inside ordinary prose.
_US_STATE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|"
    r"WA|WV|WI|WY|DC)\b"
)

# The twenty state codes that are also ISO 3166-1 country codes. Matching one
# of these proves nothing on its own, so the city decides first.
_AMBIGUOUS_STATE = re.compile(
    r",\s*(AL|AR|CA|CO|DE|GA|ID|IL|IN|KY|LA|MD|ME|MO|MS|MT|NC|NE|PA|SC|SD|"
    r"TN|VA)\b"
)

# Spelled out, too. "Birmingham, Alabama" and "Cambridge, Massachusetts" were
# reading as UK, because only the two-letter codes were recognised and the
# city list is checked with UK first.
_US_STATE_NAME = re.compile(
    r",\s*(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|west virginia|wisconsin|wyoming)\b",
    re.I)

# Tier 3: city names, consulted only when nothing above fired. UK entries that
# collide with a bigger foreign city are guarded rather than dropped, since
# "London" and "Manchester" are still the common case in a UK-facing tool.
_CITY_HINTS = {
    "UK": r"\blondon\b(?!,? *(?:ontario|ky|oh))|\bmanchester\b|\bbristol\b|"
          r"\bbirmingham\b|\bleeds\b|\bedinburgh\b|\bglasgow\b|"
          r"\bcambridge\b|\boxford\b|\breading\b|milton keynes|\bcardiff\b|"
          r"\bbelfast\b|\bliverpool\b|\bnewcastle\b|\bsheffield\b|"
          r"\bnottingham\b|\bsouthampton\b|\bbrighton\b|(?<!new )\byork\b|"
          r"\bbath\b|\bleicester\b|\bcoventry\b|\bderby\b|\bswindon\b|"
          r"\bipswich\b|\bnorwich\b|\bexeter\b|\bplymouth\b|"
          # A UK postcode is a strong signal on its own: employers hiring
          # nationally list towns, not cities.
          r"\b[a-z]{1,2}\d[a-z\d]?\s*\d[a-z]{2}\b",
    "US": r"san francisco|new york|seattle|austin|boston|chicago|los angeles|"
          r"denver|atlanta|palo alto|mountain view|menlo park|san jose|"
          r"washington,? d\.?c|bellevue|redmond|sunnyvale",
    "IE": r"\bdublin\b(?!,? *(?:oh|ca))|\bcork\b|galway|limerick", "DE": r"\bberlin\b|munich|m\u00fcnchen|hamburg|cologne|k\u00f6ln|frankfurt|stuttgart|d\u00fcsseldorf|dusseldorf|leipzig|n\u00fcrnberg|nuremberg",
    "FR": r"\bparis\b(?!,? *(?:tx|tn))|\blyon\b|marseille|toulouse|bordeaux|\bnantes\b|\blille\b", "ES": r"\bmadrid\b|barcelona|valencia|seville|sevilla|bilbao|malaga|m\u00e1laga|zaragoza",
    "NL": r"amsterdam|rotterdam|utrecht|eindhoven|the hague|den haag|groningen|delft", "CA": r"\btoronto\b|vancouver|montreal|ottawa",
    "AU": r"\bsydney\b|melbourne|brisbane|perth", "NZ": r"auckland|wellington",
    "AE": r"\bdubai\b|abu dhabi", "IN": r"bangalore|bengaluru|hyderabad|mumbai|pune|gurgaon|noida",
    "JP": r"\btokyo\b", "CN": r"beijing|shanghai|shenzhen", "PL": r"warsaw|warszawa|krakow|krak\u00f3w|wroclaw|wroc\u0142aw|gdansk|gda\u0144sk|poznan|\bl\u00f3dz\b",
    "PT": r"\blisbon\b|lisboa|\bporto\b|\boporto\b|coimbra|\bbraga\b|\bfaro\b|aveiro|funchal", "SE": r"stockholm|gothenburg|g\u00f6teborg|malm\u00f6|\bmalmo\b", "CH": r"zurich|z\u00fcrich|geneva|gen\u00e8ve|basel|lausanne|zug\b",
    "IL": r"tel aviv", "BR": r"s(?:a|\u00e3)o paulo|rio de janeiro", "ZA": r"cape town|johannesburg",
    "ID": r"jakarta", "TH": r"bangkok", "MY": r"kuala lumpur", "PH": r"manila",
    "IT": r"\bmilan\b|milano|\brome\b|\broma\b|turin|torino|bologna|florence|firenze|naples", "BE": r"brussels|bruxelles|antwerp|\bghent\b|leuven", "AT": r"\bvienna\b|\bwien\b|\bgraz\b|salzburg",
    "DK": r"copenhagen|k\u00f8benhavn|aarhus|\bodense\b", "NO": r"\boslo\b|bergen|trondheim", "FI": r"helsinki|espoo|tampere", "CZ": r"prague|praha|\bbrno\b",
    "RO": r"bucharest", "TR": r"istanbul", "AR": r"buenos aires", "SG": r"\bsingapore\b",
    "HK": r"hong kong", "KR": r"\bseoul\b", "VN": r"hanoi|ho chi minh",
}

# "Remote" with nothing else attached. Anything more specific than this names
# a place, and a place has to clear the country filter even when it is remote:
# a US-remote role is not open to someone in the UK.
# Every word here means "we have not told you where". "Remote - Worldwide",
# "Hybrid", "HQ", "Various Locations" and "Multiple" all name no place, and a
# posting that names no place must be treated as one that named none: 75
# postings in a 13,588-posting sample were dropped as "location not
# recognised" for saying nothing at all, which is the opposite of what the
# rule below it does with an EMPTY location field.
_NOWHERE = (r"remote|anywhere|global(?:ly)?|worldwide|distributed|hybrid|"
            r"on[- ]?site|onsite|various|multiple|all|company[- ]?wide|"
            r"flexible|tbc|tba|n/?a|hq|head office|headquarters|office|"
            r"virtual|unspecified|locations?")
_GENERIC_REMOTE = re.compile(
    rf"^[\s(]*(?:(?:fully|100%|mostly|primarily)\s+)?(?:{_NOWHERE})"
    rf"(?:(?:\s*[,\-–—/&()]\s*|\s+(?:or|and)\s+|\s+)(?:{_NOWHERE}))*"
    r"[\s,\-–—/()]*$",
    re.I,
)

# Deliberately NOT splitting on commas. A comma binds a place to its
# qualifier ("Cambridge, MA"), and splitting there throws away the state code
# that identifies the country. Postings separate genuinely distinct locations
# with a pipe or a slash.
_SPLIT = re.compile(r"[;|/]| or |\bor\b")


# ---------------------------------------------------------------- place data
#
# What follows is lists, not more alternations. A tagging run over the whole
# bundled list read 433,955 live postings and 94,841 of them (21.9%) carried a
# location this file could not place. The shapes behind that number are not
# exotic: countries nobody had typed in yet, towns outside the dozen biggest
# cities, and one widespread ATS convention that writes the country as a
# lowercase code. A list is the right shape for that, because being wrong
# about one entry is a one-line fix and being wrong about a regex is an
# afternoon.

# UTF-8 decoded as Latin-1 turns "München" into "MÃ¼nchen". 2,430 unresolved
# postings (3.3% of the total) arrived mangled that way, with "MÃ¼nchen",
# "KÃ¶ln" and "DÃ¼sseldorf" alone accounting for over 1,500. The accented
# spellings were already on the city list; the bytes just needed putting back.
_MOJIBAKE = re.compile(r"[ÃÂ][\x80-\xbf]")


def _repair(s: str) -> str:
    """Undo a UTF-8 string that was decoded as Latin-1, where that is what happened."""
    if _MOJIBAKE.search(s):
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeError, ValueError):
            pass
    return s


def _fold(s: str) -> str:
    """Accents stripped, so a plain pattern can match "Montréal" or "Košice".

    Matching is tried against the folded AND the unfolded text, never only
    the folded one: the city list holds accented spellings ("münchen",
    "kraków") that folding would stop matching.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _alt(names) -> str:
    """A word-boundary alternation over a list of place names.

    Longest first, because Python's alternation takes the first branch that
    matches and "stoke on trent" must win over "stoke".
    """
    return r"\b(?:" + "|".join(sorted((re.escape(n) for n in names),
                                      key=len, reverse=True)) + r")\b"


# ISO 3166-1 alpha-2 with the local-language and colloquial spellings
# employers actually type. Names are matched as a WHOLE comma-separated
# segment, never as a substring, so "Jordan" cannot fire inside a street name
# and "Lebanon" cannot fire inside "Lebanon, PA" (the US state tier answers
# that one before this table is reached anyway).
#
# GB is written UK because that is the code this tool's config uses.
# Georgia is deliberately absent. "Atlanta, Georgia" and "Tbilisi, Georgia"
# are the same shape and the US state is by far the commoner reading, so the
# country is recognised from its cities instead and the bare word stays US.
_ISO_TABLE = """
AD Andorra
AE United Arab Emirates|UAE
AF Afghanistan
AG Antigua and Barbuda
AL Albania
AM Armenia
AO Angola
AR Argentina
AT Austria|Österreich|Oesterreich
AU Australia
AZ Azerbaijan
BA Bosnia and Herzegovina|Bosnia
BB Barbados
BD Bangladesh
BE Belgium|Belgique|België|Belgie
BF Burkina Faso
BG Bulgaria
BH Bahrain
BI Burundi
BJ Benin
BN Brunei|Brunei Darussalam
BO Bolivia
BR Brazil|Brasil
BS Bahamas|The Bahamas
BT Bhutan
BW Botswana
BY Belarus
BZ Belize
CA Canada
CD Democratic Republic of the Congo|DR Congo
CF Central African Republic
CH Switzerland|Schweiz|Suisse|Svizzera
CI Ivory Coast|Côte d'Ivoire|Cote d'Ivoire
CL Chile
CM Cameroon
CN China
CO Colombia
CR Costa Rica
CU Cuba
CV Cape Verde|Cabo Verde
CY Cyprus
CZ Czechia|Czech Republic
DE Germany|Deutschland
DK Denmark|Danmark
DO Dominican Republic
DZ Algeria
EC Ecuador
EE Estonia|Eesti
EG Egypt
ER Eritrea
ES Spain|España|Espana
ET Ethiopia
FI Finland|Suomi
FJ Fiji
FR France
GA Gabon
GH Ghana
GM Gambia
GN Guinea
GQ Equatorial Guinea
GR Greece|Hellas
GT Guatemala
GY Guyana
HK Hong Kong
HN Honduras
HR Croatia|Hrvatska
HT Haiti
HU Hungary|Magyarország|Magyarorszag
ID Indonesia
IE Ireland|Republic of Ireland|Éire
IL Israel
IN India
IQ Iraq
IR Iran
IS Iceland|Ísland
IT Italy|Italia
JM Jamaica
JO Jordan
JP Japan
KE Kenya
KG Kyrgyzstan
KH Cambodia
KR South Korea|Korea|Korea, Republic of|Republic of Korea
KW Kuwait
KZ Kazakhstan
LA Laos
LB Lebanon
LI Liechtenstein
LK Sri Lanka
LR Liberia
LS Lesotho
LT Lithuania|Lietuva
LU Luxembourg|Luxemburg
LV Latvia|Latvija
LY Libya
MA Morocco|Maroc
MC Monaco
MD Moldova
ME Montenegro
MG Madagascar
MK North Macedonia
ML Mali
MM Myanmar|Burma
MN Mongolia
MO Macau|Macao
MR Mauritania
MT Malta
MU Mauritius
MV Maldives
MW Malawi
MX Mexico|México
MY Malaysia
MZ Mozambique
NA Namibia
NG Nigeria
NI Nicaragua
NL Netherlands|Nederland|The Netherlands|Holland
NO Norway|Norge
NP Nepal
NZ New Zealand|Aotearoa
OM Oman
PA Panama
PE Peru|Perú
PG Papua New Guinea
PH Philippines|The Philippines|Pilipinas
PK Pakistan
PL Poland|Polska
PR Puerto Rico
PS Palestine
PT Portugal
PY Paraguay
QA Qatar
RO Romania|România
RS Serbia|Srbija
RU Russia|Russian Federation
RW Rwanda
SA Saudi Arabia|KSA|Kingdom of Saudi Arabia
SC Seychelles
SD Sudan
SE Sweden|Sverige
SG Singapore
SI Slovenia|Slovenija
SK Slovakia|Slovensko
SN Senegal
SO Somalia
SR Suriname
SV El Salvador
SY Syria
TD Chad
TH Thailand
TJ Tajikistan
TM Turkmenistan
TN Tunisia|Tunisie
TR Turkey|Türkiye|Turkiye
TT Trinidad and Tobago
TW Taiwan
TZ Tanzania
UA Ukraine
UG Uganda
UK United Kingdom|Great Britain|Britain|England|Scotland|Wales|Northern Ireland
US United States|United States of America|USA|U.S.A.
UY Uruguay
UZ Uzbekistan
VE Venezuela
VN Vietnam|Viet Nam
YE Yemen
ZA South Africa
ZM Zambia
ZW Zimbabwe
"""

# name (lowercased, and again accent-folded) -> country code
_COUNTRY_NAME: dict[str, str] = {}
for _line in _ISO_TABLE.strip().splitlines():
    _code, _rest = _line.split(" ", 1)
    for _name in _rest.split("|"):
        _COUNTRY_NAME[_name.lower()] = _code
        _COUNTRY_NAME[_fold(_name.lower())] = _code

_ISO_CODES = {l.split(" ", 1)[0] for l in _ISO_TABLE.strip().splitlines()}

# code -> the first, canonical name. The table is written name-first because
# everything else here reads a location string and wants a code; this is the
# one caller going the other way, a search API that takes a country by name.
_CANONICAL_NAME: dict[str, str] = {
    _l.split(" ", 1)[0]: _l.split(" ", 1)[1].split("|")[0]
    for _l in _ISO_TABLE.strip().splitlines()
}


def country_name(code: str) -> str:
    """"UK" -> "United Kingdom". Empty for anything not in the table."""
    return _CANONICAL_NAME.get((code or "").strip().upper(), "")

_US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC".split())

# The codes that are both an ISO country and a US state, worked out rather
# than typed, so the two lists cannot drift apart. Same rule as the uppercase
# case: the code alone proves nothing and something else in the string has to
# name that country independently.
_AMBIGUOUS_CC = frozenset(_ISO_CODES & _US_STATE_CODES)


def _code_country(cc: str) -> str | None:
    """A country code in this tool's vocabulary, or None if it names nothing."""
    cc = cc.upper()
    if cc == "GB":
        return "UK"
    return cc if cc in _ISO_CODES else None


# The single biggest fixable shape in the data. 15,915 unresolved postings,
# 21.4% of everything unresolved, end in a LOWERCASE ISO country code:
# "Aachen, NRW, de", "Montréal, QC, ca", "Chennai, in", "Sofia, bg". All
# 1,585 distinct strings carrying it were genuinely that country, with no US
# state among them, and the lowercasing is itself the disambiguator, because
# `_US_STATE` is case-sensitive and only ever matches an uppercase code. So
# ", CA" and ", ca" cannot collide.
_TRAILING_CC = re.compile(r",\s*([a-z]{2})\s*$")

# Subnational codes and names, used only to corroborate one of the
# ambiguous codes above. "Whitecourt, AB, ca" is Canada because AB is a
# Canadian province; "Savannah, ga" stays unresolved because nothing in it
# names Gabon.
#
# Two patterns per country, because case is load-bearing in one half and not
# the other. The codes are matched against the string as written, the same
# rule `_US_STATE` follows, so "ON" is Ontario and "on" is the English word.
# Reading them case-insensitively would let "hands on, ca" corroborate
# Canada, and "by", "he", "st", "as" and "up" would do the same for Germany
# and India. The spelled-out names carry no such risk.
#
# NL, PE and SK are left out of the Canadian codes on purpose: Newfoundland,
# Prince Edward Island and Saskatchewan share them with the Netherlands, Peru
# and Slovakia, so a bare ", NL" is a coin toss.
_SUBNATIONAL = {
    "CA": (r"\b(?:AB|BC|MB|NB|NS|NT|NU|ON|QC|YT)\b",
           r"\b(?:alberta|british columbia|manitoba|new brunswick|"
           r"nova scotia|ontario|quebec|saskatchewan|newfoundland|nunavut|"
           r"yukon|northwest territories|prince edward island)\b"),
    "DE": (r"\b(?:BW|BY|BE|BB|HB|HH|HE|MV|NI|NW|NRW|RP|SL|SN|ST|SH|TH)\b",
           r"\b(?:nordrhein[- ]westfalen|north rhine[- ]westphalia|bayern|"
           r"bavaria|baden[- ]wurttemberg|hessen|hesse|niedersachsen|"
           r"lower saxony|sachsen|saxony|thuringen|thuringia|"
           r"rheinland[- ]pfalz|schleswig[- ]holstein|brandenburg|saarland|"
           r"mecklenburg[- ]vorpommern)\b"),
    "IN": (r"\b(?:AP|AS|BR|CG|GJ|HR|HP|JH|KA|KL|MP|MH|OD|PB|RJ|TN|TG|UP|WB|DL)\b",
           r"\b(?:maharashtra|karnataka|tamil nadu|telangana|kerala|gujarat|"
           r"haryana|punjab|rajasthan|west bengal|uttar pradesh|odisha)\b"),
    # Australia was missing, which is why "Newcastle, New South Wales" was a
    # British job: the state was invisible and the city hint was not.
    # Deliberately no "victoria" or "tasmania" in the spelled-out list:
    # Victoria is a city in British Columbia and a district in London.
    "AU": (r"\b(?:NSW|QLD|VIC|WA|SA|TAS|ACT|NT)\b",
           r"\b(?:new south wales|queensland|western australia|"
           r"south australia|northern territory|"
           r"australian capital territory)\b"),
}

# Canadian provinces as a signal in their own right, not only as
# corroboration. "Winnipeg, Manitoba", "Brampton, ON" and "Calgary, AB" all
# named Canada and none of them resolved. The code half is case-sensitive for
# the reason above; the name half is not.
_CA_PROVINCE = re.compile(r",\s*(?:AB|BC|MB|NB|NS|NT|NU|ON|QC|YT)\b")
_CA_PROVINCE_NAME = re.compile(
    r",\s*(?:alberta|british columbia|manitoba|new brunswick|nova scotia|"
    r"ontario|quebec|saskatchewan|newfoundland|nunavut|yukon|"
    r"northwest territories|prince edward island)\b", re.I)

# US states spelled out with no comma in front. "California", "Texas" and
# "Maryland - Remote" are whole locations on plenty of boards, and the
# existing rule wanted a leading comma, so a state on its own resolved to
# nothing. Georgia is excluded, per the note on the ISO table.
_US_STATES_BARE = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "west virginia", "wisconsin", "wyoming",
)
# The work-mode qualifier employers glue on is allowed, because the data has
# "Virginia-remote", "Maryland - Remote" and "New Hampshire - Remote".
_US_STATE_ALONE = re.compile(
    r"^\s*(?:" + "|".join(_US_STATES_BARE) + r")"
    r"(?:\s*[-–—,]?\s*(?:remote|hybrid|on[- ]?site|in[- ]?office|us|usa))?\s*$",
    re.I)

# State codes that are NOT also ISO country codes, so they can be trusted with
# only a space in front. "Dallas TX", "Tampa FL" and "Olathe KS" all name a US
# state and none resolved, because the rule wanted a comma. Subtracted from
# the ISO list rather than typed out, so no code can be trusted here and
# treated as ambiguous three lines further down: without the comma there is
# nothing to corroborate against, and "Casablanca MA" must not become
# American. The ambiguous ones stay comma-only.
_US_STATE_SPACED = re.compile(
    r"[a-z]\s+(?:" + "|".join(sorted(_US_STATE_CODES - _ISO_CODES)) + r")\s*$")

# Taleo flattens a JSON location array into a hyphen-joined hierarchy,
# biggest first: "IL-Northbrook", "TX-Plano Legacy", "PH-National
# Capital-Quezon City". `parse_taleo` unpicks it in the adapter, but boards
# mirroring Taleo's format send the raw shape here too, so the head is read
# in this file as well. Only a leading two-letter code counts, which is why
# "Stoke-on-Trent" and "Aix-en-Provence" are untouched.
_TALEO_HEAD = re.compile(r"^([A-Z]{2})-(?=[A-Za-z])")

# Georgia the country, told apart from Georgia the state by its cities. This
# is a precision fix rather than a recall one: "Tbilisi, Georgia" resolved to
# US before, because the spelled-out state rule matched the last word.
_GEORGIA_COUNTRY = re.compile(r"\b(?:tbilisi|t'bilisi|batumi|kutaisi|rustavi)\b")

# Any county ending in -shire, which no other country names places after.
# "New Hampshire" is the one thing this must not swallow.
_SHIRE = re.compile(r"(?<!new )\b[a-z]{3,}shire\b")

# UK towns and counties. The person running this filters on countries: [UK],
# so a UK town that does not resolve is a role they never see. BambooHR sends
# no country at all for office and hybrid roles and Reed sends free text, so
# "Farnborough", "Stoke-on-Trent" and "Cambridgeshire" all arrived unknown.
#
# Names shared with a US place of comparable size are deliberately absent:
# Durham, Norfolk, Lincoln, Portsmouth, Worcester, Gloucester, Dover,
# Windsor, Peterborough, Salisbury, Winchester, Richmond, Lancaster,
# Carlisle, Greenwich and Camden stay unresolved rather than guessed, because
# a role filed under the wrong country is worse than one filed under none.
_UK_PLACES = (
    "farnborough", "stoke-on-trent", "stoke on trent", "aberdeen", "dundee",
    "inverness", "stirling", "swansea", "wrexham", "londonderry",
    "lisburn", "wolverhampton", "sunderland", "middlesbrough",
    "huddersfield", "bradford", "wakefield", "doncaster", "rotherham",
    "barnsley", "grimsby", "scunthorpe", "kingston upon hull",
    "harrogate", "chesterfield", "mansfield", "loughborough",
    "burton upon trent", "tamworth", "solihull", "redditch", "dudley",
    "walsall", "west bromwich", "telford", "shrewsbury", "stafford",
    "crewe", "chester", "warrington", "wigan", "bolton", "oldham",
    "rochdale", "stockport", "salford", "blackburn", "blackpool", "preston",
    "seaham", "gateshead", "darlington", "hartlepool", "cheltenham",
    "basingstoke", "aldershot", "camberley", "farnham", "woking",
    "guildford", "bracknell", "wokingham", "maidenhead", "slough",
    "high wycombe", "aylesbury", "bedford", "luton", "watford", "stevenage",
    "hemel hempstead", "st albans", "welwyn garden city", "hatfield",
    "basildon", "chelmsford", "colchester", "crawley", "horsham", "reigate",
    "redhill", "epsom", "croydon", "bromley", "ealing", "wembley",
    "uxbridge", "twickenham", "kingston upon thames", "wimbledon",
    "islington", "southwark", "lambeth", "hackney", "canary wharf",
    "shoreditch", "farringdon", "bournemouth", "poole", "weymouth",
    "eastbourne", "hastings", "margate", "folkestone", "maidstone",
    "tunbridge wells", "yeovil", "taunton", "torquay", "truro", "newquay",
    "barnstaple", "chippenham", "trowbridge", "eastleigh", "fareham",
    "gosport", "havant", "newbury", "didcot", "abingdon", "bicester",
    "kettering", "corby", "wellingborough", "nuneaton", "worksop",
    "grantham", "skegness", "beverley", "scarborough", "castleford",
    "pontefract", "halifax", "keighley", "skipton", "burnley", "accrington",
    "chorley", "leyland", "st helens", "birkenhead", "runcorn", "widnes",
    "macclesfield", "congleton", "buxton", "matlock", "belper",
    "livingston", "motherwell", "paisley", "kilmarnock", "falkirk",
    "dunfermline", "kirkcaldy", "cumbernauld",
    # counties and regions, which Reed and BambooHR send instead of a town
    "north yorkshire", "west yorkshire", "south yorkshire", "east yorkshire",
    "greater london", "greater manchester", "merseyside", "tyne and wear",
    "west midlands", "east midlands", "cornwall", "devon", "dorset",
    "somerset", "surrey", "west sussex", "east sussex", "kent", "essex",
    "suffolk", "northumberland", "cumbria", "county durham", "rutland",
    "midlothian", "lothian", "fife", "aberdeenshire", "isle of wight",
    "anglesey", "gwynedd", "powys", "ceredigion", "county antrim",
    "county down", "county armagh", "county tyrone", "county fermanagh",
    "home counties",
)

# German towns. The mojibake above was mostly German cities, and the
# non-mangled long tail (Hannover, Bremen, Aachen, Dresden, Karlsruhe) is the
# same story: the list stopped at the ten biggest.
_DE_PLACES = (
    "hannover", "bremen", "aachen", "dresden", "braunschweig", "augsburg",
    "münster", "muenster", "karlsruhe", "essen", "erfurt", "bonn", "mainz",
    "darmstadt", "mannheim", "bielefeld", "dortmund", "duisburg", "kiel",
    "magdeburg", "siegen", "paderborn", "lübeck", "luebeck", "wiesbaden",
    "bochum", "wuppertal", "chemnitz", "rostock", "kassel", "hagen",
    "saarbrücken", "saarbruecken", "freiburg", "regensburg", "ingolstadt",
    "heidelberg", "würzburg", "wuerzburg", "heilbronn", "osnabrück",
    "osnabruck", "oldenburg", "solingen", "krefeld", "potsdam", "jena",
    "göppingen", "goeppingen", "landshut", "hildesheim", "bayreuth",
    "gütersloh", "guetersloh", "ludwigshafen", "leverkusen", "offenbach",
    "koblenz", "bergisch gladbach", "recklinghausen", "remscheid", "trier",
    "salzgitter", "cottbus", "zwickau", "iserlohn", "schwerin", "gießen",
    "giessen", "flensburg", "villingen-schwenningen", "konstanz", "worms",
    "marburg", "delmenhorst", "bamberg", "aschaffenburg", "lüneburg",
    "lueneburg", "sindelfingen", "fellbach", "böblingen", "friedrichshafen",
    "ravensburg", "esslingen", "reutlingen", "tübingen", "tuebingen",
    "pforzheim", "metzingen", "riederich", "nordrhein-westfalen",
    "north rhine-westphalia", "baden-württemberg", "baden-wurttemberg",
    "niedersachsen", "sachsen-anhalt", "rheinland-pfalz",
    "schleswig-holstein", "mecklenburg-vorpommern",
)

# US cities that turned up unresolved on their own with no state attached:
# "Detroit", "Dallas", "NYC", "Miami", "Houston", "Philadelphia". Each is the
# only place of that size in the world with the name, which is the bar.
_US_PLACES = (
    "detroit", "dallas", "houston", "philadelphia", "phoenix", "miami",
    "san diego", "san mateo", "santa clara", "el segundo", "redwood city",
    "berkeley", "salt lake city", "indianapolis", "milwaukee",
    "kansas city", "st. louis", "saint louis", "cincinnati", "cleveland",
    "pittsburgh", "baltimore", "nashville", "charlotte", "raleigh",
    "greensboro", "tampa", "orlando", "jacksonville", "new orleans",
    "oklahoma city", "albuquerque", "tucson", "sacramento", "san antonio",
    "fort worth", "las vegas", "minneapolis", "des moines", "omaha",
    "wichita", "olathe", "buffalo", "syracuse", "hartford", "providence",
    "boise", "anchorage", "honolulu", "irvine", "pasadena", "long beach",
    "oakland", "fremont", "santa monica", "culver city", "burbank",
    "scottsdale", "chandler", "plano", "irving", "bethesda", "reston",
    "mclean", "brooklyn", "manhattan", "nyc", "new york city", "boston",
)

# Cities elsewhere that the data threw up, each the only place of any size
# with that name. Where a name is shared (Santiago, Lima, Athens, Cordoba)
# the entry carries its country with it or is left out entirely.
_MORE_CITIES = {
    "CA": ("montreal", "montréal", "calgary", "edmonton", "winnipeg",
           "mississauga", "brampton", "hamilton ontario", "kitchener",
           "burnaby", "saskatoon", "regina", "sherbrooke", "laval",
           "brossard", "etobicoke", "trois-rivières", "trois-rivieres",
           "saguenay", "oakville", "markham", "vaughan", "richmond hill",
           "gatineau", "kelowna", "st. john's", "moncton", "québec city",
           "quebec city"),
    "TW": ("taipei", "taichung", "kaohsiung", "taoyuan", "tainan", "hsinchu"),
    "UA": ("kyiv", "kiev", "lviv", "kharkiv", "odesa", "dnipro"),
    "BG": ("sofia", "plovdiv", "varna", "burgas"),
    "HU": ("budapest", "debrecen", "szeged"),
    "LT": ("vilnius", "kaunas", "klaipeda", "klaipėda", "panevėžys"),
    "LV": ("riga", "rīga"),
    "EE": ("tallinn", "tartu"),
    "RS": ("belgrade", "beograd", "novi sad"),
    "HR": ("zagreb", "rijeka", "osijek"),
    "SI": ("ljubljana", "maribor"),
    "SK": ("bratislava", "kosice", "košice"),
    "GR": ("athens", "thessaloniki", "patras", "heraklion", "marousi"),
    "CY": ("nicosia", "limassol", "larnaca", "paphos", "latsia"),
    "MT": ("valletta", "sliema", "birkirkara", "gozo", "st julian's"),
    "SA": ("riyadh", "jeddah", "dammam", "khobar", "al khobar", "makkah",
           "ad dammam", "al hufuf"),
    "QA": ("doha", "al khor", "lusail"),
    "KW": ("kuwait city", "al ahmadi", "al-ahmadi"),
    "BH": ("manama",),
    "OM": ("muscat", "sohar", "salalah"),
    "EG": ("cairo", "giza", "suez"),
    "MA": ("casablanca", "rabat", "marrakech", "tangier"),
    "NG": ("lagos", "abuja", "port harcourt", "ibadan"),
    "KE": ("nairobi", "mombasa", "eldoret"),
    "PK": ("karachi", "lahore", "islamabad", "rawalpindi", "faisalabad"),
    "LK": ("colombo", "kandy"),
    "BD": ("dhaka", "chittagong", "cox's bazar"),
    "NP": ("kathmandu", "pokhara"),
    "KZ": ("almaty", "astana", "nur-sultan", "shymkent"),
    "GE": ("tbilisi", "t'bilisi", "batumi", "kutaisi"),
    "AM": ("yerevan",),
    "AZ": ("baku",),
    "CO": ("bogota", "bogotá", "medellin", "medellín", "barranquilla",
           "cartagena"),
    "PE": ("lima peru", "arequipa", "chorrillos", "callao"),
    "CL": ("santiago de chile", "valparaiso", "valparaíso", "vitacura",
           "puerto montt", "coyhaique", "antofagasta", "maipú"),
    "UY": ("montevideo",),
    "CR": ("san jose costa rica", "heredia", "cartago"),
    "SV": ("san salvador",),
    "GT": ("guatemala city",),
    "PA": ("panama city",),
    "DO": ("santo domingo",),
    "VN": ("hanoi", "ho chi minh", "hcmc", "da nang", "haiphong"),
    "MM": ("yangon", "naypyidaw"),
    "KH": ("phnom penh",),
    "LU": ("luxembourg city", "esch-sur-alzette", "rodange"),
    "MU": ("port louis", "grand baie", "ebene", "ebène"),
    "MV": ("malé",),
    "IN": ("delhi", "new delhi", "gurugram", "chennai", "kolkata",
           "ahmedabad", "jaipur", "kochi", "coimbatore", "nashik", "indore",
           "chandigarh", "thiruvananthapuram", "trivandrum", "vadodara",
           "surat",
           "nagpur", "bhubaneswar"),
    "NL": ("amersfoort", "hoofddorp", "arnhem", "breda", "tilburg",
           "nijmegen", "haarlem", "leiden", "zwolle", "apeldoorn", "almere",
           "amstelveen", "dordrecht", "deventer", "heerenveen", "gorinchem",
           "naaldwijk", "boxmeer", "oosterhout", "wageningen", "terneuzen",
           "'s-hertogenbosch", "den bosch", "alkmaar", "enschede", "venlo",
           "roosendaal", "helmond", "hilversum", "veenendaal", "dongen",
           "etten-leur",
           # Missing outright, and each the only place of that name of any
           # size. "Maastricht" was the visible one: the sixth city of the
           # Netherlands, with a university and a tech scene, and a posting
           # there was dropped as "location not recognised" for anyone whose
           # countries list said NL.
           "maastricht", "leeuwarden", "heerlen", "sittard", "zoetermeer",
           "hengelo", "zaandam", "purmerend", "middelburg", "delfzijl"),
    "BE": ("bruges", "brugge", "wavre", "machelen", "mechelen", "hasselt",
           "namur", "charleroi", "liege", "liège", "kortrijk", "aalst"),
    "AT": ("linz", "innsbruck", "klagenfurt", "villach", "wels",
           "sankt pölten", "steiermark", "styria", "carinthia",
           "oberösterreich", "wolfsberg"),
    "CH": ("bern", "berne", "winterthur", "lucerne", "luzern", "st. gallen",
           "lugano", "thun", "neuchâtel", "neuchatel", "bioggio"),
    "FR": ("annecy", "la rochelle", "rennes", "montpellier", "grenoble",
           "dijon", "angers", "clermont-ferrand", "avignon",
           "aix-en-provence", "aix en provence", "levallois-perret",
           "issy-les-moulineaux", "rueil-malmaison", "courbevoie",
           "puteaux", "saint-ouen-sur-seine", "villeurbanne",
           "saint-priest", "blanquefort", "boulogne-billancourt",
           "nanterre", "créteil", "versailles", "strasbourg", "toulon",
           "reims", "le havre", "saint-étienne", "limoges", "amiens",
           "metz", "besançon", "perpignan", "orléans", "mulhouse", "caen",
           "occitanie", "nouvelle-aquitaine", "auvergne-rhône-alpes",
           "île-de-france", "hauts-de-france", "bretagne",
           "provence-alpes-côte d'azur", "centre-val de loire"),
    "ES": ("sevilla", "murcia", "las palmas", "alicante", "córdoba",
           "valladolid", "vigo", "gijón", "granada", "tarragona",
           "pamplona", "san sebastian", "donostia", "catalunya", "cataluña",
           "andalucia", "andalucía", "castilla la mancha"),
    "IT": ("genova", "palermo", "catania", "venezia", "verona", "padova",
           "trieste", "brescia", "parma", "modena", "reggio emilia",
           "perugia", "lazio", "lombardia", "piemonte", "veneto", "toscana",
           "friuli-venezia giulia"),
    "PL": ("katowice", "lublin", "bydgoszcz", "szczecin", "rzeszow",
           "rzeszów", "bialystok", "białystok", "gdynia", "torun", "toruń"),
    "PT": ("guimaraes", "guimarães", "leiria", "setubal", "setúbal"),
    "RO": ("bucharest", "bucuresti", "bucurești", "cluj-napoca",
           "timisoara", "timișoara", "iasi", "iași", "constanta",
           "constanța", "brasov", "brașov"),
    "SE": ("uppsala", "linkoping", "linköping", "vasteras", "västerås",
           "orebro", "örebro", "karlstad", "umea", "umeå", "lund"),
    "NO": ("stavanger", "tromso", "tromsø", "drammen", "kristiansand"),
    "DK": ("aalborg", "esbjerg", "roskilde", "kolding", "vejle", "horsens"),
    "FI": ("turku", "oulu", "vantaa", "jyvaskyla", "jyväskylä"),
    "CZ": ("ostrava", "olomouc", "plzen", "plzeň", "liberec",
           "mladá boleslav"),
    "IE": ("waterford", "kilkenny", "athlone", "sligo", "drogheda",
           "dundalk"),
    "IL": ("jerusalem", "haifa", "herzliya", "ra'anana"),
    "TR": ("ankara", "izmir", "bursa", "antalya", "gaziantep", "adana"),
    "ZA": ("pretoria", "durban", "gqeberha", "stellenbosch", "tembisa"),
    "AU": ("adelaide", "canberra", "hobart", "gold coast", "newcastle nsw",
           "wollongong", "geelong", "townsville", "moruya"),
    "NZ": ("christchurch", "dunedin", "tauranga", "napier", "invercargill"),
    "MX": ("guadalajara", "monterrey", "queretaro", "querétaro", "puebla",
           "tijuana", "cancun", "cancún", "merida", "mérida", "zapopan",
           "ciudad de méxico", "ciudad de mexico", "cdmx", "mexico city"),
    "BR": ("brasilia", "brasília", "belo horizonte", "curitiba",
           "porto alegre", "recife", "fortaleza", "campinas",
           "florianopolis", "florianópolis", "manaus", "goiania",
           "goiânia", "são bernardo do campo"),
    "AR": ("cordoba argentina", "rosario", "mendoza"),
    "JP": ("osaka", "yokohama", "nagoya", "fukuoka", "sapporo", "kyoto",
           "kobe", "sendai"),
    "KR": ("busan", "incheon", "daegu", "daejeon", "pangyo", "seongnam"),
    "CN": ("guangzhou", "hangzhou", "chengdu", "wuhan", "suzhou", "nanjing",
           "tianjin", "qingdao", "dalian"),
    "TH": ("chiang mai", "rayong", "chonburi", "phuket"),
    "MY": ("penang", "johor bahru", "shah alam", "petaling jaya",
           "cyberjaya", "selangor"),
    "ID": ("surabaya", "bandung", "medan", "yogyakarta", "tangerang",
           "bekasi"),
    "PH": ("makati", "taguig", "quezon city", "cebu", "pasig",
           "bonifacio global city", "mandaluyong", "davao"),
    "TN": ("tunis", "sousse", "sfax", "monastir", "zaghouan"),
    "DZ": ("algiers",),
    "GH": ("accra",),
    "TZ": ("dar es salaam",),
    "UZ": ("tashkent",),
    "KG": ("bishkek",),
    "MD": ("chisinau", "chișinău"),
}

# Fold the new lists into the existing city table. Appending rather than
# replacing keeps every hand-tuned guard already in there ("london" not
# followed by Ontario, "paris" not followed by TX) exactly as it was.
_CITY_HINTS["UK"] += "|" + _alt(_UK_PLACES)
_CITY_HINTS["DE"] += "|" + _alt(_DE_PLACES)
_CITY_HINTS["US"] += "|" + _alt(_US_PLACES)
for _code, _names in _MORE_CITIES.items():
    if _code in _CITY_HINTS:
        _CITY_HINTS[_code] += "|" + _alt(_names)
    else:
        _CITY_HINTS[_code] = _alt(_names)


def _country_of(location: str, *, cities: bool = True) -> str | None:
    """Best single guess at the country a location string refers to.

    Tiered deliberately: an explicit country code or name beats a US state
    code beats a city name. Returns None when nothing identifies it, which
    callers treat as unknown rather than as a match, and None stays a real
    answer: "Remote", "2 Locations" and "EMEA" name no country, and 17% of
    everything this file cannot place is of exactly that kind.

    `cities=False` drops the tiers that answer on city evidence alone, so the
    result means "this string names a country" rather than "we can attribute
    this string to a country". Callers that add a country to a location need
    that narrower question: see `names_a_country`.
    """
    if not location:
        return None
    raw = _repair(location)
    low = raw.lower()
    # Accents folded, tried alongside the unfolded text rather than instead
    # of it. "Montréal, QC, ca" and "Košice, Slovakia" need the folded form;
    # "München" and "Kraków" need the unfolded one.
    fold = _fold(low)

    def hit(pat: str) -> bool:
        return bool(re.search(pat, low) or (fold != low and re.search(pat, fold)))

    # An explicit lowercase country code at the end of the string is the most
    # specific signal a posting can carry: the employer named the country
    # outright, in a field of its own. It goes first because the city tier
    # would get it wrong, not merely miss it: "Newcastle, au" is Australia
    # and the UK city list would claim it.
    cc = _TRAILING_CC.search(raw)
    if cc:
        code = _code_country(cc.group(1))
        if code and code not in _AMBIGUOUS_CC:
            return code
        if code:
            # One that is also a US state code, so the same rule applies
            # as for the uppercase form: something else has to name that
            # country. A province or Land does it ("Whitecourt, AB, ca"),
            # and so does a city ("Chennai, in"). Nothing does it for
            # "Savannah, ga", so Gabon is not claimed and it stays unknown.
            sub = _SUBNATIONAL.get(code)
            if sub and (re.search(sub[0], raw) or re.search(sub[1], fold)):
                return code
            city = _CITY_HINTS.get(code)
            if city and hit(city):
                return code

    # A named foreign subdivision beats a bare city name, because a city name
    # is the weaker signal of the two: plenty of them exist twice. "Newcastle,
    # New South Wales" resolved to the UK even after "New South Wales" stopped
    # matching the Wales marker, because Newcastle is a UK city hint and
    # nothing said Australia. A state or province is named by exactly one
    # country, so when one is present it decides.
    for code, sub in _SUBNATIONAL.items():
        if re.search(sub[1], fold):
            return code

    for code, pat in _COUNTRY_MARKERS.items():
        if hit(pat):
            return code

    # Georgia the country before Georgia the state, and only ever on the
    # evidence of a Georgian city. "Tbilisi, Georgia" used to resolve to US,
    # because the spelled-out state rule matched the last word of it.
    if cities and _GEORGIA_COUNTRY.search(fold):
        return "GE"

    # Twenty state codes are also country codes. "Berlin, DE" read as Delaware
    # and "Toronto, CA" as California, so a German role and a Canadian one
    # both arrived filed as US, a country the user may need a visa for.
    #
    # Simply letting the city win instead is worse: "Birmingham, AL" and
    # "Reading, PA" would go to the UK city list, which is the bug that once
    # marked 59 of 296 American roles as British. So the code counts as a
    # country only when the city independently names that same country.
    # Berlin corroborates DE, Toronto corroborates CA, Birmingham does not
    # corroborate Albania.
    amb = _AMBIGUOUS_STATE.search(raw)
    if amb:
        code = amb.group(1).upper()
        pat = _CITY_HINTS.get(code)
        if pat and hit(pat):
            return code
    if (_US_STATE.search(raw) or _US_STATE_NAME.search(low)
            or _US_STATE_ALONE.match(low) or _US_STATE_SPACED.search(raw)):
        return "US"
    # A Canadian province names Canada as plainly as a US state names the US,
    # and nothing read them: "Winnipeg, Manitoba", "Brampton, ON" and
    # "Calgary, AB" all arrived unknown.
    if _CA_PROVINCE.search(raw) or _CA_PROVINCE_NAME.search(fold):
        return "CA"
    # Taleo's hyphen hierarchy, biggest first. "IL-Northbrook" and
    # "TX-Plano Legacy" are US states; "PH-National Capital-Quezon City" is a
    # country. The state reading is tried first because that is what the data
    # is: 258 postings arrive in this shape and almost all of them are US.
    head = _TALEO_HEAD.match(raw)
    if head:
        code = head.group(1)
        if code in _US_STATE_CODES:
            return "US"
        named = _code_country(code)
        if named:
            return named
    # A country written out in full, as its own comma-separated segment.
    # Read back to front, because a location hierarchy puts the country last:
    # "Benin, Nigeria" is the city of Benin in Nigeria, not the country of
    # Benin. This is where Qatar, Saudi Arabia, Taiwan, Colombia, Ukraine and
    # forty other countries nobody had typed in yet get recognised.
    for seg in reversed(re.split(r"[,/;|]|\s+[-–—]\s+", fold)):
        code = _COUNTRY_NAME.get(seg.strip(" .()"))
        if code:
            return code
    # A county ending in -shire is British and nothing else. Cambridgeshire
    # and Lincolnshire both arrived unknown, and Reed sends counties rather
    # than towns for a large share of its adverts.
    if _SHIRE.search(fold):
        return "UK"
    if cities:
        for code, pat in _CITY_HINTS.items():
            if hit(pat):
                return code
    return None


def names_a_country(location: str) -> bool:
    """Does this string explicitly NAME a country?

    Not the same question as "can we work out which country this is", and the
    difference is a whole country's worth of listings. `_countries_in` answers
    on city evidence too, so it says yes to a bare "Perth" (Australia) and a
    bare "Boston" (United States). `_reed_location` asked it whether to append
    ", United Kingdom" to a location typed into a UK-only job site, so Perth in
    Scotland was filed as Australian and Boston in Lincolnshire as American,
    and both then vanished for anyone with `countries: [UK]`.

    A city hint must not suppress the suffix; only a country marker may. The
    case the suffix has to keep protecting is a real overseas listing that
    names its country outright, "Dublin, Ireland", which must never become
    "Dublin, Ireland, United Kingdom".
    """
    for part in _SPLIT.split(location or ""):
        if _country_of(part, cities=False):
            return True
    return bool(_country_of(location or "", cities=False))


# A comma-separated segment that can only be a qualifier of the place in
# front of it, never a place in its own right: a US state, a Canadian
# province, an Australian state. "Cambridge, MA" is one location and
# "London, New York" is two, and this is what tells them apart -- "MA" is a
# subdivision and nothing else, while "New York" is also a city, so it does
# not swallow the London before it.
_SUBDIVISION_ONLY = re.compile(
    r"^(?:"
    r"[a-z]{2}|"
    r"new south wales|western australia|south australia|"
    r"northern territory|australian capital territory|"
    r"british columbia|newfoundland(?: and labrador)?|nova scotia|"
    r"prince edward island|saskatchewan|manitoba|alberta|quebec|ontario|"
    r"alabama|alaska|arizona|arkansas|colorado|connecticut|delaware|"
    r"florida|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|"
    r"maryland|massachusetts|michigan|minnesota|mississippi|missouri|"
    r"montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    r"north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode island|south carolina|south dakota|tennessee|utah|vermont|"
    r"virginia|west virginia|wisconsin|wyoming|california|texas|hawaii|"
    r"washington"
    r")$", re.I)
# Deliberately absent: "new york" and "georgia". Both are also a city and a
# country respectively, so treating them as pure qualifiers would swallow the
# segment in front of them -- "London, New York" is two cities, not one.


def _looks_like_one_place(segs: list[str]) -> bool:
    """Is this comma list a single address rather than a list of places?

    An address is short and gets broader left to right, ending in the thing
    that identifies the country: "Cambridge, MA", "Toronto, Ontario, Canada",
    "Benin, Nigeria". A list of places does not end that way: "London, New
    York", "Germany, Cyprus, Poland, London, Portugal".

    Getting this wrong in the address direction re-files "Cambridge, MA" as
    British; getting it wrong in the list direction deletes a London vacancy
    that was advertised alongside two American ones.
    """
    if not 2 <= len(segs) <= 3:
        return False
    return _SUBDIVISION_ONLY.match(segs[-1]) is not None \
        or _is_country_name(segs[-1])


def _is_country_name(seg: str) -> bool:
    """Is this whole segment the name of a country?

    Narrower than `_country_of(..., cities=False)` on purpose. That answers
    yes for "New York", because a spelled-out US state names the US, and using
    it here made "London, New York" look like an address and swallowed the
    London.
    """
    low = _fold(_repair(seg or "").lower().strip(" .()"))
    if low in _COUNTRY_NAME:
        return True
    return any(re.fullmatch(pat, low, re.I) for pat in _COUNTRY_MARKERS.values())


# What a region name covers, for postings that name one instead of a country.
#
# "Remote - Europe", "Remote, EU" and "Remote - EMEA" resolved to nothing and
# were dropped as "location not recognised". Bare "Remote" is kept, so adding
# the qualifier that makes a role MORE relevant to a European reader was what
# made it fail. For a reader who wants remote work in the EU, that is their
# best category being binned.
#
# Deliberately not solved by adding these to the "no location given" list.
# That would hand a Europe-only role to a reader in Texas, which is the same
# mistake pointing the other way. A region is a set of countries, so it is
# resolved to one and intersected with the reader's own countries exactly as
# a named country is.
#
# EMEA is treated as Europe plus the Middle East and Africa entries this tool
# knows about, rather than the full list, because the point is only ever
# "does the reader's country fall inside it".
_EUROPE = {"UK", "IE", "FR", "DE", "NL", "BE", "LU", "ES", "PT", "IT", "AT",
           "CH", "SE", "NO", "DK", "FI", "IS", "PL", "CZ", "SK", "HU", "RO",
           "BG", "GR", "HR", "SI", "EE", "LV", "LT", "CY", "MT", "RS", "UA"}
_MEA = {"AE", "SA", "QA", "IL", "TR", "EG", "ZA", "NG", "KE", "MA"}
_APAC = {"AU", "NZ", "SG", "IN", "JP", "CN", "HK", "KR", "MY", "TH", "ID",
         "PH", "VN", "TW", "BD", "PK"}
_NAMER = {"US", "CA", "MX"}
_LATAM = {"BR", "AR", "CL", "CO", "MX", "PE", "UY"}

REGIONS = {
    "europe": _EUROPE, "eu": _EUROPE, "eea": _EUROPE,
    "emea": _EUROPE | _MEA,
    "apac": _APAC, "asia pacific": _APAC, "asia-pacific": _APAC,
    "anz": {"AU", "NZ"},
    "namer": _NAMER, "north america": _NAMER, "nam": _NAMER,
    "latam": _LATAM, "latin america": _LATAM,
    "mena": _MEA, "middle east": _MEA,
}
# Matched on a whole segment, never as a substring. "EU" inside "EUROPE" is
# harmless, but a bare `in` test would also find "eu" in "Deutschland" and
# "nam" in "Vietnam", which is how a location filter starts inventing
# continents.
_REGION_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(map(re.escape, REGIONS), key=len,
                                        reverse=True)) + r")(?![A-Za-z])",
    re.I)


def regions_in(location: str) -> set[str]:
    """Every country covered by a region the posting names. Empty if none."""
    out: set[str] = set()
    for m in _REGION_RE.finditer(location or ""):
        out |= REGIONS[m.group(1).lower()]
    return out


def _countries_in(location: str) -> set[str]:
    """Every country a posting names. Postings routinely list several.

    Commas are read as separators too, but only carefully. A comma usually
    binds a place to its qualifier ("Cambridge, MA"), which is why the
    pipe-and-slash split above exists; but employers also write plain
    comma-separated lists of cities, and reading those as one string returns
    exactly one country. "London, New York", "San Francisco, New York,
    London" and "Germany, Cyprus, Poland, London, Portugal" all came back as
    a single foreign country, so a London vacancy on any of them was dropped
    as being outside the UK.

    A comma segment therefore contributes its own country UNLESS the segment
    after it is a bare subdivision, in which case the pair is one place and
    the subdivision decides.
    """
    found = set()
    for part in _SPLIT.split(location or ""):
        c = _country_of(part)
        if c:
            found.add(c)
        segs = [x.strip() for x in part.split(",") if x.strip()]
        if len(segs) < 2 or _looks_like_one_place(segs):
            continue
        for i, seg in enumerate(segs):
            nxt = segs[i + 1] if i + 1 < len(segs) else ""
            if nxt and _SUBDIVISION_ONLY.match(nxt):
                continue        # "Cambridge" belongs to the "MA" behind it
            if _SUBDIVISION_ONLY.match(seg):
                continue        # a qualifier names no country on its own
            c = _country_of(seg)
            if c:
                found.add(c)
    if not found:
        c = _country_of(location)
        if c:
            found.add(c)
    return found


# Whether a role is remote, hybrid or office-based is rarely a field. Ashby
# and Workable expose it; the rest bury it in prose or omit it. So this reads
# the flag where there is one and the text where there is not, and reports
# "unstated" rather than guessing, which is over half of postings.
_HYBRID = re.compile(
    r"\bhybrid\b|\d\s*days?\s*(?:a week\s*)?(?:in|per week in)\s*(?:the\s*)?office|"
    r"\b\d+\s*days? on[- ]?site", re.I)
_ONSITE = re.compile(
    r"\bon[- ]?site\b|\bin[- ]person\b|\boffice[- ]based\b|100% in office|"
    r"\bfull[- ]?time in the office\b", re.I)
_REMOTE_TXT = re.compile(
    r"\bfully remote\b|\b100% remote\b|\bremote[- ]first\b|\bwork from anywhere\b|"
    r"\bremote\b", re.I)

# Bits that are not a city: countries, regions and the wrapper words postings
# put in front of a place.
_NOT_A_CITY = re.compile(
    r"^(?:remote|hybrid|on[- ]?site|anywhere|global|worldwide|europe|emea|americas?|apac|"
    r"north america|south america|latin america|latam|nationwide|"
    r"home[- ]based|work from home|wfh|"
    r"united kingdom|uk|england|scotland|wales|northern ireland|united states|usa?|"
    r"canada|australia|ireland|germany|france|spain|netherlands|india|singapore|"
    r"various|multiple locations|flexible|tbc|n/?a|"
    # Time zones. Remote-first employers write the zone where a city goes:
    # Junction advertise "GMT / BST (UK, Portugal, Ireland)", which the
    # first-comma-part rule reduces to "GMT" and files as a town. It is a
    # working-hours requirement, not a place, and the country is read from the
    # rest of the string anyway.
    # `wet`, `west` and `art` are left out on purpose: they are ordinary
    # words and "West" is a place name, and this list only has to catch the
    # zones employers actually advertise hours in.
    r"gmt|bst|utc|cet|cest|eet|eest|"
    r"est|edt|cst|cdt|mst|mdt|pst|pdt|akst|hst|"
    r"ist|jst|kst|sgt|aest|aedt|awst|acst|nzst|brt|msk|"
    r"gmt\s*[+-]\s*\d{1,2}|utc\s*[+-]\s*\d{1,2})$", re.I)


# "Remote" with a country named somewhere in the body. Airbnb's "Senior Data
# Scientist" said `Remote` in the location field and "This position is US -
# Remote Eligible" in the description; the tool called it "remote, no country
# named", gave it 20 points for it, and ranked it second for a user who
# cannot work in the US.
_REMOTE_SCOPE = re.compile(
    r"(?:remote|based|located|work|eligible|hire|role is)[^.\n]{0,60}?"
    r"\b(?:in|within|from|across|to)\b[^.\n]{0,40}"
    r"|\b(?:uk|us|usa|united states|united kingdom|eu|europe|emea)\b[\s-]*"
    r"(?:only|based|remote|eligible)", re.I)


def remote_scope(job: Job) -> set[str]:
    """Countries a nominally-location-free remote role is actually limited to.

    Only consulted when the location field names nothing, and only over the
    first part of the description, where employers put this.
    """
    d = (job.description or "")[:3000]
    if not d:
        return set()
    found: set[str] = set()
    for m in _REMOTE_SCOPE.finditer(d):
        found |= _countries_in(m.group(0))
    return found


def work_mode(job: Job) -> str:
    """remote | hybrid | office | unstated.

    Hybrid is checked first on purpose: a posting saying "remote/hybrid" is
    describing a hybrid job, and reading the word "remote" first would file it
    wrongly in the more attractive bucket.
    """
    blob = f"{job.title} {job.location} {(job.description or '')[:2500]}"
    if _HYBRID.search(blob):
        return "hybrid"
    # A structured flag from the platform beats prose. Pinpoint, Breezy and
    # Teamtailor all state the arrangement in a field, and scanning the advert
    # first threw that away: an on-site gym, on-site parking or "occasional
    # on-site visits" anywhere in the body filed a role the ATS had marked
    # remote as office. Prose still decides when no field was set.
    #
    # But the flag is not evidence when the advert contradicts it. Ashby's
    # `isRemote` is true on 52.4% of postings, and 87.2% of those name a
    # physical city and never use the word remote anywhere: measured over
    # 1,316 postings from 30 real boards on 2026-08-27, roles in New York,
    # London, Bogota and Sao Paulo were all being filed as remote. Ashby is
    # the largest platform in the fast pass, so this is most of what a new
    # user sees in their first five minutes, and "remote" is the one label a
    # remote-only reader filters on.
    #
    # So a named town with no mention of remote anywhere wins. That is not a
    # guess about Ashby: it is the advert's own statement of where the work
    # is, against a boolean that no employer had to look at. Anything that
    # says "Remote - US", "London (Remote)" or "Fully remote" still has the
    # word in `blob` and is unaffected, which is every honest use of the flag.
    if job.remote is True and not (city_of(job.location)
                                   and not _REMOTE_TXT.search(blob)):
        return "remote"
    if _ONSITE.search(blob):
        return "office"
    if _REMOTE_TXT.search(blob):
        return "remote"
    return "unstated"


def city_of(location: str) -> str:
    """The town, where a posting names one. Empty when it does not."""
    if not location:
        return ""
    # `;` as well as `|` and `/`. A posting open in several places is written
    # as a list by several boards, and PCSX writes "United Kingdom; Ireland".
    # Without the semicolon the whole list survived the comma split and landed
    # in the city column, where a list of countries sits exactly where a town
    # would and reads as one. Same failure as Workday's "2 Locations".
    first = re.split(r"[|/;]", location)[0]
    first = re.sub(r"^\s*(?:remote|hybrid|on[- ]?site)\s*[-–—:,]\s*", "", first, flags=re.I)
    part = first.split(",")[0].strip(" -–—")
    # Snowflake ship "US-CA-Menlo Park"; LinkedIn ship "London Area". Both are
    # the same city as everyone else's, so normalise rather than splitting the
    # filter into near-duplicates.
    part = re.sub(r"^[A-Z]{2}-[A-Z]{2}-", "", part)
    part = re.sub(r"\s+Bay\s+Area$", "", part, flags=re.I)
    part = re.sub(r"\s+Area$", "", part, flags=re.I)
    # How-you-work words wrapped around the place, in whatever order the
    # employer felt like. `_NOT_A_CITY` below only ever recognised these when
    # the word stood ALONE, and the prefix strip above only when a dash or a
    # comma followed it, so a US board filled the dashboard's city filter with
    # "US Remote", "Remote (US-based)", "Fully Remote", "Anywhere in the US",
    # "NYC Hybrid Available" and "HQ - NYC" as if each were a town. Stripped
    # here rather than added to the list, because the list cannot grow to
    # cover every arrangement of the same four words.
    part = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", part)
    part = re.sub(r"^\s*(?:hq|head office|main office|office)\s*[-–—:]\s*",
                  "", part, flags=re.I)
    # And the same words on the END, which is where American boards put them.
    # The prefix strip above handled "HQ - NYC" and left "San Francisco
    # Office", "New York Office", "NYC Office" and "SF Office" standing as
    # four separate towns in the dashboard's city filter, alongside the plain
    # "San Francisco" and "New York" they are the same place as. On the
    # published US shard that was 252, 207, 100 and 55 roles filed away from
    # the city they are in.
    part = re.sub(r"\s+(?:office|hq|headquarters|head\s+office|campus|site)$",
                  "", part, flags=re.I)
    # "USA - Corona", "USA - New York": a country pinned to the front of its
    # own city. The country is already read from the full location string, so
    # this only ever split one town into two entries.
    part = re.sub(r"^\s*(?:usa|us|uk|gb|can(?:ada)?|aus(?:tralia)?)\s*"
                  r"[-–—:]\s*", "", part, flags=re.I)
    part = re.sub(r"^\s*anywhere\s+(?:in|within)\s+(?:the\s+)?", "", part, flags=re.I)
    part = re.sub(r"\b(?:fully|100%|entirely|primarily|mostly)\s+"
                  r"(?:remote|on[- ]?site|in[- ]?office)\b", "", part, flags=re.I)
    part = re.sub(r"\b(?:remote|hybrid|on[- ]?site|in[- ]?office)\b"
                  r"(?:\s+(?:available|optional|only|first|friendly|working|eligible))?",
                  "", part, flags=re.I)
    part = re.sub(r"\s{2,}", " ", part).strip(" -–—,;:/")
    part = re.sub(r"\s*\b[a-z]{1,2}\d[a-z\d]?\s*\d[a-z]{2}\b\s*$", "", part, flags=re.I)
    if not part or _NOT_A_CITY.match(part) or len(part) > 34:
        return ""
    return part.strip()


# How many days a week this job wants you in an office.
#
# `work_mode` answers remote, hybrid or on-site, and that is not the question
# somebody with a commute actually asks. Measured on a real board: 271 of
# 3,029 postings with a readable description state a required number of office
# days, and of those, 171 had `work_mode: unstated` and **40 were marked
# remote**. Sanity's posting says "3 days per week in the office" and the
# dashboard called it remote. That is a confident wrong answer, which is worse
# than no answer, and it is the decision that ends an application: a role at
# 120,000 with three days in London is a no that a role at 180,000 with three
# days might not be.
#
# Deliberately narrow. Only a stated NUMBER of days counts, because "hybrid"
# on its own is what `work_mode` already says. An optional day is not a
# requirement, so "you are welcome in the office two days a week" is not a
# match while "at least two days a week in the office" is.
_OFFICE_OPTIONAL = re.compile(
    r"\b(?:optional|if you (?:like|prefer|want)|welcome to|you can|as much or "
    r"as little|no requirement|not required|free to)\b", re.I)

_DAY_WORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}

# A bare "office" counts as the anchor, not only "in the office". The first
# draft required the preposition and missed "office at least 3 days per week"
# and "Office / Hybrid (2 days per week", which are both perfectly ordinary
# ways to write it.
_OFFICE_WORD = r"(?:offices?|on[-\s]?site|in[-\s]person|hq)"

_OFFICE_DAYS = re.compile(
    r"(?P<n>one|two|three|four|five|[1-5])\s*\+?\s*days?\s*(?:a|per)\s*week"
    r"[^.]{0,40}?" + _OFFICE_WORD
    + r"|" + _OFFICE_WORD + r"[^.]{0,40}?"
    r"(?P<m>one|two|three|four|five|[1-5])\s*\+?\s*days?\s*(?:a|per)\s*week",
    re.I)


def office_days(text: str) -> int | None:
    """Days a week this posting requires in an office, or None if it does not say.

    Reads the sentence around the match rather than the whole advert, so a
    company that is remote-first and mentions an optional office day is not
    read as requiring one.
    """
    if not text:
        return None
    for m in _OFFICE_DAYS.finditer(text):
        start = text.rfind(".", 0, m.start()) + 1
        end = text.find(".", m.end())
        sentence = text[start:end if end != -1 else len(text)]
        if _OFFICE_OPTIONAL.search(sentence):
            continue
        n = m.group("n") or m.group("m")
        return _DAY_WORD.get(n.lower())
    return None


def stated_work_mode(job: Job) -> tuple[str, int | None]:
    """`work_mode`, with a stated number of office days overriding it.

    Returns the mode and the day count that decided it, or None when the
    advert never gave one.

    This exists because `enrich` applied the override and `match` did not, and
    the two of them are the answer to the same question. `match`'s
    `work_modes` gate called bare `work_mode()`, got "unstated" for a posting
    whose location is just a city, kept it and flagged it "arrangement not
    stated"; `enrich` then read "4 days a week in the office" out of the same
    advert one line later and stored the role as hybrid. 38 of one remote-only
    reader's 472 seeded roles came out that way, 33 of them carrying both
    "arrangement not stated; you asked for remote" and "4 days a week in the
    office" on the same row, which is the tool contradicting itself in two
    adjacent lines of its own output.
    """
    days = office_days(job.description or "")
    if days:
        return ("office" if days >= 5 else "hybrid"), days
    return work_mode(job), None


def enrich(job: Job) -> Job:
    """Fill the derived fields the dashboard filters on."""
    # And here, so the dashboard's country facet can place them. Without it
    # every region-located role was filed under "unknown", and a
    # country-filtered board read "unknown (120), GR (8), multiple (4)".
    #
    # A region resolves to many countries, so it lands in "multiple" rather
    # than picking one, which is what "open across Europe" actually means.
    found = _countries_in(job.location) or regions_in(job.location)
    job.country = job.country or (sorted(found)[0] if len(found) == 1 else
                                  ("multiple" if found else None))
    job.city = city_of(job.location)
    # A stated number of office days outranks whatever `work_mode` guessed.
    # It is the posting saying so in words, against a heuristic over a location
    # string, and 40 postings that say "3 days per week in the office" were
    # being shown as remote.
    job.work_mode, days = stated_work_mode(job)
    if days:
        flag = f"{days} day{'s' if days > 1 else ''} a week in the office"
        if flag not in job.flags:
            job.flags.append(flag)
    rights = work_rights(job)
    if rights and rights not in job.flags:
        job.flags.append(rights)
    # Permanent, contract, or the advert did not say.
    #
    # The employer's own answer wins. Six platforms carry an explicit
    # employment-type field, covering 7,927 of the 17,811 bundled boards, and
    # the adapters set `job.employment` from it. A structured field the
    # employer filled in beats a regex over their prose every time, and the
    # regex only runs where they left it blank.
    #
    # Read here rather than only in an adapter because the text half needs the
    # description, which LinkedIn, Workday and SmartRecruiters supply only
    # after `enrich` has fetched the posting itself.
    #
    # It never drops a role. Employment type is a fact about the job that the
    # reader decides what to do with, and a filter that hid contract work
    # would hide the roles this was built to surface.
    # The order of the three, which is not obvious and is the whole of the
    # judgement here:
    #
    #  1. A title that says contract outright. "Interim Head of Engineering"
    #     and "Engineering Manager (12 Month FTC)" are deliberate acts of
    #     writing, while the structured field next to them is a dropdown with
    #     a default, and an employer who leaves the default alone while
    #     titling the role "Interim" has told you which one they meant.
    #  2. The platform's own field, where they filled it in.
    #  3. The text, for the 9,884 boards whose platform has no such field.
    _stated = job.employment or employment.UNSTATED
    _title, _ev = employment.classify(job.title)
    if _title == employment.CONTRACT:
        job.employment = employment.CONTRACT
    elif _stated != employment.UNSTATED:
        job.employment, _ev = _stated, "stated by the employer"
    else:
        job.employment, _ev = employment.classify(job.title, job.description)
    _f = employment.flag(job.employment, _ev)
    if _f and _f not in job.flags:
        job.flags.append(_f)
    return job


# ---------------------------------------------------------------------------
# Title matching
#
# `titles.include` is a substring test, and a job title is not a substring
# problem. Employers write the same job as "Engineering Manager", "Manager,
# Engineering", "Senior Manager, Software Engineering", "Head of Site
# Reliability Engineering" and "Director, Data Engineering & Architecture",
# and only the first of those contains the phrase anybody would think to type.
#
# Measured on 13,588 postings from 505 bundled boards: of 165 postings a
# person would call engineering leadership, the config in `config.yaml`
# ("engineering manager", "senior engineering manager", "head of
# engineering", "director of engineering") matched 92. The other 73 were
# dropped as "title does not match" and were therefore never seen at all.
#
# So the configured phrase is treated as its significant words, which may
# appear in any order with a couple of qualifiers between them. This layer
# only ever ADDS matches: the substring regex runs first and unchanged, so
# nothing that matched before can stop matching now.
# ---------------------------------------------------------------------------

# Dropped from both the configured term and the title before comparing, so
# "Director of Engineering", "Director, Engineering" and "Engineering
# Director" are one title and not three.
_TITLE_STOP = {"of", "the", "for", "and", "a", "an", "in", "at", "on", "to", "&"}
_TITLE_WORD = re.compile(r"[a-z0-9+#&]+")

# Words that, appearing between or immediately before the parts of a
# configured title, make it a DIFFERENT job rather than a narrower version of
# the same one. "Senior Manager, Data Engineering" is the job; "Senior
# Technical Program Manager - Foundations Engineering" is not, and neither is
# "Product Manager, Engineering Platform". A qualifier that only names the
# discipline ("data", "platform", "mobile", "site reliability") is fine and is
# exactly what this is meant to let through.
_ROLE_SHIFT = {
    "program", "programme", "project", "product", "account", "sales",
    "presales", "marketing", "finance", "financial", "legal", "recruiting",
    "recruitment", "talent", "procurement", "category", "partner",
    "partnerships", "hr", "people", "customer", "business", "brand",
    "community", "event", "events", "facilities", "construction", "land",
    "civil",
}
# How many extra words may sit inside the phrase. Two covers "Head of [Site
# Reliability] Engineering" and "Director, [Data] Engineering & Architecture".
# Three adds almost no real roles and a third more wrong ones.
_TITLE_GAP = 2


def _title_words(text: str) -> list[str]:
    folded = _fold((text or "").lower())
    return [w for w in _TITLE_WORD.findall(folded) if w not in _TITLE_STOP]


def _term_core(term: str) -> list[str]:
    words = _TITLE_WORD.findall(_fold((term or "").lower()))
    return [w for w in words if w not in _TITLE_STOP] or words


def title_matches_loosely(title: str, terms) -> str | None:
    """The configured title said a different way. Returns the term that hit.

    Every significant word of the term has to be present, within a window of
    its own length plus `_TITLE_GAP`, and none of the words that landed inside
    that window (or immediately in front of it) may be one that changes the
    job. Order is free, because "Engineering Director" and "Director of
    Engineering" are the same vacancy.
    """
    words = _title_words(title)
    if not words:
        return None
    where: dict[str, list[int]] = {}
    for i, w in enumerate(words):
        where.setdefault(w, []).append(i)
    for term in terms or ():
        core = _term_core(term)
        # A single-word term is already a substring test and the regex did it.
        if len(core) < 2 or not all(w in where for w in core):
            continue
        for combo in itertools.product(*[where[w] for w in core]):
            if len(set(combo)) != len(core):
                continue
            first, last = min(combo), max(combo)
            if last - first + 1 > len(core) + _TITLE_GAP:
                continue
            near = {words[i] for i in range(first, last + 1)} - set(core)
            # Two words of lead-in, not one. "Business Development Manager,
            # Engineering Services" puts "development" next to the phrase and
            # "business" one step further out, and "business" is the word that
            # says this is a sales job.
            near |= set(words[max(0, first - 2):first])
            if near & _ROLE_SHIFT:
                continue
            return term
    return None


def title_gate(cfg: Config):
    """A callable answering just the title half of `match`.

    Exists so a caller reading a quarter of a million rows off disk can throw
    away the 99% that fail on the title without building a `Job` list first.
    `seed load` materialised every row before screening: 325MB of resident
    memory for a 22,701-role import, so about 2.1GB for a US reader's 151,044.

    Deliberately only the title. `screen.run` opens with `dedupe` across the
    whole set, so filtering on anything that varies between duplicates of the
    same posting would change which one survives. A duplicate of a
    title-matching role matches the same title, so every one of them still
    reaches `dedupe` and the answer is unchanged.

    The gate is read once here rather than per row: `title_include_re`
    recompiles and `title_terms_expanded` re-expands on every call, and this
    is called once per posting.
    """
    inc, exc = cfg.title_include_re(), cfg.title_exclude_re()
    terms = cfg.title_terms_expanded()

    def keep(title: str) -> bool:
        title = title or ""
        if exc and exc.search(title):
            return False
        if inc and not inc.search(title):
            return bool(title_matches_loosely(title, terms))
        return True

    return keep


def match(job: Job, cfg: Config) -> tuple[bool, str]:
    """Title and location gate. Returns (keep, reason_if_dropped)."""
    inc, exc = cfg.title_include_re(), cfg.title_exclude_re()
    title = job.title or ""

    if exc and exc.search(title):
        return False, "title excluded"
    if inc and not inc.search(title):
        if not title_matches_loosely(title, cfg.title_terms_expanded()):
            return False, "title does not match"

    loc = (job.location or "").strip()
    allowed = set(cfg.countries) | set(cfg.relocate_to)

    loc_exc = cfg.location_exclude_re()
    if loc_exc and loc and loc_exc.search(loc):
        # Exclusion has to work per location, not against the whole string.
        # Asking whether any wanted COUNTRY appears meant "London" cancelled
        # its own exclusion for anyone with countries: [UK], so the single
        # most load-bearing filter a UK user writes did nothing at all.
        parts = [x.strip() for x in _SPLIT.split(loc) if x.strip()] or [loc]
        survivors = [x for x in parts if not loc_exc.search(x)]
        if not survivors:
            return False, f"location excluded ({loc})"
        # Judge the rest of the rules on what is left after the exclusion.
        loc = " / ".join(survivors)

    if not allowed and not cfg.remote_ok:
        # remote_ok lived entirely inside the country branch, so with no
        # countries set it was dead code and "no, I do not want remote roles"
        # changed nothing at all.
        if job.remote is True or _GENERIC_REMOTE.match(loc or ""):
            return False, "remote role and remote is off"

    if allowed:
        # "Remote" on its own means the employer has not named a country, so
        # take them at their word. "Remote - US" has named one, and being
        # remote does not make a US role open to someone outside the US.
        generic = not loc or bool(_GENERIC_REMOTE.match(loc))
        if generic:
            # Before taking "Remote" at face value, check whether the body
            # names a country. A role restricted to the US is a US role,
            # however the location field is written.
            scope = remote_scope(job)
            if scope and not (scope & allowed):
                return False, (f"remote but restricted to "
                               f"{', '.join(sorted(scope))}")
            # Answering "no" to "include fully remote roles" used to change
            # nothing: only a completely empty location was dropped, so every
            # posting that actually said "Remote" came through.
            if not cfg.remote_ok:
                return False, ("remote role and remote is off" if loc
                               else "no location given and remote is off")
            # This used to `return True` here, which walked straight past the
            # `work_modes` gate at the bottom of this function. A location of
            # bare "Remote" was therefore the one way to make `work_modes:
            # [remote]` inert, and it is the commonest location string there
            # is. Jump App's "Sr. UX Designer (US)" was stored for a
            # remote-only reader with `work_mode: hybrid` on the row and
            # "remote, body says US" in its reasons, because the advert says
            # "Remote or hybrid" and nothing ever asked. Fall through instead.
        else:
            if not cfg.remote_ok and work_mode(job) == "remote" \
                    and not city_of(loc):
                # Was `job.remote is True`, which only ever caught a platform
                # flag and a bare "Remote". With `countries: [US]` and
                # `remote_ok: false`, "Remote - US", "US Remote", "Remote
                # (US)" and "Fully Remote - United States" were all kept, and
                # US employers write it that way almost every time, so the
                # setting was close to inert exactly where an American reader
                # needs it.
                #
                # `city_of` is what keeps this honest. A posting listing "New
                # York, Denver, Remote, San Francisco" is a role with offices,
                # and somebody who said no to remote work still wants it: they
                # are asking not to work from home, not asking to be hidden
                # from employers who let other people. Only a location that
                # names no office at all is a remote-only role.
                return False, "remote role and remote is off"

            found = _countries_in(loc)
            if not found:
                # A region is a set of countries, not an unrecognised place.
                found = regions_in(loc)
            if not found:
                return False, (f"location not recognised ({loc})"
                               if loc.strip() else "no location given")
            if not (found & allowed):
                return False, f"{loc} outside target countries"

    if cfg.work_modes:
        # An arrangement the reader did not ask for is dropped. One the
        # posting never stated is KEPT and flagged, because half of all
        # postings do not say, and reading "we cannot tell" as "not remote"
        # would hide more real remote roles than it removed office ones. The
        # reader can see at a glance which is which.
        # `stated_work_mode`, not `work_mode`: a posting that says "4 days a
        # week in the office" in its advert has stated its arrangement, and
        # reading only the location string called that "unstated" and kept it.
        mode, _days = stated_work_mode(job)
        if mode == "unstated":
            job.flags.append(
                f"arrangement not stated; you asked for "
                f"{', '.join(cfg.work_modes)}")
        elif mode not in cfg.work_modes:
            return False, f"{mode} role and you asked for " \
                          f"{', '.join(cfg.work_modes)}"

    return True, ""


# Whether the employer will sponsor a work visa is, for anyone without the
# right to work in the country, the single fact that decides the application.
# Nothing in the tool had any concept of it: a posting saying "we are unable
# to provide visa sponsorship" and one saying "we can sponsor visas" scored
# identically and looked identical, and the only place that noticed was a
# paid screen, one role at a time.
_NO_SPONSOR = re.compile(
    r"(?:unable|not able|cannot|can.t|do not|don.t|will not|won.t)\s+"
    r"(?:to\s+)?(?:provide|offer|support|sponsor)\w*\s*"
    r"(?:\w+\s+){0,3}?(?:visa|sponsorship|work permit)"
    r"|no\s+(?:visa\s+)?sponsorship"
    r"|without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship"
    r"|sponsorship\s+is\s+not\s+(?:available|offered|provided)"
    # "sponsor" used as a bare verb, with no visa/sponsorship noun after it.
    # The alternative above requires that noun, so "we do not sponsor work
    # passes", "unable to sponsor candidates" and the standard US boilerplate
    # "Employer will not sponsor applicants for this position" all fell
    # through it -- and then matched _WILL_SPONSOR, whose "(?:can|able to|
    # will|do)\s+(?:\w+\s+){0,2}?sponsor" happily reads the negated form.
    # Six of eight real refusal phrasings came back "sponsorship offered",
    # were flagged as such to a reader who needs a visa, and collected the
    # +12 "says it will sponsor" score in `rank_one`. The exact opposite of
    # what the posting says, on the one fact that decides the application.
    r"|(?:unable|not\s+able|cannot|can.t|do(?:es)?\s+not|do(?:es)?n.t|"
    r"will\s+not|won.t|not\s+in\s+a\s+position)\s+(?:to\s+)?sponsor\w*"
    # The bar stated as a requirement rather than as a refusal. Outside the UK
    # and the US that is the normal wording, and none of it mentions the word
    # sponsorship at all:
    #
    #   Applicants must be Singapore Citizens or Permanent Residents.
    #   Candidates must already hold a valid Employment Pass.
    #   You must hold a valid UAE residency visa and an NOC.
    #
    # All three read as "not stated", so a reader who needs a visa was shown
    # roles they cannot apply for, with nothing to tell them apart from the
    # ones they can. The gate itself is properly config-driven; it was the
    # vocabulary that was UK and US only.
    #
    # Tied to a requirement verb on purpose. "citizens or permanent residents"
    # on its own appears in equal-opportunity boilerplate on adverts that
    # sponsor perfectly happily, and matching that would hide the roles this
    # reader most needs.
    # The filler is one word, not three, and it has to be a nationality or a
    # place. `(?:\w+\s+){0,3}?` let any three words sit between the verb and
    # the noun, so "You must be a good corporate citizen and a team player"
    # was read as a bar on working here and the role was hidden from the
    # reader it was aimed at. A citizenship requirement names a country: "must
    # be a US citizen", "must be Singapore Citizens or Permanent Residents".
    # "a good corporate citizen" names none.
    r"|(?:must|need\s+to|required\s+to)\s+(?:be\s+)?"
    r"(?:a\s+|an\s+)?(?:[A-Z]\w+|\w+ese|\w+ish|\w+ian|\w+can|eu|uk|us|"
    r"british|american|irish|dutch|german|french|spanish|indian|chinese|"
    r"singaporean|emirati|swiss|canadian|australian)?\s*"
    r"(?:citizens?|permanent\s+residents?|nationals?)\b"
    r"|(?:must|need\s+to|required\s+to|should)\s+(?:already\s+)?"
    r"(?:hold|have|possess)\s+(?:a\s+|an\s+|the\s+)?(?:\w+\s+){0,3}?"
    r"(?:employment\s+pass|work\s+pass|residency\s+visa|work\s+visa|"
    r"work\s+permit|right\s+to\s+work)"
    r"|must\s+(?:already\s+)?have\s+(?:the\s+)?(?:full\s+)?rights?\s+to\s+work"
    r"|full\s+rights?\s+to\s+work", re.I)

# US export control. A separate barrier from a visa and it stacks on top of
# one: EAR and ITAR restrict releasing controlled technology to foreign
# persons, so a role can be perfectly willing to sponsor and still be closed.
# Datadog's posting carries this in its standard footer and says nothing at
# all about sponsorship, so reading only for the word "sponsor" reported a
# clean bill of health on a role with two immigration problems.
_EXPORT_CONTROL = re.compile(
    r"export control|\bITAR\b|\bEAR\b(?!\w)|"
    r"eligible for any required authoriz|"
    r"(?:must be|require[sd]?)\s+(?:a\s+)?(?:US|U\.S\.)\s+(?:person|citizen)|"
    r"security clearance|\bSC clearance\b|developed vetting|"
    r"\bDV cleared\b|baseline personnel security", re.I)

_WILL_SPONSOR = re.compile(
    r"(?:can|able to|will|do|happy to|willing to)\s+(?:\w+\s+){0,2}?sponsor"
    r"|(?:visa|sponsorship)\s+(?:support\s+)?(?:is\s+)?(?:available|offered|provided)"
    r"|we\s+(?:offer|provide)\s+(?:visa\s+)?sponsorship"
    # "Visa sponsorship and relocation are provided" read as not stated,
    # because the alternative above wants the noun next to the verb and this
    # puts a whole clause between them. A miss in the safe direction, but a
    # miss: it is an employer saying yes, to the reader who needs the answer.
    r"|(?:visa\s+)?sponsorship\s+and\s+relocation\s+(?:is|are)\s+"
    r"(?:available|offered|provided)"
    r"|relocation\s+and\s+visa", re.I)


def work_rights(job: Job) -> str:
    '''"no sponsorship", "sponsorship offered", or "" for not stated.

    Read from the description the tool has already downloaded. Reported, never
    used to drop a role: most people running this already have the right to
    work where they are looking, and for them it is noise rather than a filter.
    '''
    d = job.description or ""
    if not d:
        return ""
    if _NO_SPONSOR.search(d) and not _all_mentions_incidental(_NO_SPONSOR, d):
        # Omnea's adverts carry a paragraph naming "UNABLE TO PROVIDE VISAS"
        # as an EXAMPLE of the kind of hard requirement a posting might have.
        # It is not this posting's policy, and it hid 13 of the 77 US roles a
        # sponsorship filter dropped in a 13,588-posting sample.
        return "no sponsorship"
    # Checked before the willing case: a role that sponsors and is also export
    # controlled is still one you may not be allowed to do, and reporting
    # "sponsorship offered" on it would be the more reassuring half of the
    # truth.
    if _EXPORT_CONTROL.search(d):
        return "export control or clearance"
    # The same guard the refusal branch gets, and for a worse reason.
    #
    # `_WILL_SPONSOR` matches "will not sponsor" as readily as "will sponsor",
    # which is why `_NO_SPONSOR` has a negated branch and is checked first.
    # But once a refusal is ruled INCIDENTAL, the text falls through to here
    # and the same "that employer will not sponsor" is read as an offer. So an
    # advert quoting another team's refusal told a reader who needs a visa
    # that this employer would sponsor them: the opposite of the truth on the
    # one fact the whole gate exists for, and more dangerous than the miss it
    # was introduced fixing.
    if _WILL_SPONSOR.search(d) and not _all_mentions_incidental(_WILL_SPONSOR, d):
        return "sponsorship offered"
    return ""


def sponsorship_gate(job: Job, cfg: Config) -> tuple[bool, str]:
    """The same shape as the salary rule, for the same reason.

    A role in a country you would need a visa for, whose posting says outright
    it will not sponsor, is one you cannot take however well it fits. So a
    stated refusal hides it.

    Silence does not. Most postings say nothing about sponsorship, and
    filtering on absence would throw away most of the market abroad, which is
    exactly the mistake the salary floor exists to avoid. Those are kept and
    marked, so the answer is a question worth asking rather than a role
    quietly removed.
    """
    if not cfg.need_sponsorship:
        return True, ""
    where = _countries_in(job.location) or ({job.country} if job.country else set())
    need = where & set(cfg.need_sponsorship)
    if not need:
        return True, ""

    rights = work_rights(job)
    if rights == "no sponsorship":
        return False, "needs a visa and the posting rules out sponsorship"
    if rights == "export control or clearance":
        # Not a refusal to sponsor, but it stacks on one and a visa does not
        # clear it. Kept, because many are open to anyone who can be cleared.
        job.flags.append("export control or clearance, on top of needing a visa")
    elif rights == "sponsorship offered":
        # enrich() already flags this for every role. Only add it if it is not
        # there, or a US role carried it twice.
        if "sponsorship offered" not in job.flags:
            job.flags.append("sponsorship offered")
    else:
        job.flags.append("sponsorship not stated, ask before you invest time")
    return True, ""


# A dealbreaker is a statement about THIS job. The regex is not: it fires on
# any occurrence anywhere in the description, including the teams you would
# work alongside and the certifications they would like you to hold.
#
# Measured on the 157 engineering-leadership postings in a 13,588-posting
# sample: the shipped "pre-sales" dealbreaker hid 4 of them, and 3 were
# nothing to do with the job. A "Cloud Platform Engineering Manager" was
# deleted because a PREFERRED CERTIFICATION is called "Azure Solutions
# Architect Expert", and an "Engineering Manager, Agent Oversight" because it
# said it works "cross-functionally with customers, forward deployed teams".
#
# So a hard dealbreaker whose every occurrence is one of these is downgraded
# to a warning rather than acted on. The role is still shown with the phrase
# named, which is the answer the reader can argue with; deleting it is not.
_INCIDENTAL = re.compile(
    r"(?:work|works|working|partner|partners|partnering|collaborate|"
    r"collaborates|collaborating|liaise|engage|engages|align|aligns|"
    r"coordinate|coordinates|interface|interfaces|pair|pairs)\s+"
    r"(?:closely\s+|cross[- ]functionally\s+|effectively\s+|directly\s+)?"
    r"(?:with|alongside|across)\b[^.]{0,80}$"
    r"|\balongside\b[^.]{0,60}$"
    r"|\bin concert with\b[^.]{0,60}$"
    r"|\b(?:pulling in|hand(?:ing)? off to|escalat\w+ to|supported by)\b[^.]{0,60}$"
    r"|\b(?:certified|certification|certifications|certificate|accreditation)\b"
    r"[^.]{0,60}$"
    r"|\((?:e\.?g\.?|for example|such as|including|i\.?e\.?)[^).]{0,120}$"
    # A sentence that has already said it is about something else.
    #
    # "our US graduate scheme is a separate programme and that employer will
    # not sponsor applicants for those positions" was read as this posting's
    # own policy, so a reader who needs a visa never saw a role that would
    # have taken them. The guard knew "works with X" and "e.g." and nothing
    # about a clause naming a different thing.
    #
    # Deliberately narrow. The words have to say separate or another AND name
    # a thing a policy can belong to, because the failure in the other
    # direction is worse: a real refusal read as incidental shows somebody a
    # role that will reject them, and the whole point of the gate is that it
    # does not. "other roles" and "those positions" are not here for that
    # reason; they appear in adverts that mean the opposite.
    r"|\b(?:separate|different|another|other)\s+"
    r"(?:programme|program|scheme|entity|employer|company|organisation|"
    r"organization|subsidiary)\b[^.]{0,90}$",
    re.I)
_INCIDENTAL_LEAD = 140


def _mention_is_incidental(text: str, start: int) -> bool:
    """Is this occurrence about somebody else's job, or a hypothetical?"""
    return bool(_INCIDENTAL.search(text[max(0, start - _INCIDENTAL_LEAD):start]))


# Words that turn a dealbreaker into a promise.
#
# "There is no night shift and no on-call rota" and "We do not set take-home
# exercises" both hid the role, silently, with no flag: the pattern matched
# and nothing looked at what came before it. Those are adverts going out of
# their way to say the thing you are avoiding is absent, and they were the
# first ones thrown away.
#
# A negated mention is downgraded to a soft flag rather than ignored. Getting
# this wrong in the other direction would let a real take-home through
# unseen, and the whole point of a hard dealbreaker is that it does not. Shown
# and labelled is the safe answer to "we are not certain what this sentence
# means".
_NEGATED = re.compile(
    r"(?:\b(?:no|not|never|without|zero|free\s+from|free\s+of)\b|"
    r"\bdo(?:es)?\s+not\b|\bdon.t\b|\bwon.t\b|\bisn.t\b|\baren.t\b)"
    r"[^.;:!?]{0,40}$", re.I)


def _mention_is_negated(text: str, at: int) -> bool:
    """Whether the sentence up to this match negates it."""
    start = max(0, at - 90)
    before = text[start:at]
    # Only within the same sentence: a full stop resets the claim.
    before = re.split(r"[.;:!?]", before)[-1]
    return bool(_NEGATED.search(before))


def _all_mentions_negated(pattern, text: str, title: str = "") -> bool:
    if pattern.search(title or ""):
        return False
    hits = list(pattern.finditer(text))
    return bool(hits) and all(_mention_is_negated(text, m.start()) for m in hits)


def _all_mentions_incidental(pattern, text: str, title: str = "") -> bool:
    if pattern.search(title or ""):
        return False
    hits = list(pattern.finditer(text))
    return bool(hits) and all(_mention_is_incidental(text, m.start()) for m in hits)


def screen(job: Job, cfg: Config) -> tuple[bool, list[str]]:
    """Dealbreaker scan over the description. Returns (keep, hits)."""
    # Warn on a posting too thin to have been screened properly, but still run
    # the patterns over whatever text is there.
    #
    # Two separate faults. The flag was added only for LinkedIn, so a Workday
    # role whose enrichment failed passed every dealbreaker with no warning at
    # all. And the first attempt at fixing that skipped the patterns entirely
    # below a length threshold, which is worse: a thirty-character description
    # saying "take home exercise" contains the disqualifying sentence, and
    # refusing to read it is the same silent pass by another route.
    text = (job.description or "").strip()
    if len(text) < 200:
        # Two different facts, and one sentence was covering both. "No
        # description from this source" was printed against postings that
        # plainly had one, just a short one, which reads as a broken adapter
        # rather than a thin advert and sends the reader looking for a bug.
        job.flags.append(
            "not screened: no description from this source" if not text else
            f"barely screened: this source gave {len(text)} characters of "
            f"advert, too little to check properly")

    # The title and the location are read too, and they were not.
    #
    # A role posted at "Hybrid - New York, NY" was kept for a reader with a
    # hard `hybrid` dealbreaker, because the word was in the location column
    # rather than the advert. The dashboard printed it, in that exact form, on
    # the row it should have hidden. Employers routinely put the arrangement
    # in the location field and nowhere else, and a contract, a night shift or
    # a clearance requirement often appears only in the title.
    #
    # Both fields are short and factual, which is why they are safe to add:
    # the incidental-mention guard below exists for the description, where a
    # long advert can mention another team's policy in passing. A location of
    # "Hybrid - New York" is not mentioning hybrid working, it is stating it.
    scanned = " ".join(x for x in (job.title, job.location, job.description)
                       if x).strip()
    if not scanned:
        return True, []

    hits, hard = [], []
    for db in cfg.dealbreakers:
        pat = db.compiled()
        if not pat.search(scanned):
            continue
        hits.append(db.name)
        if not db.hard:
            job.flags.append(f"soft flag: {db.name}")
        elif _all_mentions_incidental(pat, job.description, job.title):
            job.flags.append(
                f"soft flag: {db.name} mentioned, but only in passing "
                f"(another team, or an example) -- shown rather than hidden")
        elif _all_mentions_negated(pat, scanned, job.title):
            job.flags.append(
                f"soft flag: {db.name} appears only as something this role "
                f"does NOT have -- shown rather than hidden, check the advert")
        else:
            hard.append(db.name)
    return (not hard), hits


def apply_salary(job: Job, cfg: Config) -> tuple[bool, str]:
    keep, why = clears_floor(job.salary, cfg.salary_floor, cfg.salary_currency)
    if keep and why:
        job.flags.append(why)
    return keep, why


# Seniority words, roughly ordered. Used to notice when a posting sits well
# above or below the level you asked for, which the score was blind to: a
# Principal role and a grade-I role got identical numbers because nothing in
# the calculation read the candidate at all.
# Two things were wrong here and they compounded.
#
# The level-2 vocabulary was a list of engineering job nouns, so every title
# outside engineering scored 0: "Product Designer", "Data Scientist", "Scrum
# Master" and "Nurse Practitioner" all had no level at all. A target list of
# 0s and 3s then made every senior posting look like a leap.
#
# And `staff` and `principal` sat above `manager`, which reads the individual
# and the management tracks as one ladder. They are parallel: Staff is the
# rung above Senior on the IC side, roughly level with Manager, and Principal
# is the one above that. With the old numbers a remote-only product designer
# searching for "Product Designer" and "Senior Product Designer" saw "Staff
# Product Designer" -- the correct next role for them -- scored "2 levels
# above your targets" and docked 25, landing it below a plain junior posting.
# Their ranked list came out upside down.
_LEVELS = [
    (1, r"\b(?:junior|graduate|trainee|apprentice|entry.level|assistant)\b|\bI\b$"),
    (2, r"\b(?:analyst|associate|engineer|officer|advisor|coordinator|"
        r"executive|designer|scientist|developer|researcher|consultant|"
        r"nurse|teacher|technician|accountant|planner|writer|editor|"
        r"recruiter|buyer|controller|architect|administrator)\b"),
    (3, r"\b(?:senior|snr|specialist|lead(?!ership)|supervisor)\b"),
    # Staff sits with Manager, not above it: one is the senior IC rung and
    # the other is the first management rung, and neither outranks the other.
    (4, r"\b(?:manager|management|staff)\b"),
    (5, r"\b(?:principal|head of|senior manager|group manager)\b"),
    (6, r"\b(?:director|vp|vice president|chief|c-level|partner)\b"),
]


def seniority(title: str) -> int:
    """Rough level of a title, 0 when nothing identifies one.

    An explicit junior marker wins outright. Taking the maximum meant "Junior
    Data Engineer" scored as mid-level, because "engineer" outranks "junior".
    """
    title = title or ""
    if re.search(_LEVELS[0][1], title, re.I):
        return 1
    best = 0
    for lvl, pat in _LEVELS[1:]:
        if re.search(pat, title, re.I):
            best = max(best, lvl)
    return best


def _target_band(cfg: Config) -> tuple[int, int]:
    """The span of levels you asked for, not just the top of it.

    Taking the maximum meant listing one director title alongside a manager
    title marked every manager role as two levels too junior, which is the
    opposite of what listing both means.
    """
    levels = [l for l in (seniority(t) for t in cfg.titles_include) if l]
    return (min(levels), max(levels)) if levels else (0, 0)


def score(job: Job, cfg: Config) -> float:
    """A transparent 0-100 score. Explanations land in `job.reasons`."""
    s, why = 0.0, []

    inc = cfg.title_include_re()
    if inc and inc.search(job.title):
        s += 35
        why.append("title matches your targets")

    # Regions here too. `regions_in` was wired into `match` and nowhere else,
    # so a role in "Remote - Europe" passed the filter and then scored 20
    # lower than a bare "Remote", which is scored as naming nowhere. The
    # qualifier that makes a role MORE relevant to a European reader cost it
    # the points, and every one of the eight roles actually in Greece sat
    # below fifteen US-only postings the reader cannot take.
    found = _countries_in(job.location) or regions_in(job.location)
    home = found & set(cfg.countries)
    if home:
        s += 20
        why.append("remote in " + ", ".join(sorted(home)) if job.remote
                   else "in " + ", ".join(sorted(home)))
    elif job.remote and not found:
        # The description may name one even when the location field does not.
        scope = remote_scope(job)
        if scope & set(cfg.countries):
            s += 20
            why.append("remote, body says " + ", ".join(sorted(scope)))
        elif scope:
            why.append("remote but restricted to " + ", ".join(sorted(scope)))
        else:
            s += 20
            why.append("remote, no country named")
    elif found & set(cfg.relocate_to):
        s += 8
        why.append("in " + ", ".join(sorted(found & set(cfg.relocate_to))) + ", relocation")

    # Where you would need a visa, a posting that says it will sponsor is
    # worth more than one that says nothing: the difference decides whether
    # you can take the job at all.
    if cfg.need_sponsorship and (found & set(cfg.need_sponsorship)) \
            and "sponsorship offered" in job.flags:
        s += 12
        why.append("says it will sponsor")

    if job.salary.confirmed:
        # Publishing a figure is worth points, but only fully when the figure
        # tells you something. A EUR floor cannot read a sterling number, and
        # a 13.50/hour NHS post was collecting the same transparency bonus as
        # a role paying twice the floor.
        comparable_cur = not (cfg.salary_currency and job.salary.currency
                              and job.salary.currency != cfg.salary_currency)
        s += 10 if comparable_cur else 4
        # `label()` rather than `raw`, with the same fallback `clears_floor`
        # already uses. `raw` is the snippet a text parser cut out of the
        # advert, and it is absent whenever the figure came from a structured
        # field instead: every Ashby posting, and every role read out of a
        # seed shard, whose format carries the numbers and not the snippet.
        # The f-string printed the missing value straight through, so a new
        # user's first `list --json` said "pay stated (None)" on 10 of their
        # 19 priced roles while the row beside it read "$165k - $185k".
        # And only when it carries a figure. `raw` is the snippet the parser
        # matched in, and on Greenhouse that is routinely the HEADING sitting
        # in the same field as the numbers: "Annual base salary range
        # (excluding equity and bonus):" and "Local Pay Range". So the top row
        # of a dashboard read "pay stated (Annual base salary range (excluding
        # equity and bonus):)" beside a perfectly good label of INR 6.6M.
        #
        # `Salary.label` exists for exactly this and says so three lines above
        # its own definition. The None case was fixed and the heading case was
        # not, which is the same fault with something in the variable.
        raw = (job.salary.raw or "").strip()
        shown = raw if any(c.isdigit() for c in raw) else job.salary.label()
        why.append(f"pay stated ({shown})" if comparable_cur
                   else f"pay stated ({shown}), not comparable to your floor")
        top = job.salary.annualised()
        # The same currency guard `clears_floor` applies. Without it the
        # filter refused to compare GBP against a EUR floor while the scorer
        # went ahead and awarded points for it, so one row said "not compared"
        # and the next said "comfortably above your floor" about the same pay.
        if top and cfg.salary_floor and comparable_cur and top >= cfg.salary_floor * 1.15:
            s += 10
            # Both the filter and these points read the TOP of the band, which
            # is deliberate and documented on `Salary.top`: 100k-150k against a
            # 120k floor is still a role worth talking to them about. The
            # sentence was not so careful. Accenture Federal's "$90k - $184k"
            # was printed to a reader with a $150k floor as "comfortably above
            # your floor", and so were 31 other bands whose advertised bottom
            # is below the number they said was their minimum. The points are
            # unchanged; only the claim is, because a band that straddles the
            # floor and a band that clears it are not the same news.
            bottom = job.salary.min if job.salary.min is not None else top
            if job.salary.period == "day":
                bottom *= 220
            elif job.salary.period == "hour":
                bottom *= 220 * 8
            why.append("comfortably above your floor"
                       if bottom >= cfg.salary_floor else
                       "top of that band is above your floor, the bottom "
                       "is not")
    else:
        why.append("unconfirmed salary")

    if job.posted_at:
        from datetime import date
        try:
            age = (date.today() - date.fromisoformat(job.posted_at)).days
            if age <= 7:
                s += 15
                why.append("posted this week")
            elif age <= 21:
                s += 8
                why.append(f"posted {age} days ago")
            elif age >= 180:
                # Age only ever ADDED points, so an old posting was scored as
                # though its date were unknown and sat wherever the rest of
                # the scoring put it. Measured on one board: 89 of 442 roles
                # were over 180 days old and 26 over a year, the oldest posted
                # 2022-02-23, and a 2023 posting scored 85 and outranked
                # fresher roles with nothing anywhere saying it was two years
                # old.
                #
                # Flagged rather than dropped. Some of those URLs still answer
                # 200, so the role may genuinely be open, and an employer who
                # never takes a posting down is not the same as a role that
                # has gone. What was wrong was silence, not the presence of
                # the role.
                #
                # The penalty is deliberately smaller than the freshness
                # bonus: this says "check the date", not "this is dead".
                s -= 10
                years = age // 365
                why.append(f"posted over {years} year{'s' if years > 1 else ''} "
                           f"ago" if years else f"posted {age} days ago")
                job.flags.append(
                    f"posted {age} days ago; boards do not always take old "
                    f"listings down, so check it is still open")
        except ValueError:
            pass

    if not job.flags:
        s += 10

    # A role two levels above what you asked for is not a better match for
    # being more senior. Without this the top of the list was whatever was
    # posted most recently, regardless of whether it was reachable.
    low, high = _target_band(cfg)
    lvl = seniority(job.title)
    if high and lvl:
        above, below = lvl - high, low - lvl
        if above >= 2:
            s -= 25
            why.append(f"reads {above} levels above your targets")
            job.flags.append("a stretch: this sits well above the titles you asked for")
        elif above == 1:
            s -= 8
            why.append("a level above your targets")
        elif below >= 2:
            s -= 15
            why.append(f"reads {below} levels below your targets")

    job.score = round(max(min(s, 100.0), 0.0), 1)
    job.reasons = why
    return job.score


# How direct a source is. A posting read from the employer's own applicant
# tracking system is the employer speaking; the same role on a keyword search
# is a copy, usually with no description, sometimes reposted by an agency. So
# when the same job arrives twice, keep the one closest to the employer.
def directness(platform: str) -> int:
    """How close a source is to the employer, for picking a dedupe winner.

    An aggregator scores below an employer's own board. Reed sits with NHS
    Jobs rather than with Greenhouse: it carries full advert text, so leaving
    it at the default would let a Reed repost beat the employer's own posting
    on description length alone and hand the reader a redirect instead of the
    real apply page.

    Every aggregator has to be listed here, not just the ones that carry a
    long description. `_fold_aggregators` decides what counts as an aggregator
    by asking whether this returns less than 2, so a new one left at the
    default is treated as an employer's own board: it will not fold, and its
    repost shows as a second row beside the real vacancy.
    """
    return {"linkedin": 0, "nhs": 1, "reed": 1, "adzuna": 1,
            # jobs.workable.com is Workable's aggregator over the boards it
            # hosts, so it sits with Reed for the same reason: it carries the
            # full advert, and left at the default it would beat the
            # employer's own apply.workable.com board on description length
            # and hand the reader Workable's view page instead of the real
            # one. 36% of what it finds is an employer already on the list.
            "workable_search": 1, "workable_recent": 1}.get(
        (platform or "").lower(), 2)


# Legal form and holding-company words. An aggregator prints whatever the
# employer registered as, so the same role arrives as "Monzo Bank Ltd" from
# Reed and "Monzo" from the employer's own board. Grouping on the raw name
# meant they never met, both rows showed, and directness never got to decide.
#
# Deliberately not in this list: country and region words. "Ramsay Health Care
# UK" and "Ramsay Health Care" are separate entities hiring separately.
_LEGAL_FORM = re.compile(
    r"\b(?:ltd|limited|plc|inc|incorporated|llc|llp|lp|gmbh|ag|a\.?g|bv|b\.?v|"
    r"nv|n\.?v|sa|s\.?a|srl|s\.?r\.?l|pty|pte|oy|ab|as|aps|kk|corp|"
    r"corporation|holdings?|group)\b\.?", re.I)


def _same_employer(name: str) -> str:
    """The grouping key for one employer, however a source spells it."""
    n = _LEGAL_FORM.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return " ".join(n.split()) or (name or "").strip().lower()


# How the offices of one role are written on a single line, and how many of
# them are named before the line turns into a count.
LOCATION_JOIN = " / "
MAX_SHOWN_LOCATIONS = 6
_MORE_SUFFIX = re.compile(r"\s*\+\s*\d+\s+more\s*$", re.I)


def location_parts(text: str) -> list[str]:
    """The individual places in a location line, joined or not.

    Undoes `merged_location`, so merging a row that was already merged does
    not nest one joined line inside another and produce "London / Berlin /
    London / Berlin". The "+N more" tail is dropped rather than parsed: the
    names behind it are gone and inventing a count for them would be worse
    than under-reporting one.
    """
    text = _MORE_SUFFIX.sub("", (text or "").strip())
    return [p.strip() for p in text.split(LOCATION_JOIN) if p.strip()]


def merged_location(locations, cfg: Config | None = None) -> tuple[str, int]:
    """One location line for a role that is open in several places, and how
    many places that is.

    Shared with `store.merge_duplicates` deliberately. Both functions collapse
    the same role posted once per office, and for a while only one of them
    kept the other offices: this pass joined the locations onto the survivor,
    and the database pass deleted the losing row outright. So a Greenhouse
    role open in London and in New York showed both cities when both copies
    arrived in one scan, and lost New York when the second copy arrived a day
    later. Same input, two answers, decided by timing.
    """
    locs: list[str] = []
    seen: set[str] = set()
    for raw in locations:
        for part in location_parts(raw):
            if part.lower() in seen:
                continue
            seen.add(part.lower())
            locs.append(part)
    # Show the locations the reader can actually take first. A role open in
    # twenty countries should not lead with the nineteen that are no use.
    if cfg:
        wanted = set(cfg.countries) | set(cfg.relocate_to)
        locs.sort(key=lambda l: 0 if (_countries_in(l) & wanted) else 1)
    shown = locs[:MAX_SHOWN_LOCATIONS]
    text = LOCATION_JOIN.join(shown)
    if len(locs) > len(shown):
        text += f" +{len(locs) - len(shown)} more"
    return text, len(locs)


def dedupe(jobs: list[Job], cfg: Config | None = None) -> list[Job]:
    """Collapse the same role posted once per location, or once per source.

    Several ATSs publish one posting per office, so a single job appears six
    times with six URLs. Merging them on company+title and joining the
    locations turns six rows back into the one job it actually is.

    The same grouping catches a role that arrived from two sources: Wise's
    Risk API role came in from both LinkedIn and SmartRecruiters under
    identical titles. The SmartRecruiters copy is the one to keep, because it
    is the employer's own board and carries the description that LinkedIn's
    does not.
    """
    groups: dict[tuple[str, str], list[Job]] = {}
    for j in jobs:
        groups.setdefault((_same_employer(j.company), j.title.strip().lower()),
                          []).append(j)

    out: list[Job] = []
    for members in groups.values():
        if len(members) == 1:
            out.append(members[0])
            continue
        best = max(members, key=lambda x: (directness(x.platform),
                                           x.salary.confirmed,
                                           len(x.description or "")))
        best.location, n_locs = merged_location(
            [m.location or "" for m in members], cfg)
        if len({m.platform for m in members}) > 1:
            others = sorted({m.platform for m in members} - {best.platform})
            best.flags.append("also listed on " + ", ".join(others))
        if len({(m.location or "").lower() for m in members}) > 1:
            best.flags.append(f"posted in {n_locs} locations")
        out.append(best)
    return _fold_aggregators(out)


def _fold_aggregators(jobs: list[Job]) -> list[Job]:
    """Second pass: an aggregator row folds into the employer's own posting.

    Stripping legal forms is not enough on its own. Reed prints "Monzo Bank
    Ltd" and LinkedIn prints "Wise Payments Limited" where the employer's own
    board says "Monzo" and "Wise", and the leftover descriptor word means the
    two never group. Both rows then show, which is the thing an aggregator is
    most likely to do to this list.

    Restricted to folding an aggregator into a direct board, never one direct
    board into another. A loose name match is a guess, and the cost of a wrong
    guess has to fall on the duplicate rather than on somebody's real vacancy.
    The title must still match exactly, because "Engineering Manager, Platform"
    and "Engineering Manager, Payments" are two jobs.
    """
    direct: dict[str, list[Job]] = {}
    for j in jobs:
        if directness(j.platform) >= 2:
            direct.setdefault(j.title.strip().lower(), []).append(j)
    if not direct:
        return jobs

    out = []
    for j in jobs:
        if directness(j.platform) >= 2:
            out.append(j)
            continue
        mine = _same_employer(j.company)
        owner = None
        for d in direct.get(j.title.strip().lower(), []):
            theirs = _same_employer(d.company)
            if not mine or not theirs:
                continue
            # One name has to start with the other. Containment anywhere would
            # fold "Data Engineer at Sky" into "Sky" and also into "Skyscanner".
            if mine.startswith(theirs) or theirs.startswith(mine):
                owner = d
                break
        if owner is None:
            out.append(j)
            continue
        if j.platform not in " ".join(owner.flags):
            owner.flags.append(f"also listed on {j.platform}")
    return out


def run(jobs: list[Job], cfg: Config) -> tuple[list[Job], dict[str, int]]:
    """Full pipeline. Returns (kept, counts_by_drop_reason)."""
    jobs = dedupe(jobs, cfg)
    kept: list[Job] = []
    dropped: dict[str, int] = {}

    def drop(reason: str):
        key = re.sub(r"\(.*?\)", "", reason).strip() or reason
        dropped[key] = dropped.get(key, 0) + 1

    for j in jobs:
        # The title gate first, and `enrich` only for what survives it.
        #
        # `enrich` resolves the country, the city, the work mode and the work
        # rights flag, which is the most expensive thing done per posting: 85%
        # of screening CPU, measured. It was being run on every posting before
        # the filter that discards more than 99% of them. `match` reads
        # nothing `enrich` sets, because it resolves countries itself through
        # `_countries_in`, so the two lines simply swap.
        #
        # Measured over 6,044 real postings: 5.58 seconds to 0.38, a 93% cut,
        # with an identical kept set, identical drop-reason counts, identical
        # flags and identical countries. Across a full scan that is about
        # seven minutes down to thirty seconds.
        #
        # The order below is load-bearing. `sponsorship_gate` reads
        # `job.country`, and `apply_salary` and `screen` both append to
        # `job.flags`, so `enrich` has to come after `match` and before all
        # three of them.
        ok, why = match(j, cfg)
        if not ok:
            drop(why)
            continue
        enrich(j)
        ok, why = apply_salary(j, cfg)
        if not ok:
            drop("stated pay below floor")
            continue
        ok, hits = screen(j, cfg)
        if not ok:
            drop(f"dealbreaker: {', '.join(hits)}")
            continue
        ok, why = sponsorship_gate(j, cfg)
        if not ok:
            drop(why)
            continue
        score(j, cfg)
        # The same Job objects are screened more than once in a real scan:
        # `_flush_phase` screens what has arrived between passes and then the
        # summary screens `all_jobs` again at the end. `apply_salary`,
        # `screen` and `score` all append to `job.flags` unconditionally, so
        # a role that survived two of those passes carried every flag twice
        # ("salary in USD, floor in EUR, not compared" was rendered twice on
        # every affected row of the dashboard, and up to five times on a
        # four-pass scan). Flags are a set in meaning, so make them one here,
        # keeping the order they were added in.
        j.flags = list(dict.fromkeys(j.flags))
        kept.append(j)

    kept.sort(key=lambda x: (-x.score, x.company.lower()))
    return kept, dropped
