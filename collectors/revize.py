"""
ZoningTrack collector for township websites on the Revize platform.

Each town lists its documents on per-board pages ("Planning Commission Agendas & Minutes", ...),
with the meeting date in the row text beside each link. This collector reads the configured board
pages, keeps document links that carry a meeting date (or are the board's "current agenda"),
ignores site-wide menu links, and stores new documents (text + metadata) by content hash so
re-posted or overwritten files are handled. Robots-aware and paced (shared PoliteSession).
Output: data/<key>/documents.json and data/<key>/text/<doc_id>.txt
"""
import hashlib, json, os, re, sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from civicplus import PoliteSession, pdf_text, ocr_text

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "190"))
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

GOV, PC, ZHB, NOTICE = "Governing Body", "Planning Commission", "Zoning Hearing Board", "Public Notices"
SITES = {
    "north-whitehall": ("North Whitehall Township", "Lehigh", "https://www.northwhitehall.org/", [
        ("boards_commissions/agendas_and_minutes.php", GOV),
        ("boards_commissions/agendas_and_minutes_.php", PC)]),
    "lower-nazareth": ("Lower Nazareth Township", "Northampton", "https://lowernazareth.com/", [
        ("boards_commissions/agendas___minutes.php", GOV),
        ("boards_commissions/pc_agendas___minutes.php", PC),
        ("boards_commissions/zhb_agendas___minutes.php", ZHB)]),
    "upper-nazareth": ("Upper Nazareth Township", "Northampton", "https://www.uppernazarethtownship.org/", [
        ("boards___commissions/planning_commission.php", PC),
        ("boards___commissions/zoning_hearing_board.php", ZHB)]),
    "lower-saucon": ("Lower Saucon Township", "Northampton", "https://www.lowersaucontownship.org/", [
        ("government/council/agendas_and_minutes.php", GOV),
        ("government/planning_commission/index.php", PC),
        ("government/zoning_hearing_board.php", ZHB)]),
    "east-allen": ("East Allen Township", "Northampton", "https://www.eatwp.org/", [
        ("government/township_meetings/public_notices.php", NOTICE),
        ("government/township_meetings/agenda___minutes.php", GOV)]),
}
SKIP = r"application|comprehensive|nazplan|\bmap\b|schedule|fee|newsletter|all agendas|deadline|checklist|form\b|guide|brochure|ordinance book"
MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _mk(y, m, d):
    y = y if y > 99 else 2000 + y
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(*texts):
    for t in texts:
        if not t:
            continue
        t = unquote(t)
        for rx, f in (
            (r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)", lambda m: _mk(int(m[3]), MON[m[1][:3].lower()], int(m[2]))),
            (r"(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(20\d\d)", lambda m: _mk(int(m[3]), MON[m[2][:3].lower()], int(m[1]))),
            (r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d\d|\d\d)(?!\d)", lambda m: _mk(int(m[3]), int(m[1]), int(m[2]))),
            (r"(?<!\d)(\d{1,2})-(\d{1,2})-(20\d\d|\d\d)(?!\d)", lambda m: _mk(int(m[1]), int(m[2]), int(m[3])) if int(m[1]) > 12 else _mk(int(m[3]), int(m[1]), int(m[2]))),
            (r"(?<!\d)(\d{1,2})(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(\d{2}|20\d\d)(?!\d)", lambda m: _mk(int(m[3]), MON[m[2].lower()], int(m[1]))),
            (r"(?<!\d)(0[1-9]|1[0-2])([0-3]\d)(\d\d)(?!\d)", lambda m: _mk(int(m[3]), int(m[1]), int(m[2]))),  # 102826
        ):
            m = re.search(rx, t, re.I)
            if m:
                d = f(m)
                if d and 2015 <= d.year <= date.today().year + 1:
                    return d
    return None


def kind_of(text, fname, context):
    for blob in (text, fname):
        b = (blob or "").lower()
        if re.search(r"minutes|\bpcm\b", b): return "minutes"
        if re.search(r"packet", b): return "packet"
        if re.search(r"notice|decision|appeal", b): return "notice"
        if re.search(r"agenda", b): return "agenda"
    c = (context or "").lower()
    if "minutes" in c and "agenda" not in c: return "minutes"
    if "agenda" in c and "minutes" not in c: return "agenda"
    return "meeting document"  # can't tell agenda from minutes; don't guess


def page_links(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip())
        if not re.search(r"\.pdf(\?|$)", url, re.I):
            continue
        par = a.find_parent(["tr", "li", "p"])
        out.append({"url": url, "text": " ".join(a.get_text(" ", strip=True).split()),
                    "context": " ".join(par.get_text(" ", strip=True).split())[:160] if par else ""})
    return out


def collect(key):
    name, county, base, pages = SITES[key]
    out_dir = os.path.join(DATA_DIR, key); text_dir = os.path.join(out_dir, "text")
    os.makedirs(text_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "documents.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    seen_hash = {v["sha256"] for v in index.values()}
    seen_url = {v["url"].split("?")[0] for v in index.values() if not v.get("rolling")}

    sess = PoliteSession(base)
    raw = []
    for path, board in pages:
        url = urljoin(base, path)
        try:
            r = sess.get(url)
            raw.append((board, url, page_links(r.text, r.url)))
        except Exception as e:
            print(f"  [{key}] page skipped {url}: {e}")
    # links that appear on every page are site-wide menu links, not meeting documents
    common = Counter(l["url"] for _, _, ls in raw for l in {x["url"]: x for x in ls}.values())
    menu = {u for u, c in common.items() if len(raw) > 1 and c == len(raw)}

    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS))
    docs = {}
    for board, page_url, links in raw:
        for l in links:
            if l["url"] in menu:
                continue
            fname = unquote(urlparse(l["url"]).path.rsplit("/", 1)[-1])
            if re.search(SKIP, f"{l['text']} {fname}", re.I):
                continue
            meeting = parse_date(l["text"], l["context"], fname)
            current = re.search(r"current agenda|plan-agenda|^agenda\.pdf$", f"{l['text']} {fname}", re.I) is not None
            notice_page = board == NOTICE
            if not meeting and not current and not notice_page:
                continue
            if meeting and meeting < cutoff:
                continue
            rolling = (not notice_page) and parse_date(fname) is None and (current or not re.search(r"\d", fname))
            docs.setdefault(l["url"].split("?")[0], dict(
                url=l["url"], text=l["text"] or fname, context=l["context"], board=board,
                kind="notice" if notice_page else kind_of(l["text"], fname, l["context"]), meeting_date=meeting.isoformat() if meeting else None,
                rolling=rolling, source_page=page_url))

    todo = [d for k, d in docs.items() if d["rolling"] or k not in seen_url]
    print(f"[{key}] {len(docs)} meeting documents in range, checking {len(todo)}")
    added = 0
    for d in todo:
        try:
            content = sess.get(d["url"]).content
            if not content.startswith(b"%PDF"):
                raise ValueError("not a PDF")
        except Exception as e:
            print(f"  [{key}] ! {d['text'][:40]}: {e}"); continue
        sha = hashlib.sha256(content).hexdigest()
        if sha in seen_hash:
            continue
        text, n = pdf_text(content)
        needs_ocr = len(text.strip()) < 50 * n
        if needs_ocr:
            text = ocr_text(content)
        meeting = d["meeting_date"] or ((parse_date(text[:3000]) or None) and parse_date(text[:3000]).isoformat())
        slug = re.sub(r"[^a-z]+", "-", d["board"].lower()).strip("-")
        doc_id = f"{d['kind']}_{slug}_{meeting or 'undated'}_{sha[:10]}"
        index[doc_id] = dict(d, municipality=name, county=county, meeting_date=meeting, sha256=sha, pages=n,
                             chars=len(text.strip()), ocr=needs_ocr,
                             collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        open(os.path.join(text_dir, doc_id + ".txt"), "w").write(text)
        seen_hash.add(sha); added += 1
        print(f"  + {meeting or 'undated'} {d['board']} {d['kind']} ({n}p{', OCR' if needs_ocr else ''})")
        json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    return added


if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor
    only = sys.argv[1:] or list(SITES)
    def run(k):
        try:
            return k, collect(k)
        except Exception as e:
            print(f"[{k}] SKIPPED: {e}"); return k, None
    with ThreadPoolExecutor(max_workers=5) as ex:
        print("SUMMARY", dict(ex.map(run, only)))
