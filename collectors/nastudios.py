"""
ZoningTrack collector for township websites built on the na studios template
(a Lehigh Valley web designer's hand-coded layout shared by ~15 municipalities).

What these sites publish:
  - the home page holds each board's CURRENT agenda (Supervisors/Council, Planning Commission,
    Zoning Hearing Board, ...) as a PDF with a reused file name (e.g. planagenda.pdf) or a
    public Google Drive file -- overwritten when the next agenda goes up;
  - a minutes page lists the governing body's dated minutes (minutes/05jan26.pdf).

Because current agendas are overwritten, documents are tracked by CONTENT (sha256), not URL:
each run re-checks the current agendas and stores any version not seen before. Dated files are
downloaded once. Honors robots.txt for every host, paces requests per host, keeps going if a
site is down. Output: data/<key>/documents.json and data/<key>/text/<doc_id>.txt.
"""
import hashlib, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from civicplus import PoliteSession, pdf_text, ocr_text  # shared helpers

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "190"))
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

SITES = [
    ("alburtis", "Alburtis Borough", "Lehigh", "https://www.alburtis.org/"),
    ("coplay", "Coplay Borough", "Lehigh", "https://www.coplayborough.org/"),
    ("macungie", "Macungie Borough", "Lehigh", "https://www.macungie.pa.us/"),
    ("hanover-lehigh", "Hanover Township (Lehigh)", "Lehigh", "https://www.hanleco.org/"),
    ("upper-milford", "Upper Milford Township", "Lehigh", "https://www.uppermilford.net/"),
    ("washington-lehigh", "Washington Township (Lehigh)", "Lehigh", "https://www.washingtonlehigh.org/"),
    ("weisenberg", "Weisenberg Township", "Lehigh", "https://www.weisenbergtownship.org/"),
    ("allen", "Allen Township", "Northampton", "https://www.allentownship.org/"),
    ("bethlehem-twp", "Bethlehem Township", "Northampton", "https://www.bethlehemtownship.org/"),
    ("bushkill", "Bushkill Township", "Northampton", "https://bushkilltownship.com/"),
    ("lehigh-twp", "Lehigh Township", "Northampton", "https://www.lehightownship.com/"),
    ("lower-mount-bethel", "Lower Mount Bethel Township", "Northampton", "https://www.lowermtbethel.org/"),
    ("moore", "Moore Township", "Northampton", "https://mooretownship.org/"),
    ("plainfield", "Plainfield Township", "Northampton", "https://plainfieldtownship.org/"),
    ("washington-northampton", "Washington Township (Northampton)", "Northampton", "https://washington-township.com/"),
]

BOARDS = [  # (label, pattern on link text / file name / surrounding text)
    ("Zoning Hearing Board", r"zoning hearing|\bzhb|zoneagenda|zoning board"),
    ("Planning Commission", r"planning|\bpc\b|pcagenda|planagenda|[_\- ]pc[_\-. ]"),
    ("Governing Body", r"supervisors|commissioners|council|\bbos\b|\bboc\b|bosagenda|bos[_\-.]|boc[_\-.]|re-?org|current agenda"),
]
HARD_SKIP = r"environmental|\beac\b|recreation|auditor|historic|agricultural|emergency services|open space|community days|parks?\b|fire company|library"
SKIP = r"trash|recycl|newsletter|garbage|dog|tax|insert|flyer|rfp|budget|leaf|calendar|sanitation|guideline|tour|ambulance|detour|senior|vet|caregiver|grandparent|disposal|hhw|fmnp|sba|yardwaste|winter|curbside|wmfaq|recreation|recagenda|auditor|audagenda|historic|histagenda|community days|cdagenda|environmental|eac|emergency|open space|agricultural|park"
PAGE_RX = r"minut|agenda|plann|zhb|hearing|pc[-_]|bos[-_]|boc[-_]|council"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(*texts):
    for t in texts:
        if not t:
            continue
        m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s*,?\s*(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(20\d\d)", t, re.I)
        if m:
            return date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        m = re.search(r"(?<!\d)(\d{1,2})[_\-](\d{1,2})[_\-](20\d\d)", t)  # 11_5_2026
        if m:
            try: return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError: pass
        m = re.search(r"(?<!\d)(\d{1,2})(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(\d{2}|20\d\d)(?!\d)", t, re.I)  # 05jan26, 28jan2026
        if m:
            y = int(m.group(3)); y = y if y > 99 else 2000 + y
            try: return date(y, MONTHS[m.group(2).lower()], int(m.group(1)))
            except ValueError: pass
    return None


def board_of(*texts):
    blob = " ".join(t for t in texts if t)
    for label, rx in BOARDS:
        if re.search(rx, blob, re.I):
            return label
    return None


def drive_id(url):
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=download&)?id=)([\w-]{20,})", url)
    return m.group(1) if m else None


def find_docs(html, page_url, is_home):
    soup = BeautifulSoup(html, "html.parser")
    docs = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip().split('"')[0]
        url = urljoin(page_url, href)
        did = drive_id(url)
        if not (did or re.search(r"\.pdf(\?|$)", url, re.I)):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        context = " ".join((a.find_parent(["li", "p", "td", "tr", "div"]) or a).get_text(" ", strip=True).split())[:200]
        fname = urlparse(url).path.rsplit("/", 1)[-1]
        generic = not text or re.fullmatch(r"(click )?here\.?|view|link(-\d)?|current agenda.*|agenda", text, re.I) is not None
        if re.search(HARD_SKIP, f"{text} {fname}", re.I) or (generic and re.search(HARD_SKIP, context[:120], re.I)):
            continue
        if re.search(SKIP, f"{text} {fname}", re.I) and not re.search(r"zoning|planning|supervisor|council|commission", text, re.I):
            continue
        board = board_of(text, fname, context if (generic or len(context) < 120) else "")
        in_minutes_dir = "/minutes/" in url or "/council/" in url
        meeting = parse_date(text, fname, context if generic else "")
        if not board:
            if in_minutes_dir and meeting:
                board = "Governing Body"   # dated minutes on the governing body's minutes page
            else:
                continue
        kind = "minutes" if (in_minutes_dir and "agenda" not in fname.lower()) or \
            (generic and "minutes" in context.lower() and "agenda" not in context.lower()) else "agenda"
        # Re-check every run: undated links, and any non-Drive file whose NAME carries no date
        # (fixed names like bosagenda.pdf are overwritten with each new agenda).
        rolling = meeting is None or (not did and parse_date(fname) is None)
        if rolling and not did:
            meeting = None  # date comes from the document text instead
        docs.append({"url": url, "drive_id": did, "text": text or fname, "board": board, "kind": kind,
                     "meeting_date": meeting.isoformat() if meeting else None, "rolling": rolling,
                     "source_page": page_url, "on_home_page": is_home})
    return docs


def fetch_doc(sess, drive_sess, d):
    if d["drive_id"]:
        dl = f"https://drive.usercontent.google.com/download?id={d['drive_id']}&export=download&confirm=t"
        r = drive_sess.get(dl)
    else:
        r = sess.get(d["url"])
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"not a PDF ({r.headers.get('content-type', '?')})")
    return r.content


def collect(site):
    key, name, county, base = site
    out_dir = os.path.join(DATA_DIR, key); text_dir = os.path.join(out_dir, "text")
    os.makedirs(text_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "documents.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    seen_hash = {v["sha256"] for v in index.values()}
    seen_url = {v["url"] for v in index.values() if not v.get("rolling")}

    sess = PoliteSession(base)
    drive_sess = PoliteSession("https://drive.usercontent.google.com/")
    home = sess.get(base)
    docs = find_docs(home.text, home.url, True)
    host = urlparse(home.url).netloc
    soup = BeautifulSoup(home.text, "html.parser")
    pages = []
    for a in soup.find_all("a", href=True):
        u = urljoin(home.url, a["href"]).split("#")[0]
        if urlparse(u).netloc == host and not re.search(r"\.pdf|zoning-map|scrollto=", u, re.I) \
                and re.search(PAGE_RX, u + " " + a.get_text(" ", strip=True), re.I) and u not in pages:
            pages.append(u)
    for p in pages[:6]:
        try:
            docs += find_docs(sess.get(p).text, p, False)
        except Exception as e:
            print(f"  [{key}] page skipped {p}: {e}")

    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    unique = {}
    for d in docs:
        unique.setdefault(d["drive_id"] or d["url"], d)
    todo = [d for d in unique.values()
            if (d["rolling"] or d["url"] not in seen_url) and (d["meeting_date"] is None or d["meeting_date"] >= cutoff)]
    print(f"[{key}] {len(unique)} documents linked, checking {len(todo)}")

    added = 0
    for d in todo:
        try:
            content = fetch_doc(sess, drive_sess, d)
        except PermissionError as e:
            print(f"  [{key}] robots: {e}"); continue
        except Exception as e:
            print(f"  [{key}] ! {d['text'][:40]}: {e}"); continue
        sha = hashlib.sha256(content).hexdigest()
        if sha in seen_hash:
            continue
        text, pages_n = pdf_text(content)
        needs_ocr = len(text.strip()) < 50 * pages_n
        if needs_ocr:
            text = ocr_text(content)
        meeting = d["meeting_date"] or (parse_date(text[:3000]) or None)
        meeting = meeting.isoformat() if isinstance(meeting, date) else meeting
        board_slug = re.sub(r"[^a-z]+", "-", d["board"].lower()).strip("-")
        doc_id = f"{d['kind']}_{board_slug}_{meeting or 'undated'}_{sha[:10]}"
        index[doc_id] = dict(d, municipality=name, county=county, meeting_date=meeting, sha256=sha,
                             pages=pages_n, chars=len(text.strip()), ocr=needs_ocr,
                             collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        open(os.path.join(text_dir, doc_id + ".txt"), "w").write(text)
        seen_hash.add(sha); added += 1
        print(f"  + {meeting or 'undated'} {d['board']} {d['kind']} ({pages_n}p{', OCR' if needs_ocr else ''})")
        json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    return key, added


def run(site):
    try:
        return collect(site)
    except Exception as e:
        print(f"[{site[0]}] SKIPPED: {e}")
        return site[0], None


if __name__ == "__main__":
    only = sys.argv[1:]
    todo = [s for s in SITES if not only or s[0] in only]
    with ThreadPoolExecutor(max_workers=6) as ex:  # different towns in parallel; each host is paced
        results = list(ex.map(run, todo))
    print("SUMMARY", {k: v for k, v in results})
