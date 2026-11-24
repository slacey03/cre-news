"""
ZoningTrack page watcher: for municipalities whose websites are custom-built (WordPress, Joomla, Wix, hand-coded...),
watch the specific pages where each board posts its agendas and minutes, and pick up new dated documents.

Each site lists one or more pages. A page either belongs to one board (Planning Commission, Zoning Hearing Board,
the governing body) or mixes boards, in which case the board is read from each link's text or file name. Only boards
that matter for development are kept (parks, environmental, library, pension and similar are skipped). A document needs
a meeting date in its link text or file name and must fall within LOOKBACK_DAYS; files already collected are skipped.

Robots-aware and paced per host (shared PoliteSession), keeps going if a site or a file fails, records health for the
daily health check. Output: data/<key>/documents.json and data/<key>/text/<doc_id>.txt (same format as civicplus).
"""
import hashlib, html as htmllib, io, json, os, re, sys, zipfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from civicplus import PoliteSession, pdf_text, ocr_text, FOUND, record_health

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "190"))
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

PC, ZHB = "Planning Commission", "Zoning Hearing Board"
# (key, name, county, [(page url, board or None = read from each link)])
SITES = [
    ("reading", "City of Reading", "Berks", [
        ("https://www.readingpa.gov/planning-commission-1", PC),
        ("https://www.readingpa.gov/zoning-hearing-board", ZHB),
        ("https://www.readingpa.gov/council-meeting-minutes-and-agendas", "City Council")]),
    ("cumru-berks", "Cumru Township", "Berks", [
        ("http://www.cumrutownship.com/boardscommissions/bocmeetingminutes.html", "Board of Commissioners"),
        ("http://www.cumrutownship.com/boardscommissions/planningcommission.html", PC)]),
    ("spring-berks", "Spring Township", "Berks", [
        ("https://www.springtwpberks.org/planning-commission", PC),
        ("https://www.springtwpberks.org/zoning-hearing-board", ZHB),
        ("https://www.springtwpberks.org/board-of-supervisors", "Board of Supervisors")]),
    # batch 2
    ("bern-berks", "Bern Township", "Berks", [
        ("http://www.berntownship.org/newsite/township-of-bern/planning-commission/", PC),
        ("http://www.berntownship.org/newsite/township-of-bern/zoning-hearing-board/", ZHB),
        ("http://www.berntownship.org/newsite/board-of-supervisors/", "Board of Supervisors")]),
    ("birdsboro-berks", "Birdsboro Borough", "Berks", [
        ("https://birdsboropa.org/council/", "Borough Council"),
        ("https://birdsboropa.org/planning-commission/", PC)]),
    ("colebrookdale-berks", "Colebrookdale Township", "Berks", [
        ("https://colebrookdale.org/meeting-minutes", None, {"doc": r"download\.aspx\?downloadid=|\.pdf(\?|$)"})]),
    ("kutztown-berks", "Kutztown Borough", "Berks", [
        ("https://www.kutztownboro.org/elected-officials/meetings/", "Borough Council"),
        ("https://www.kutztownboro.org/commissions/pc/", PC),
        ("https://www.kutztownboro.org/elected-officials/committees/", "Community Development & Public Safety Committee",
         {"match": r"cdps"})]),
    ("lower-heidelberg-berks", "Lower Heidelberg Township", "Berks", [
        ("https://lowerheidelbergtownship.org/meeting-minutes/", None)]),
    ("shillington-berks", "Shillington Borough", "Berks", [
        ("https://shillingtonboro.com/minutes-reports/", None)]),
    # batch 3
    ("topton-berks", "Topton Borough", "Berks", [
        ("https://toptonborough.com/index.php/council-minutes/", "Borough Council", {"kind": "minutes"})]),
    ("mohnton-berks", "Mohnton Borough", "Berks", [
        ("https://mohntonboro.org/agenda-minutes/", None, {"default": "Borough Council"})]),
    ("mount-penn-berks", "Mount Penn Borough", "Berks", [
        ("https://mtpennborough.com/meeting-agendas", None, {"default": "Borough Council"}),
        ("https://mtpennborough.com/meeting-minutes", None, {"default": "Borough Council", "kind": "minutes"})]),
    ("centre-berks", "Centre Township", "Berks", [
        ("https://centretownship.com/meeting-minutes/", None, {"default": "Board of Supervisors", "kind": "minutes"})]),
    ("hereford-berks", "Hereford Township", "Berks", [
        ("https://www.herefordtownship.org/minutes-and-agendas", None, {"default": "Board of Supervisors"})]),
    ("albany-berks", "Albany Township", "Berks", [
        ("https://www.albanytwpberks.org/meeting-minutes", None, {"default": "Board of Supervisors", "kind": "minutes"})]),
    ("tulpehocken-berks", "Tulpehocken Township", "Berks", [
        ("https://tulpytwp.org/minutes/", None, {"default": "Board of Supervisors"})]),
    ("jefferson-berks", "Jefferson Township", "Berks", [
        ("https://jeffersontwpbc.com/meet-dates/", None, {"default": "Board of Supervisors"})]),
    ("north-heidelberg-berks", "North Heidelberg Township", "Berks", [
        ("https://northheidelbergtwp.org/board-of-supervisors-agenda/", None, {"default": "Board of Supervisors"})]),
]
# Not yet: Maidencreek (files on Dropbox, no year in links), West Reading (nothing posted since late 2025),
# Sinking Spring, Shoemakersville, Bernville, Bethel and Penn (no recent agendas/minutes online).
# Not collected: Robeson and Brecknock townships post their files on a host whose robots.txt disallows automated access
# (irp.cdn-website.com). Needs the township's permission or another source.

DOC = re.compile(r"\.(pdf|docx)(\?|$)", re.I)
# boards that aren't about development: never collected, whatever page they sit on
OFF_BOARD = re.compile(r"\beac\b|environmental|park|recreation|library|pension|police|fire|civil service|audit|emergency|"
                       r"shade tree|tree commission|veteran|youth|senior|arts?\b|human relations|blight|code.{0,12}appeal|"
                       r"building.{0,12}appeal|ag(ricultural)? security|redevelopment authority|water authority", re.I)
# non-meeting files that sit on the same pages
NOT_MEETING = re.compile(r"application|checklist|fee|schedule|calendar|manual|specification|ordinance section|map\b|"
                         r"newsletter|budget|form\b|instructions|guide|policy", re.I)
BOARD_FROM_TEXT = [(ZHB, re.compile(r"zoning hearing|\bzhb\b|zoning board", re.I)),
                   (PC, re.compile(r"planning commission|\bpc\b|planning", re.I)),
                   ("Governing Body", re.compile(r"supervisors|\bbos\b|commissioners|\bboc\b|council|re-?organi[sz]ation", re.I))]
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _d(y, m, d):
    y = int(y); y = y + 2000 if y < 100 else y
    try:
        return date(y, int(m), int(d))
    except ValueError:
        return None


def parse_date(*texts):
    """First meeting date found: 'November 17, 2026', 'November_17_2026', '11/17/26', '11-23-26', '1.5.26',
    '02.19.2026', '2026-01-05', '01052026' (MMDDYYYY), '05jan26'."""
    for t in texts:
        if not t:
            continue
        t = unquote(t)
        s = re.sub(r"[_+]", " ", t)
        m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*-?\s*(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*-?\s*(20\d\d)", s, re.I)
        if m and (d := _d(m.group(3), MONTHS[m.group(1)[:3].lower()], m.group(2))):
            return d
        m = re.search(r"(?<!\d)(20\d\d)-(\d{1,2})-(\d{1,2})(?!\d)", t)
        if m and (d := _d(m.group(1), m.group(2), m.group(3))):
            return d
        m = re.search(r"(?<![\d.])(\d{1,2})[/.\-](\d{1,2})[/.\-](20\d\d|\d\d)(?![\d])", t)
        if m and (d := _d(m.group(3), m.group(1), m.group(2))):
            return d
        m = re.search(r"(?<!\d)(0[1-9]|1[0-2])([0-3]\d)(20\d\d)(?!\d)", t)
        if m and (d := _d(m.group(3), m.group(1), m.group(2))):
            return d
        m = re.search(r"(?<!\d)(\d{1,2})(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(\d{2}|20\d\d)(?!\d)", t, re.I)
        if m and (d := _d(m.group(3), MONTHS[m.group(2).lower()], m.group(1))):
            return d
    for t in texts:   # last resort: month and year only ('January 2026 Agenda', 'CDPSAgendaJanuary2026', '03_2026_PC') -> 1st of month
        if not t:
            continue
        s = re.sub(r"[_+]", " ", unquote(t))
        m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s\-]*(20\d\d)(?!\d)", s, re.I)
        if m and (d := _d(m.group(2), MONTHS[m.group(1)[:3].lower()], 1)):
            return d
        m = re.search(r"(?<!\d)(20\d\d)-(0[1-9]|1[0-2])(?!\d)", s)   # 2026-10-Borough-Council-Minutes
        if m and (d := _d(m.group(1), m.group(2), 1)):
            return d
        m = re.search(r"(?<![\d.\-/])(0[1-9]|1[0-2])[\s\-](20\d\d)(?!\d)", s)
        if m and (d := _d(m.group(2), m.group(1), 1)):
            return d
    return None


def docx_text(content):
    """Plain text of a Word .docx: paragraphs from word/document.xml."""
    xml = zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml").decode("utf8", "ignore")
    xml = re.sub(r"</w:p>|<w:br/>|<w:tab/>", "\n", xml)
    return htmllib.unescape(re.sub(r"<[^>]+>", "", xml))


def kind_of(text, fname):
    blob = f"{text} {fname}".lower()
    if re.search(r"minute|\bmins?\b", blob):
        return "minutes"
    if re.search(r"decision|order|opinion", blob):
        return "decision"
    if re.search(r"agenda", blob):
        return "agenda"
    return None


def find_docs(html, page_url, page_board, opts=None):
    """One record per dated development-board document linked from a page (duplicate links merged)."""
    opts = opts or {}
    doc_rx = re.compile(opts["doc"], re.I) if opts.get("doc") else DOC
    match_rx = re.compile(opts["match"], re.I) if opts.get("match") else None
    soup = BeautifulSoup(html, "html.parser")
    best = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip()).split("#")[0]
        if not doc_rx.search(url):
            continue
        text = " ".join(a.get_text(" ", strip=True).replace("\xa0", " ").split())
        if url not in best or len(text) > len(best[url]):
            best[url] = text
    docs = []
    for url, text in best.items():
        fname = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        label = f"{text} {fname}"
        if match_rx and not match_rx.search(f"{label} {url}"):
            continue
        if OFF_BOARD.search(label) or (NOT_MEETING.search(label) and not re.search(r"agenda|minute|hearing", label, re.I)):
            continue
        if re.search(r"cancel", label, re.I):
            continue
        board = page_board or next((b for b, rx in BOARD_FROM_TEXT if rx.search(label)), None) or (opts or {}).get("default")
        if not board:
            continue
        meeting = parse_date(text, fname)
        if not meeting:
            continue
        kind = kind_of(text, fname) or (opts or {}).get("kind") or "agenda"
        docs.append({"doc_id": kind + hashlib.sha1(url.encode()).hexdigest()[:12],
                     "board": board, "kind": kind, "title": text or fname,
                     "meeting_date": meeting.isoformat(), "url": url, "source_page": page_url})
    return docs


def collect(site):
    key, name, county, pages = site
    out_dir = os.path.join(DATA_DIR, key); text_dir = os.path.join(out_dir, "text")
    os.makedirs(text_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "documents.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    sessions = {}

    def session_for(url):
        host = urlparse(url).scheme + "://" + urlparse(url).netloc
        if host not in sessions:
            sessions[host] = PoliteSession(host)
        return sessions[host]

    listing, errors = {}, []
    for page in pages:
        page_url, board, opts = (list(page) + [None])[:3]
        try:
            resp = session_for(page_url).get(page_url)
            found = find_docs(resp.text, page_url, board, opts)
            for d in found:
                listing.setdefault(d["doc_id"], d)
            if not found:   # say what came back, so an empty page can be told from a block or a redesign
                title = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.I | re.S)
                print(f"  ? {page_url}: no dated documents (HTTP {resp.status_code}, {len(resp.text)} bytes, "
                      f"title {(title.group(1).strip()[:60] if title else '-')!r}, {resp.text.lower().count('.pdf')} '.pdf' mentions)")
        except Exception as e:   # one page down shouldn't stop the others
            errors.append(f"{page_url}: {e}")
            print(f"  ! {page_url}: {str(e)[:150]}")
    if errors and not listing:
        raise RuntimeError("; ".join(errors)[:300])
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    todo = [d for d in listing.values() if d["meeting_date"] >= cutoff and d["doc_id"] not in index]
    FOUND[key] = len(listing)
    print(f"[{key}] {len(listing)} dated documents listed, {len(todo)} new within {LOOKBACK_DAYS} days")

    for d in sorted(todo, key=lambda x: x["meeting_date"]):
        rec = dict(d, municipality=name, county=county, collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        rec.pop("doc_id")
        try:
            r = session_for(d["url"]).get(d["url"])
            if r.content.startswith(b"PK"):          # Word (.docx)
                text, pages_n, needs_ocr = docx_text(r.content), 1, False
            elif r.content.startswith(b"%PDF"):
                text, pages_n = pdf_text(r.content)
                needs_ocr = len(text.strip()) < 50 * pages_n
                if needs_ocr:
                    text = ocr_text(r.content)
            else:
                raise ValueError(f"not a PDF or Word file ({r.headers.get('content-type', '?')})")
            rec.update(pages=pages_n, chars=len(text.strip()), needs_ocr=needs_ocr, ocr=needs_ocr)
            open(os.path.join(text_dir, d["doc_id"] + ".txt"), "w").write(text)
        except Exception as e:
            print(f"  ! {d['doc_id']} {d['url']}: {str(e)[:120]}")
            continue
        index[d["doc_id"]] = rec
        print(f"  + {d['meeting_date']} {d['board']} {d['kind']} ({rec['pages']}p, {rec['chars']} chars) {d['title'][:60]}")
        json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    return len(todo)


if __name__ == "__main__":
    only = sys.argv[1:]
    for s in SITES:
        if not only or s[0] in only:
            try:
                record_health(s[0], new=collect(s))
            except Exception as e:
                print(f"[{s[0]}] SKIPPED: {e}")
                record_health(s[0], error=e)
