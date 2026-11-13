"""
ZoningTrack collector for municipalities on the CivicPlus "Agenda Center" platform.

For each configured municipality it:
  1. checks the site's robots.txt and honors its rules and crawl delay,
  2. reads the Agenda Center page and finds every agenda and minutes document,
  3. downloads new documents (within LOOKBACK_DAYS) and extracts their text,
  4. saves text to data/<muni>/text/<doc_id>.txt and a record to data/<muni>/documents.json.

It is incremental: documents already collected are skipped, so daily runs only fetch what's new.
"""
import io, json, os, re, sys, time
from datetime import date, datetime, timedelta, timezone
from urllib import robotparser
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

UA = "ZoningTrack/0.1 (+https://zoningtrack.com; public-records collector)"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "190"))
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

MUNICIPALITIES = [
    {"key": "forks", "name": "Forks Township", "county": "Northampton",
     "base": "https://www.forkstownship.org"},
    # Berks County (Nov 2026)
    {"key": "boyertown-berks", "name": "Boyertown Borough", "county": "Berks", "base": "https://www.boyertownborough.org"},
    {"key": "marion-berks", "name": "Marion Township", "county": "Berks", "base": "https://www.mariontwpberks.com"},
    {"key": "muhlenberg-berks", "name": "Muhlenberg Township", "county": "Berks", "base": "https://www.muhlenbergtwp.com"},
    {"key": "south-heidelberg-berks", "name": "South Heidelberg Township", "county": "Berks", "base": "https://www.shtwp.org"},
    {"key": "womelsdorf-berks", "name": "Womelsdorf Borough", "county": "Berks", "base": "https://www.womelsdorfborough.org"},
    {"key": "wyomissing-berks", "name": "Wyomissing Borough", "county": "Berks", "base": "https://wyomissingboro.org"},
]
# Boards worth reading for development activity; parks, library, pension, police etc. are skipped
KEEP_BOARD = re.compile(r"planning|zoning|council|supervisors|commissioners|redevelop|historic|harb|land develop|subdivision|"
                        r"economic|municipal authority|board of trustees|hearing", re.I)
SKIP_BOARD = re.compile(r"park|recreation|library|shade tree|pension|police|fire|civil service|audit|emergency|ems\b|"
                        r"veteran|holiday|environmental advisory|tree|youth|senior|\barts?\b", re.I)


FOUND = {}  # source key -> documents found on its listing page this run (filled by every collector)


def record_health(key, new=None, error=None):
    """Append this run's outcome for one source to data/<key>/health.json (last 90 runs) for the health check."""
    path = os.path.join(DATA_DIR, key, "health.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        hist = json.load(open(path))
    except Exception:
        hist = []
    hist.append(dict(run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     status="error" if error else "ok", found=FOUND.get(key), new=new,
                     error=(str(error)[:300] if error else None)))
    json.dump(hist[-90:], open(path, "w"), indent=0)


class PoliteSession:
    """requests session that obeys robots.txt and waits between requests."""

    def __init__(self, base):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.rp = robotparser.RobotFileParser()
        robots_url = urljoin(base, "/robots.txt")
        r = None
        for i in range(3):
            try:
                r = self.s.get(robots_url, timeout=60); break
            except requests.RequestException:
                if i == 2: raise
                time.sleep(20 * (i + 1))
        if r.status_code in (401, 403):
            self.rp.parse(["User-agent: *", "Disallow: /"])   # rules file refused: stay out
        elif r.status_code >= 400 or "html" in r.headers.get("content-type", "").lower():
            self.rp.parse([])                                  # no rules file: no restrictions
        else:
            self.rp.parse(r.text.splitlines())
        self.delay = max(self.rp.crawl_delay(UA) or 0, 5)
        self.last = 0.0

    def _retry(self, url, tries=4):
        for i in range(tries):
            try:
                r = self.s.get(url, timeout=60)
                r.raise_for_status()
                return r
            except requests.RequestException:
                if i == tries - 1:
                    raise
                time.sleep(30 * (i + 1))

    def get(self, url):
        if not self.rp.can_fetch(UA, url):
            raise PermissionError(f"robots.txt disallows {url}")
        wait = self.last + self.delay - time.time()
        if wait > 0:
            time.sleep(wait)
        r = self._retry(url)
        self.last = time.time()
        return r


def parse_agenda_center(html, base):
    """Return one record per agenda/minutes document listed on an Agenda Center page."""
    soup = BeautifulSoup(html, "html.parser")
    # map category panels to their board names
    boards = {}
    for h2 in soup.select("h2[aria-controls^=category-panel-]"):
        boards[h2["aria-controls"].replace("category-panel-", "")] = h2.get_text(strip=True)

    docs = []
    for table in soup.select("table[id^=table]"):
        board = boards.get(table["id"].replace("table", ""), "Unknown")
        for row in table.select("tr.catAgendaRow"):
            strong = row.select_one("strong[aria-label]")
            m = re.search(r"for (.+)$", strong["aria-label"]) if strong else None
            try:
                meeting_date = datetime.strptime(m.group(1), "%B %d, %Y").date()
            except (AttributeError, ValueError):
                continue
            for kind, sel in (("agenda", "a[href*='/ViewFile/Agenda/']"),
                              ("minutes", "a[href*='/ViewFile/Minutes/']")):
                a = row.select_one(sel)
                if not a:
                    continue
                href = a["href"].split("?")[0]
                docs.append({
                    "doc_id": f"{kind}{href.rsplit('/', 1)[-1]}",
                    "board": board,
                    "kind": kind,
                    "title": a.get_text(" ", strip=True) if kind == "agenda" else f"{board} Minutes",
                    "meeting_date": meeting_date.isoformat(),
                    "url": urljoin(base, href),
                })
    return docs


def pdf_text(content):
    reader = PdfReader(io.BytesIO(content))
    return "\n\n".join((p.extract_text() or "") for p in reader.pages), len(reader.pages)


def ocr_text(content):
    """Read text from a scanned PDF with Tesseract OCR (installed on the GitHub runner)."""
    from pdf2image import convert_from_bytes
    import pytesseract
    pages = convert_from_bytes(content, dpi=300)
    return "\n\n".join(pytesseract.image_to_string(img) for img in pages)


def collect(muni):
    out_dir = os.path.join(DATA_DIR, muni["key"])
    text_dir = os.path.join(out_dir, "text")
    os.makedirs(text_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "documents.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}

    sess = PoliteSession(muni["base"])
    listing = parse_agenda_center(sess.get(urljoin(muni["base"], "/AgendaCenter")).text, muni["base"])
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    # new documents, plus earlier scanned documents that haven't been OCR'd yet
    listing = [d for d in listing if KEEP_BOARD.search(d["board"]) and not SKIP_BOARD.search(d["board"])] \
        if muni["key"] != "forks" else listing   # Forks keeps its existing all-boards history
    todo = [d for d in listing if d["meeting_date"] >= cutoff.isoformat()
            and (d["doc_id"] not in index or (index[d["doc_id"]].get("needs_ocr") and not index[d["doc_id"]].get("ocr")))]
    FOUND[muni["key"]] = len(listing)
    print(f"[{muni['key']}] {len(listing)} listed, {len(todo)} new within {LOOKBACK_DAYS} days "
          f"(crawl delay {sess.delay}s)")

    for d in todo:
        rec = dict(d, municipality=muni["name"], county=muni["county"],
                   collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        try:
            r = sess.get(d["url"])
            text, pages = pdf_text(r.content)
            needs_ocr = len(text.strip()) < 50 * pages
            if needs_ocr:
                text = ocr_text(r.content)
            rec.update(pages=pages, chars=len(text.strip()), needs_ocr=needs_ocr, ocr=needs_ocr)
            open(os.path.join(text_dir, d["doc_id"] + ".txt"), "w").write(text)
        except Exception as e:  # keep going; record the failure so it can be retried
            print(f"  ! {d['doc_id']}: {e}")
            continue
        index[d["doc_id"]] = rec
        print(f"  + {d['meeting_date']} {d['board']} {d['kind']} ({rec['pages']}p, {rec['chars']} chars)")
        json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)

    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    return len(todo)


if __name__ == "__main__":
    only = sys.argv[1:]
    for m in MUNICIPALITIES:
        if not only or m["key"] in only:
            try:
                record_health(m["key"], new=collect(m))
            except Exception as e:  # one township being down shouldn't stop the others
                print(f"[{m['key']}] SKIPPED: {e}")
                record_health(m["key"], error=e)
