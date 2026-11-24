"""Find where a town's website posts agendas and minutes (used when setting up the page watcher).

Starting from the pages in registry/out/<region>_registry.csv (home, documents, planning, zoning), follows links on the
same site whose text or address looks like meetings/agendas/minutes/boards, two levels deep, and prints every page that
links to documents (PDF, Word, Google Drive) with a sample of those links. Robots-aware and paced (PoliteSession).
Usage: python tools/discover_pages.py berks reading cumru-berks ...
"""
import csv, os, re, sys
from collections import deque
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
from civicplus import PoliteSession

PAGEY = re.compile(r"agenda|minute|meeting|planning|zoning|council|supervisor|commission|board|hearing|government|document|calendar|archive", re.I)
DOC = re.compile(r"\.(pdf|docx?)(\?|$)|drive\.google\.com/(file|open|uc)|docs\.google\.com|/DocumentCenter/View/|/ViewFile/|download", re.I)
SKIP = re.compile(r"\.(jpe?g|png|gif|zip|mp4|mp3)(\?|$)|mailto:|tel:|facebook|twitter|instagram|youtube|#$", re.I)
MAX_PAGES = 40


def main(region, keys):
    rows = {r["key"]: r for r in csv.DictReader(open(f"registry/out/{region}_registry.csv"))}
    for key in keys:
        r = rows[key]
        starts = [u for u in (r["website"], r["documents_page"], r["planning_link"], r["zoning_link"]) if u and u.startswith("http")]
        host = urlparse(r["website"]).netloc.replace("www.", "")
        print(f"\n######## {key} ({r['name']}) {r['website']}")
        try:
            sess = PoliteSession(r["website"])
        except Exception as e:
            print("  ! cannot open:", e); continue
        sess.delay = min(sess.delay, 5)
        seen, queue, n = set(), deque((u, 0) for u in starts), 0
        while queue and n < MAX_PAGES:
            url, depth = queue.popleft()
            url = url.split("#")[0]
            if url in seen or host not in urlparse(url).netloc.replace("www.", ""):
                continue
            seen.add(url)
            try:
                resp = sess.get(url); n += 1
            except Exception as e:
                print(f"  ! {url}: {str(e)[:80]}"); continue
            if "html" not in resp.headers.get("content-type", ""):
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            docs, pages = [], []
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"]); text = a.get_text(" ", strip=True)[:70]
                if SKIP.search(href):
                    continue
                if DOC.search(href):
                    docs.append((text, href))
                elif depth < 2 and (PAGEY.search(text) or PAGEY.search(urlparse(href).path)):
                    pages.append(href)
            for p in pages:
                if p not in seen:
                    queue.append((p, depth + 1))
            if docs:
                years = re.findall(r"20(2[4-7])", " ".join(t + " " + unquote(h) for t, h in docs))
                print(f"  PAGE {url}  docs={len(docs)}  years={sorted(set(years))}")
                for t, h in docs[:12]:
                    print(f"      - {t!r} {h[:150]}")
        print(f"  ({n} pages read)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
