"""One-off: list every link on LVPC's BuildLV and PlanLV Portal pages, and sample the latest monthly report."""
import io, re, sys
from urllib.parse import urljoin, unquote, urlparse
from bs4 import BeautifulSoup
from pypdf import PdfReader
sys.path.insert(0, "collectors")
from civicplus import PoliteSession
s = PoliteSession("https://lvpc.org")
for p in ["https://lvpc.org/buildlv", "https://lvpc.org/planlv-portal"]:
    soup = BeautifulSoup(s.get(p).text, "html.parser")
    print(f"== {p}")
    for el in soup.find_all(["h1", "h2", "h3", "h4", "a", "iframe", "embed", "object"]):
        if el.name in ("h1", "h2", "h3", "h4"):
            print("  H", el.get_text(" ", strip=True)[:100]); continue
        h = el.get("href") or el.get("src") or el.get("data")
        if not h: continue
        h = urljoin(p, h); t = " ".join(el.get_text(" ", strip=True).split())[:80]
        if "lvpc.org" in urlparse(h).netloc and not re.search(r"\.(pdf|xlsx?)", h, re.I) and el.name == "a": continue
        print(f"  {el.name.upper()} {t} | {unquote(h)[:220]}")
# sample the most recent monthly report
soup = BeautifulSoup(s.get("https://lvpc.org/buildlv").text, "html.parser")
pdfs = [urljoin("https://lvpc.org/buildlv", a["href"]) for a in soup.find_all("a", href=True) if re.search(r"\.pdf", a["href"], re.I)]
cdn = PoliteSession("https://img1.wsimg.com")
for u in pdfs[:2]:
    try:
        c = cdn.get(u).content; r = PdfReader(io.BytesIO(c))
        txt = "\n".join((pg.extract_text() or "") for pg in r.pages[:3])
        print(f"== SAMPLE {unquote(u.split('/')[-1])[:80]} ({len(r.pages)} pages)\n{txt[:3500]}\n== END SAMPLE")
    except Exception as e:
        print("SAMPLE ERROR", u, e)
