"""One-off probe of lvpc.org: robots, sitemap, and pages/links about meetings, agendas, reviews."""
import re, requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
UA = "ZoningTrack/0.1 (+https://zoningtrack.com; public-records collector)"
s = requests.Session(); s.headers["User-Agent"] = UA
def get(u):
    try:
        r = s.get(u, timeout=60); print(f"GET {u} -> {r.status_code} {r.headers.get('content-type')} {len(r.content)}b"); return r
    except Exception as e:
        print(f"GET {u} -> ERROR {e}"); return None
r = get("https://lvpc.org/robots.txt"); print("ROBOTS:\n" + (r.text[:1500] if r is not None else ""))
pages = set()
for sm in ["https://lvpc.org/sitemap.xml", "https://www.lvpc.org/sitemap.xml"]:
    r = get(sm)
    if r is not None and r.ok:
        locs = re.findall(r"<loc>(.*?)</loc>", r.text); print(f"SITEMAP {len(locs)} urls")
        for l in locs: print("  SM", l)
        pages |= set(locs)
home = get("https://lvpc.org/")
if home is not None:
    soup = BeautifulSoup(home.text, "html.parser")
    for a in soup.find_all("a", href=True):
        pages.add(urljoin("https://lvpc.org/", a["href"]))
kw = re.compile(r"meet|agenda|review|minute|commission|committee|development|subdivision|zoning|act-?247|reports?|calendar|packet", re.I)
cand = sorted(p for p in pages if "lvpc.org" in p and kw.search(p))
print(f"CANDIDATE PAGES {len(cand)}")
for p in cand[:40]:
    r = get(p)
    if r is None or not r.ok or "html" not in r.headers.get("content-type", ""): continue
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        h = urljoin(p, a["href"]); t = " ".join(a.get_text(" ", strip=True).split())[:90]
        if re.search(r"\.pdf|img1\.wsimg|drive\.google|docs\.google|dropbox|onedrive|sharepoint|agenda|minutes|review", h + " " + t, re.I):
            links.append((t, h))
    print(f"== PAGE {p}: {len(links)} doc links")
    for t, h in links[:25]: print(f"   LINK {t} | {h}")
