"""
ZoningTrack collector for the Lehigh Valley Planning Commission (lvpc.org).

LVPC reviews subdivisions, land developments, rezonings and ordinances for all 62 Lehigh Valley
municipalities. Its monthly Comprehensive Planning Committee and full Commission packets are posted on
https://lvpc.org/lvpc-meetings. This collector downloads those packets (last LOOKBACK_DAYS), extracts text
(OCR if scanned), and splits long packets into parts at page boundaries so the AI step reads every review.
Robots-aware and paced (shared PoliteSession). Output: data/lvpc/documents.json and data/lvpc/text/*.txt
"""
import hashlib, io, json, os, re, sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup
from pypdf import PdfReader

sys.path.insert(0, os.path.dirname(__file__))
from civicplus import PoliteSession, ocr_text, FOUND, record_health

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "190"))
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
KEY, NAME, COUNTY = "lvpc", "Lehigh Valley Planning Commission", "Lehigh/Northampton"
PAGE = "https://lvpc.org/lvpc-meetings"
PART_CHARS = 50000
MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def board_of(fname):
    f = fname.lower()
    if re.search(r"comp(rehensive)?\.?\s*plan", f):
        return "Comprehensive Planning Committee"
    if re.search(r"environment|transport|trans comm|lvts|tip|futurelv|workshop|bike|freight|safety", f):
        return None
    if re.search(r"commission|full|lvpc\s*(meeting|packet|agenda)|board", f):
        return "Commission"
    return None


def month_year(fname, text=""):
    f = unquote(fname).lower()
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s_-]*(20\d\d|\d\d)?\b", f)
    y = re.search(r"\b(20\d\d)\b", f)
    num = re.search(r"\b(\d{1,2})[-_](20\d\d)\b", f)
    mon = MON[m.group(1)] if m else (int(num.group(1)) if num else None)
    year = int(y.group(1)) if y else (2000 + int(m.group(2)) if m and m.group(2) and len(m.group(2)) == 2 else None)
    if mon is None:
        t = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),?\s+(20\d\d)", text[:4000], re.I)
        if t:
            return date(int(t.group(3)), MON[t.group(1)[:3].lower()], int(t.group(2)))
        return None
    if year is None:
        today = date.today()
        year = today.year if mon <= today.month else today.year - 1
    return date(year, mon, 1)


def pages_text(content):
    reader = PdfReader(io.BytesIO(content))
    pages = [(p.extract_text() or "") for p in reader.pages]
    if sum(len(p.strip()) for p in pages) < 50 * len(pages):  # scanned
        from pdf2image import convert_from_bytes
        import pytesseract
        pages = [pytesseract.image_to_string(img) for img in convert_from_bytes(content, dpi=300)]
        return pages, True
    return pages, False


def split_parts(pages):
    parts, cur, size = [], [], 0
    for i, t in enumerate(pages, 1):
        block = f"\n\n[page {i}]\n{t}"
        if cur and size + len(block) > PART_CHARS:
            parts.append("".join(cur)); cur, size = [], 0
        cur.append(block); size += len(block)
    if cur:
        parts.append("".join(cur))
    return parts


def collect():
    out_dir = os.path.join(DATA_DIR, KEY); text_dir = os.path.join(out_dir, "text")
    os.makedirs(text_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "documents.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    seen_file = {v["file_sha256"] for v in index.values()}

    site = PoliteSession("https://lvpc.org")
    html = site.get(PAGE).text
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(PAGE, a["href"])
        if re.search(r"\.pdf(\?|$)", url, re.I):
            links.setdefault(url.split("?")[0], url)
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    todo, skipped = [], []
    for base, url in links.items():
        fname = unquote(urlparse(base).path.rsplit("/", 1)[-1])
        board = board_of(fname)
        if not board:
            skipped.append(fname); continue
        d = month_year(fname)
        if d and d < cutoff:
            continue
        todo.append((url, fname, board, d))
    FOUND[KEY] = len(links)
    print(f"[lvpc] {len(links)} PDFs on meetings page; {len(todo)} planning/commission packets in range; "
          f"{len(skipped)} other committees skipped")

    cdn = {}
    added = 0
    for url, fname, board, d in todo:
        host = "{0.scheme}://{0.netloc}".format(urlparse(url))
        try:
            sess = cdn.setdefault(host, PoliteSession(host))
            content = sess.get(url).content
            if not content.startswith(b"%PDF"):
                raise ValueError("not a PDF")
        except Exception as e:
            print(f"  ! {fname}: {e}"); continue
        fsha = hashlib.sha256(content).hexdigest()
        if fsha in seen_file:
            continue
        pages, ocr = pages_text(content)
        meeting = d or month_year(fname, "\n".join(pages[:3]))
        parts = split_parts(pages)
        slug = re.sub(r"[^a-z]+", "-", board.lower()).strip("-")
        for n, text in enumerate(parts, 1):
            doc_id = f"packet_{slug}_{meeting.isoformat() if meeting else 'undated'}_{fsha[:10]}_p{n}"
            index[doc_id] = dict(municipality=NAME, county=COUNTY, board=f"LVPC {board}", kind="packet",
                                 meeting_date=meeting.isoformat() if meeting else None, title=fname,
                                 url=url, source_page=PAGE, sha256=hashlib.sha256(text.encode()).hexdigest(),
                                 file_sha256=fsha, part=n, parts=len(parts), pages=len(pages),
                                 chars=len(text.strip()), ocr=ocr, regional=True,
                                 collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            open(os.path.join(text_dir, doc_id + ".txt"), "w").write(text)
        seen_file.add(fsha); added += 1
        print(f"  + {meeting} {board}: {fname} ({len(pages)}p, {len(parts)} parts{', OCR' if ocr else ''})")
        json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    if skipped:
        print("  skipped:", "; ".join(sorted(set(skipped))[:60]))
    return added


if __name__ == "__main__":
    try:
        n = collect(); record_health(KEY, new=n); print("SUMMARY lvpc", n)
    except Exception as e:
        print(f"[lvpc] SKIPPED: {e}"); record_health(KEY, error=e)
