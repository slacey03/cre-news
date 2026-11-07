"""One-off: read lvpc.org's full sitemap and list every page's document links that look like project reviews."""
import re, sys, time
from urllib.parse import urljoin, unquote
from bs4 import BeautifulSoup
sys.path.insert(0, "collectors")
from civicplus import PoliteSession
s = PoliteSession("https://lvpc.org")
idx = s.get("https://lvpc.org/sitemap.xml").text
maps = re.findall(r"<loc>(.*?)</loc>", idx)
pages = []
for m in maps:
    if m.endswith(".xml"):
        locs = re.findall(r"<loc>(.*?)</loc>", s.get(m).text); print(f"SITEMAP {m}: {len(locs)}"); pages += locs
    else:
        pages.append(m)
pages = sorted(set(p for p in pages if not p.endswith(".xml")))
print(f"PAGES {len(pages)}")
for p in pages: print("  P", p)
KW = re.compile(r"review|subdivision|land.?dev|act.?247|sald|development report|monthly|annual|building permit|permits?|housing|projects?|zoning|ordinance|comment", re.I)
for p in pages:
    try:
        r = s.get(p)
    except Exception as e:
        print(f"== {p} ERROR {e}"); continue
    if "html" not in r.headers.get("content-type", ""): continue
    soup = BeautifulSoup(r.text, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")[:80]
    docs = []
    for a in soup.find_all("a", href=True):
        h = urljoin(p, a["href"]); t = " ".join(a.get_text(" ", strip=True).split())[:100]
        if re.search(r"\.(pdf|xlsx?|csv|docx?)(\?|$)|wsimg|arcgis|maps|dashboard|tableau|powerbi", h, re.I):
            name = unquote(h.split("/")[-1])[:90]
            if KW.search(t + " " + name): docs.append((t, name, h))
    heads = [h.get_text(" ", strip=True)[:70] for h in soup.find_all(["h1", "h2", "h3"])][:12]
    print(f"== {p} | {title} | {len(docs)} matching docs | headings: {heads}")
    for t, n, h in docs[:30]: print(f"   D {t} | {n} | {h}")
