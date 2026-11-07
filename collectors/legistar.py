"""
ZoningTrack collector for cities on Granicus Legistar (public Web API: webapi.legistar.com).

For each configured city it lists recent meetings of the development-related boards, downloads the
agenda and minutes PDFs, extracts their text (OCR if scanned), and also stores Legistar's own list of
agenda item titles, which usually names the address and the request. Agendas are re-checked whenever
Legistar says they were re-published; files are de-duplicated by content hash.
Robots-aware and paced (shared PoliteSession).
Output: data/<key>/documents.json and data/<key>/text/<doc_id>.txt
"""
import hashlib, json, os, re, sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

sys.path.insert(0, os.path.dirname(__file__))
from civicplus import PoliteSession, pdf_text, ocr_text

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "190"))
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

CITIES = {
    "allentown": dict(name="City of Allentown", county="Lehigh", client="allentownpa"),
}
# boards whose meetings carry zoning, land development, redevelopment or property decisions
BOARDS = re.compile(r"^(City Council|Planning Commission|Zoning Hearing Board|Historical Architectural Review|"
                    r"Community and Economic Development|Redevelopment Authority|ANIZDA|ACIDA|"
                    r"Vacant Property|Blighted Property|Building Code Board of Appeals)", re.I)


def collect(key):
    c = CITIES[key]
    out_dir = os.path.join(DATA_DIR, key); text_dir = os.path.join(out_dir, "text")
    os.makedirs(text_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "documents.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    seen_hash = {v["sha256"] for v in index.values()}
    # what we last stored for each event + kind: (published timestamp)
    stored = {(v["event_id"], v["kind"], v.get("published_utc")) for v in index.values()}
    by_hash = {v["sha256"]: k for k, v in index.items()}

    api = f"https://webapi.legistar.com/v1/{c['client']}"
    api_sess = PoliteSession("https://webapi.legistar.com")
    file_sess = {}

    since = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    q = {"$filter": f"EventDate ge datetime'{since}'", "$orderby": "EventDate desc", "$top": "1000"}
    events = api_sess.get(f"{api}/events?{urlencode(q)}").json()
    events = [e for e in events if BOARDS.search((e["EventBodyName"] or "").strip())]
    print(f"[{key}] {len(events)} development-board meetings since {since} (crawl delay {api_sess.delay}s)")

    added = 0
    for e in events:
        board = e["EventBodyName"].strip()
        meeting = e["EventDate"][:10]
        items = None
        for kind, url, pub in (("agenda", e["EventAgendaFile"], e["EventAgendaLastPublishedUTC"]),
                               ("minutes", e["EventMinutesFile"], e["EventMinutesLastPublishedUTC"])):
            if not url:
                continue
            if (e["EventId"], kind, pub) in stored:
                continue  # unchanged since last run
            host = "{0.scheme}://{0.netloc}".format(urlparse(url))
            try:
                sess = file_sess.setdefault(host, PoliteSession(host))
                content = sess.get(url).content
                if not content.startswith(b"%PDF"):
                    raise ValueError("not a PDF")
            except Exception as ex:
                print(f"  ! {meeting} {board} {kind}: {ex}"); continue
            sha = hashlib.sha256(content).hexdigest()
            if sha in seen_hash:  # re-published but identical: remember the new timestamp
                if sha in by_hash: index[by_hash[sha]]["published_utc"] = pub
                continue
            if items is None:  # agenda item titles (addresses, applicants, requests)
                try:
                    raw = api_sess.get(f"{api}/events/{e['EventId']}/eventitems?AgendaNote=1&MinutesNote=1").json()
                    items = [dict(seq=i.get("EventItemAgendaSequence"), number=i.get("EventItemAgendaNumber"),
                                  title=(i.get("EventItemTitle") or "").strip(), action=i.get("EventItemActionName"),
                                  result=i.get("EventItemPassedFlagName"), matter=i.get("EventItemMatterFile"))
                             for i in raw if (i.get("EventItemTitle") or "").strip()]
                except Exception as ex:
                    print(f"  ! items {e['EventId']}: {ex}"); items = []
            text, n = pdf_text(content)
            needs_ocr = len(text.strip()) < 50 * n
            if needs_ocr:
                text = ocr_text(content)
            slug = re.sub(r"[^a-z]+", "-", board.lower()).strip("-")
            doc_id = f"{kind}_{slug}_{meeting}_{sha[:10]}"
            index[doc_id] = dict(municipality=c["name"], county=c["county"], board=board, kind=kind,
                                 meeting_date=meeting, meeting_time=e.get("EventTime"), event_id=e["EventId"],
                                 url=url, source_page=e.get("EventInSiteURL"), published_utc=pub,
                                 status=e.get(f"Event{kind.capitalize()}StatusName"), agenda_items=items,
                                 sha256=sha, pages=n, chars=len(text.strip()), ocr=needs_ocr,
                                 collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            open(os.path.join(text_dir, doc_id + ".txt"), "w").write(text)
            seen_hash.add(sha); by_hash[sha] = doc_id; added += 1
            print(f"  + {meeting} {board} {kind} ({n}p{', OCR' if needs_ocr else ''}, {len(items)} items)")
            json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    return added


if __name__ == "__main__":
    for k in sys.argv[1:] or list(CITIES):
        try:
            print("SUMMARY", k, collect(k))
        except Exception as ex:  # one city being down shouldn't stop the others
            print(f"[{k}] SKIPPED: {ex}")
