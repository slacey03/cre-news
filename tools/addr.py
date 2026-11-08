"""
Strict street-address matching shared by news linking and the dashboard pipeline.

An address is (house number, direction, street name, street type). Two addresses match only when the house number
and street name are identical after standard abbreviations; direction and street type must also agree when both
sides state them. Nothing else counts: no street-only, name-only, nearby or fuzzy matches.
"""
import re

SUFFIX = {"street": "st", "st": "st", "road": "rd", "rd": "rd", "avenue": "ave", "ave": "ave", "av": "ave",
          "boulevard": "blvd", "blvd": "blvd", "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln", "pike": "pike",
          "highway": "hwy", "hwy": "hwy", "court": "ct", "ct": "ct", "circle": "cir", "cir": "cir", "place": "pl",
          "pl": "pl", "way": "way", "terrace": "ter", "ter": "ter", "parkway": "pkwy", "pkwy": "pkwy", "trail": "trl",
          "trl": "trl", "tr": "trl", "alley": "aly", "aly": "aly", "square": "sq", "sq": "sq", "plaza": "plz"}
DIRS = {"n": "n", "north": "n", "s": "s", "south": "s", "e": "e", "east": "e", "w": "w", "west": "w"}
SUFFIX_RX = "|".join(sorted(SUFFIX, key=len, reverse=True))
ROUTE_RX = r"(?:route|rt|state route|sr|pa route|pa|u\.?s\.? route|us)\.?\s*(\d{1,3}[a-z]?)"

NUMS = r"(?<![\w-])(\d{1,6}[a-z]?(?:\s*(?:[-–/&,]|and)\s*\d{1,6}[a-z]?)*)\s+"
DIR = r"(?:(north|south|east|west|[nsew])\.?\s+)?"
NAME = r"((?:(?:st|saint|mt|mount)\.?\s+)?[a-z][a-z'.]*(?:\s+[a-z][a-z'.]*){0,2}|\d+(?:st|nd|rd|th))"
# 1) with a street type: '1865 Leithsville Road', '3410/3530 Mauch Chunk Rd'   2) routes: '3507 Route 378'
WITH_SUFFIX = re.compile(NUMS + DIR + NAME + r"\s+(" + SUFFIX_RX + r")\.?(?![a-z])", re.I)
ROUTE = re.compile(NUMS + DIR + r"(" + ROUTE_RX + r")(?![\w])", re.I)
# 3) address fields only: '540 Hamilton, Allentown' (no street type) -> first word as the street name
BARE = re.compile(NUMS + DIR + r"([a-z][a-z'.]*|\d+(?:st|nd|rd|th))(?![\w])", re.I)


def _name(raw):
    n = re.sub(r"[^a-z0-9 ]", " ", raw.lower())
    n = re.sub(r"\b(saint|st)\s+(?=[a-z])", "st ", n)
    n = re.sub(r"\b(mount|mt)\s+(?=[a-z])", "mt ", n)
    r = re.match(r"(?:route|rt|state route|sr|pa route|pa|u s route|us route|us)\s*(\d{1,3}[a-z]?)$", n.strip())
    return f"route {r.group(1)}" if r else " ".join(n.split())


def _nums(raw):
    return [n.rstrip("abcdefghijklmnopqrstuvwxyz") for n in re.findall(r"\d{1,6}[a-z]?", raw.lower())]


STOP = {"the", "a", "an", "and", "of", "to", "in", "on", "at", "acres", "acre", "square", "feet", "units", "lots",
        "homes", "stories", "percent", "people", "years", "spaces", "parking", "minutes", "miles", "days", "jobs"}


def parse(text, need_suffix=False):
    """All addresses in text as (num, dir, name, suffix). need_suffix=True for free text (articles): a street type or
    'Route N' is required, so '2024 Bethlehem Township' or '15 minutes' are never read as addresses."""
    out, spans = [], []
    t = text or ""
    for rx, kind in ((WITH_SUFFIX, "suffix"), (ROUTE, "route")) + (() if need_suffix else ((BARE, "bare"),)):
        for m in rx.finditer(t):
            if any(m.start() < e and s < m.end() for s, e in spans):
                continue  # already read by a stricter pattern
            name = _name(m.group(3))
            if name.split()[0] in STOP:
                continue
            suf = SUFFIX.get((m.group(m.lastindex) or "").lower()) if kind == "suffix" else None
            if kind == "suffix" and name.split()[-1] in SUFFIX:  # 'Main St Road' guard
                continue
            spans.append((m.start(), m.end()))
            d = DIRS.get((m.group(2) or "").lower().strip("."))
            for n in _nums(m.group(1)):
                out.append((n, d, name, suf))
    return out


def same(a, b):
    """Exact match: same number and street name; direction and type must agree when both are stated."""
    if a[0] != b[0] or a[2] != b[2]:
        return False
    if a[1] and b[1] and a[1] != b[1]:
        return False
    if a[3] and b[3] and a[3] != b[3]:
        return False
    return True


def key(a):
    return f"{a[0]} {a[2]}"


def town_names(municipality):
    """Names an article may use for the municipality: 'Lower Saucon Township' -> ['lower saucon']."""
    base = re.sub(r"\b(township|twp|borough|boro|city of|city)\b\.?", " ", (municipality or "").lower())
    base = re.sub(r"\((lehigh|northampton)\)", " ", base)
    base = " ".join(base.replace(",", " ").split())
    return [base] if base else []


def text_mentions_town(text, municipality):
    t = " ".join(re.sub(r"[^a-z ]", " ", (text or "").lower()).split())
    return any(re.search(rf"\b{re.escape(n)}\b", t) for n in town_names(municipality))
