"""
ZoningTrack collector for the Berks County Planning Commission (berkspa.gov).

BCPC reviews subdivision and land development plans for all Berks County municipalities and publishes a monthly
Subdivision & Land Development Report: every plan reviewed that period (file #, name, municipality, location,
applicant, developer, owner, units/lots, square feet, acreage, sewer/water) plus monthly and year-to-date totals.
Reports are listed at https://www.berkspa.gov/Departments/Planning-Commission/Subdivision.

This collector downloads the reports published since SINCE (default Jan 2025), extracts text (OCR if scanned) and
stores them as regional documents; the AI step files each plan under its own municipality.
Robots-aware and paced (shared PoliteSession). Output: data/bcpc/documents.json and data/bcpc/text/*.txt
"""
import hashlib, json, os, re, sys
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from civicplus import PoliteSession, FOUND, record_health
from lvpc import pages_text, split_parts

SINCE = date.fromisoformat(os.environ.get("BCPC_SINCE", "2025-01-01"))
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
KEY, NAME, COUNTY = "bcpc", "Berks County Planning Commission", "Berks"
PAGE = "https://www.berkspa.gov/Departments/Planning-Commission/Subdivision"
MONTHS = "january february march april may june july august september october november december".split()


def period_end(text, fname):
    """The report covers e.g. 'May 21, 2026 to June 16, 2026' -> date(2026, 6, 16). Falls back to the file name."""
    head = text[:6000]
    dates = re.findall(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),?\s+(20\d\d)", head, re.I)
    if dates:
        ds = sorted(date(int(y), MONTHS.index(m.lower()) + 1, int(d)) for m, d, y in dates[:4])
        return ds[-1]
    f = unquote(fname).lower()
    y = re.search(r"\b(20\d\d)\b", f)
    m = re.findall(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", f)
    if y and m:
        mon = [x[:3] for x in MONTHS].index(m[-1]) + 1
        yr = int(y.group(1)) + (1 if len(m) > 1 and mon == 1 else 0)
        return date(yr, mon, 1)
    return None


def collect():
    out_dir = os.path.join(DATA_DIR, KEY); text_dir = os.path.join(out_dir, "text")
    os.makedirs(text_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "documents.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    seen_file = {v["file_sha256"] for v in index.values()}

    site = PoliteSession("https://www.berkspa.gov")
    soup = BeautifulSoup(site.get(PAGE).text, "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(PAGE, a["href"])
        fname = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        if re.search(r"reports?\.pdf$", fname, re.I) and not re.search(r"^current", fname, re.I):
            links.setdefault(url.split("?")[0], (url, fname, a.get_text(" ", strip=True)))
    FOUND[KEY] = len(links)
    todo = []
    for base, (url, fname, label) in links.items():
        y = re.search(r"\b(20\d\d)\b", fname)
        if y and int(y.group(1)) < SINCE.year - 1:   # cheap pre-filter; exact check after reading the dates
            continue
        todo.append((url, fname))
    print(f"[bcpc] {len(links)} monthly reports listed; {len(todo)} to check (since {SINCE})")

    added = 0
    for url, fname in todo:
        try:
            content = site.get(url).content
            if not content.startswith(b"%PDF"):
                raise ValueError("not a PDF")
        except Exception as e:
            print(f"  ! {fname}: {e}"); continue
        fsha = hashlib.sha256(content).hexdigest()
        if fsha in seen_file:
            continue
        pages, ocr = pages_text(content)
        end = period_end("\n".join(pages[:2]), fname)
        if end and end < SINCE:
            seen_file.add(fsha); continue
        parts = split_parts(pages)
        for n, text in enumerate(parts, 1):
            doc_id = f"report_{end.isoformat() if end else 'undated'}_{fsha[:10]}_p{n}"
            index[doc_id] = dict(municipality=NAME, county=COUNTY, board="Berks County Planning Commission", kind="report",
                                 meeting_date=end.isoformat() if end else None, title=fname, url=url, source_page=PAGE,
                                 sha256=hashlib.sha256(text.encode()).hexdigest(), file_sha256=fsha, part=n, parts=len(parts),
                                 pages=len(pages), chars=len(text.strip()), ocr=ocr, regional=True,
                                 collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            open(os.path.join(text_dir, doc_id + ".txt"), "w").write(text)
        seen_file.add(fsha); added += 1
        print(f"  + period ending {end}: {fname} ({len(pages)}p{', OCR' if ocr else ''})")
        json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    return added


if __name__ == "__main__":
    try:
        n = collect(); record_health(KEY, new=n); print("SUMMARY bcpc", n)
    except Exception as e:
        print(f"[bcpc] SKIPPED: {e}"); record_health(KEY, error=e)
