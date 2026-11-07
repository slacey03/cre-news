"""
Detect, for each municipality in registry/lehigh_valley.json:
  - whether the website is reachable (and whether it blocks automated visitors),
  - the website platform (CivicPlus, Granicus govAccess, Revize, WordPress, ...),
  - any agenda/meeting vendor it links to (Granicus, Legistar, CivicClerk, eSCRIBE, ...),
  - where its agenda / minutes / planning / zoning pages are, and what the best one holds.

Read-only. Honors robots.txt for our user agent, waits between requests to the same site,
and makes at most a handful of requests per municipality.
Writes registry/out/lehigh_valley_detected.json and registry/out/lehigh_valley_registry.csv.
"""
import csv, json, os, re, time
from concurrent.futures import ThreadPoolExecutor
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = "ZoningTrack/0.1 (+https://zoningtrack.com; public-records collector)"
HERE = os.path.dirname(__file__)
PAUSE = 6  # seconds between requests to the same site

CMS = [  # (label, regex on html or final url)
    ("CivicPlus", r"civicplus|/AgendaCenter|civicengage|/DocumentCenter/View/"),
    ("Granicus govAccess", r"govaccess|/Home/Components/|granicus\.com/.*?govaccess|vision ?internet"),
    ("Revize", r"revize\.com|cms\d?\.revize"),
    ("CivicLive", r"civiclive|schoolwires"),
    ("Municipal CMS (eCivis/ECode)", r"municipalcms|ecivis"),
    ("WordPress", r"wp-content|wp-includes"),
    ("Squarespace", r"squarespace"),
    ("Wix", r"wixstatic|wix\.com|_wix"),
    ("GoDaddy Builder", r"img1\.wsimg\.com|godaddy"),
    ("Weebly", r"weebly"),
    ("Webs.com", r"webs\.com|vistaprint"),
    ("Drupal", r"drupal"),
    ("Joomla", r"joomla"),
    ("GovOffice", r"govoffice"),
    ("Streamline", r"getstreamline|streamline\.com"),
]
VENDORS = [
    ("Granicus (meetings)", r"[a-z0-9-]+\.granicus\.com"),
    ("Legistar", r"[a-z0-9-]+\.legistar\.com"),
    ("CivicClerk", r"[a-z0-9-]+\.(?:portal\.)?civicclerk\.com"),
    ("eSCRIBE", r"[a-z0-9-]+\.escribemeetings\.com"),
    ("BoardDocs", r"go\.boarddocs\.com/[^\s\"'<>]+"),
    ("Municode Meetings", r"[a-z0-9-]+\.municodemeetings\.com"),
    ("PrimeGov", r"[a-z0-9-]+\.primegov\.com"),
    ("iCompass/CivicWeb", r"[a-z0-9-]+\.civicweb\.net"),
    ("IQM2", r"[a-z0-9-]+\.iqm2\.com"),
    ("NovusAgenda", r"[a-z0-9-]+\.novusagenda\.com"),
    ("Diligent Community", r"[a-z0-9-]+\.diligent\.community"),
    ("AgendaQuick/OnBase", r"onbaseweb|agendaquick"),
    ("YouTube (video)", r"youtube\.com/(?:@|channel|c/)[^\s\"'<>]+"),
]
TOPICS = {
    "agendas": r"agenda",
    "minutes": r"minutes",
    "meetings": r"meeting|calendar",
    "planning": r"planning|land develop|subdivision",
    "zoning": r"zoning|hearing board|\bzhb\b",
    "notices": r"public notice|legal notice|hearing",
}
BLOCK_SIGNS = r"<title>[^<]*(Access Denied|Just a moment|Attention Required|Request unsuccessful|Security check)|errors\.edgesuite|_Incapsula_Resource|cf-chl-bypass"
DATE_RE = re.compile(r"\b(20\d\d)[-_./]?(0[1-9]|1[0-2])[-_./]?(0[1-9]|[12]\d|3[01])\b|\b(0?[1-9]|1[0-2])[-_./](0?[1-9]|[12]\d|3[01])[-_./](20\d\d|\d\d)\b|\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+(20\d\d)\b", re.I)


class Site:
    def __init__(self, base):
        self.s = requests.Session(); self.s.headers["User-Agent"] = UA
        self.base = base; self.last = 0.0; self.rp = None; self.robots_note = ""

    def wait(self):
        d = self.last + PAUSE - time.time()
        if d > 0: time.sleep(d)

    def fetch(self, url, check_robots=True):
        if check_robots and self.rp and not self.rp.can_fetch(UA, url):
            return None, "robots-disallowed"
        self.wait()
        try:
            r = self.s.get(url, timeout=30, allow_redirects=True)
        except requests.RequestException as e:
            self.last = time.time()
            return None, type(e).__name__
        self.last = time.time()
        return r, None

    def load_robots(self, origin):
        r, err = self.fetch(urljoin(origin, "/robots.txt"), check_robots=False)
        self.rp = robotparser.RobotFileParser()
        if r is not None and r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
            self.rp.parse(r.text.splitlines())
            ai_block = bool(re.search(r"User-agent:\s*(GPTBot|ClaudeBot|CCBot)", r.text, re.I))
            self.robots_note = "robots.txt present" + ("; blocks AI crawlers" if ai_block else "")
        else:
            self.rp.parse([]); self.robots_note = "no robots.txt" if r is not None else f"robots fetch failed ({err})"


def classify(html, url):
    hay = html[:400000] + " " + url
    cms = [name for name, rx in CMS if re.search(rx, hay, re.I)]
    vendors = {}
    for name, rx in VENDORS:
        for m in re.findall(rx, hay, re.I):
            vendors.setdefault(name, set()).add(m.lower().rstrip("/."))
    return cms, {k: sorted(v)[:3] for k, v in vendors.items()}


def topic_links(soup, page_url):
    found = {k: [] for k in TOPICS}
    host = urlparse(page_url).netloc.replace("www.", "")
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())[:80]
        href = urljoin(page_url, a["href"]).split("#")[0]
        if not href.startswith("http"): continue
        blob = f"{text} {href}"
        for k, rx in TOPICS.items():
            if re.search(rx, blob, re.I) and len(found[k]) < 6 and href not in [h for _, h in found[k]]:
                found[k].append((text, href))
    return found


def page_profile(r):
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    pdfs = [a["href"] for a in soup.find_all("a", href=True) if re.search(r"\.pdf|ViewFile|DocumentCenter/View|/document/|showdocument|download", a["href"], re.I)]
    years = []
    for m in DATE_RE.finditer(soup.get_text(" ") + " " + " ".join(pdfs)):
        y = m.group(1) or m.group(6) or m.group(8)
        if y:
            y = int(y) if len(y) == 4 else 2000 + int(y)
            if 2015 <= y <= 2027: years.append(y)
    return {"pdf_links": len(pdfs), "newest_year": max(years) if years else None,
            "has_2026": 2026 in years}


def best_doc_page(found):
    for k in ("agendas", "minutes", "planning", "zoning", "meetings"):
        for text, href in found[k]:
            if not re.search(r"\.pdf($|\?)|facebook|twitter|youtube|mailto:", href, re.I):
                return k, text, href
    return None, None, None


def detect(m):
    out = dict(m, reachable=False, final_url=None, status=None, blocked=False, cms=[], vendors={},
               links={}, doc_page=None, doc_page_profile=None, robots="", notes=[])
    tried = []
    for u in m["urls"]:
        host = urlparse(u).netloc
        bare = host[4:] if host.startswith("www.") else "www." + host
        tried += [u, u.replace("https://", "http://"), f"https://{bare}"]
    for url in dict.fromkeys(tried):
        if out["reachable"]: break
        site = Site(url)
        site.load_robots(url)
        r, err = site.fetch(url)
        if r is None:
            out["notes"].append(f"{url}: {err}"); continue
        out["status"] = r.status_code; out["final_url"] = r.url; out["robots"] = site.robots_note
        if r.status_code >= 400 or re.search(BLOCK_SIGNS, r.text[:5000], re.I):
            out["blocked"] = r.status_code in (401, 403, 406, 429, 503) or bool(re.search(BLOCK_SIGNS, r.text[:5000], re.I))
            out["notes"].append(f"{url}: HTTP {r.status_code}{' (bot protection)' if out['blocked'] else ''}")
            continue
        out["reachable"] = True; out["blocked"] = False
        out["cms"], out["vendors"] = classify(r.text, r.url)
        soup = BeautifulSoup(r.text, "html.parser")
        gen = soup.find("meta", attrs={"name": re.compile("generator", re.I)})
        out["generator"] = (gen.get("content") or "")[:60] if gen else ""
        credit = re.search(r"(?:website|site|web design|designed|powered|hosted)\s*(?:by|design by)\s*:?\s*([A-Z][\w&.\- ]{2,40})", soup.get_text(" "), re.I)
        out["credit"] = credit.group(1).strip() if credit else ""
        if "CivicPlus" in out["cms"]:
            ac, _ = site.fetch(urljoin(r.url, "/AgendaCenter"))
            if ac is not None and ac.status_code == 200 and "catAgendaRow" in ac.text:
                out["agenda_center"] = ac.url
                out["doc_page"] = {"kind": "agendas", "text": "Agenda Center", "url": ac.url}
                out["doc_page_profile"] = page_profile(ac)
        found = topic_links(soup, r.url)
        out["links"] = {k: [h for _, h in v] for k, v in found.items() if v}
        kind, text, href = best_doc_page(found)
        if href and not out.get("agenda_center"):
            r2, err2 = site.fetch(href)
            if r2 is not None and r2.status_code < 400:
                cms2, v2 = classify(r2.text, r2.url)
                for k, v in v2.items(): out["vendors"].setdefault(k, v)
                for c in cms2:
                    if c not in out["cms"]: out["cms"].append(c)
                out["doc_page"] = {"kind": kind, "text": text, "url": r2.url}
                out["doc_page_profile"] = page_profile(r2)
            else:
                out["doc_page"] = {"kind": kind, "text": text, "url": href,
                                   "error": err2 or (r2.status_code if r2 is not None else None)}
        break
    return out


def summarize(d):
    vendors = [k for k in d["vendors"] if k != "YouTube (video)"]
    platform = vendors[0] if vendors else ("CivicPlus Agenda Center" if d.get("agenda_center") else (d["cms"][0] if d["cms"] else ("unknown" if d["reachable"] else "")))
    p = d.get("doc_page_profile") or {}
    if not d["reachable"]:
        route = "BLOCKED: needs permission or another route" if d["blocked"] else "UNREACHABLE: check website"
    elif vendors and vendors[0] in ("Legistar", "CivicClerk", "eSCRIBE", "Municode Meetings", "PrimeGov", "iCompass/CivicWeb", "IQM2", "NovusAgenda", "Diligent Community"):
        route = f"Vendor connector: {vendors[0]}"
    elif vendors and vendors[0] == "Granicus (meetings)":
        route = "Granicus: decision pending"
    elif platform == "CivicPlus Agenda Center" or d.get("agenda_center"):
        route = "CivicPlus connector (built)"
    elif d.get("doc_page"):
        route = "Page watcher (custom site)"
    else:
        route = "Manual review: no document page found"
    return {
        "key": d["key"], "name": d["name"], "county": d["county"], "type": d["type"],
        "website": d["final_url"] or d["urls"][0], "reachable": d["reachable"], "blocked": d["blocked"],
        "website_platform": ", ".join(d["cms"][:2]) or d.get("generator", ""), "site_credit": d.get("credit", ""), "agenda_software": "; ".join(f"{k}: {', '.join(v)}" for k, v in d["vendors"].items() if k != "YouTube (video)"),
        "documents_page": (d.get("doc_page") or {}).get("url", ""), "documents_pdf_links": p.get("pdf_links", ""),
        "documents_newest_year": p.get("newest_year", ""),
        "planning_link": (d["links"].get("planning") or [""])[0], "zoning_link": (d["links"].get("zoning") or [""])[0],
        "robots": d["robots"], "collection_route": route, "notes": " | ".join(d["notes"]),
    }


if __name__ == "__main__":
    munis = json.load(open(os.path.join(HERE, "lehigh_valley.json")))
    with ThreadPoolExecutor(max_workers=8) as ex:  # different sites in parallel; each site is paced
        results = list(ex.map(detect, munis))
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    json.dump(results, open(os.path.join(HERE, "out", "lehigh_valley_detected.json"), "w"), indent=1, default=list)
    rows = [summarize(d) for d in results]
    with open(os.path.join(HERE, "out", "lehigh_valley_registry.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    for r in rows:
        print(f"{r['name'][:34]:34} {r['collection_route'][:40]:40} {r['website_platform'][:28]}")
