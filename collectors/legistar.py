"""
ZoningTrack collector for cities on Granicus Legistar (public Web API: webapi.legistar.com).

For each configured city it lists recent meetings of the development-related boards and stores
Legistar's own list of agenda items (from the open API), which names the address, applicant, request
and, after the vote, the outcome. Agenda/minutes PDFs are also collected where the file host's
robots.txt allows (Allentown's legistar1.com does not, so it is items only). Agendas are re-checked whenever
Legistar says they were re-published; files are de-duplicated by content hash.
Robots-aware and paced (shared PoliteSession).
Output: data/<key>/documents.json and data/<key>/text/<doc_id>.txt
"""
import hashlib, json, os, re, sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

sys.path.insert(0, os.path.dirname(__file__))
from civicplus import PoliteSession, pdf_text, ocr_text, FOUND, record_health

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
    stored = {(v["event_id"], v["kind"], v.get("published_utc")) for v in index.values() if v.get("published_utc")}
    by_hash = {v["sha256"]: k for k, v in index.items()}

    api = f"https://webapi.legistar.com/v1/{c['client']}"
    api_sess = PoliteSession("https://webapi.legistar.com")
    file_sess = {}

    since = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    q = {"$filter": f"EventDate ge datetime'{since}'", "$orderby": "EventDate desc", "$top": "1000"}
    events = api_sess.get(f"{api}/events?{urlencode(q)}").json()
    events = [e for e in events if BOARDS.search((e["EventBodyName"] or "").strip())]
    FOUND[key] = len(events)
    print(f"[{key}] {len(events)} development-board meetings since {since} (crawl delay {api_sess.delay}s)")

    added = 0
    blocked = set()  # file hosts whose robots.txt disallows us
    for e in events:
        board = e["EventBodyName"].strip()
        meeting = e["EventDate"][:10]
        slug = re.sub(r"[^a-z]+", "-", board.lower()).strip("-")

        # 1) agenda items from the open API: address, applicant, request and (after the vote) outcome
        doc_id = f"agenda-items_{slug}_{meeting}_{e['EventId']}"
        if index.get(doc_id, {}).get("modified_utc") != e["EventLastModifiedUtc"]:
            try:
                raw = api_sess.get(f"{api}/events/{e['EventId']}/eventitems?AgendaNote=1&MinutesNote=1").json()
            except Exception as ex:
                print(f"  ! items {meeting} {board}: {ex}"); raw = None
            if raw is not None:
                items = [dict(seq=i.get("EventItemAgendaSequence"), number=i.get("EventItemAgendaNumber"),
                              title=(i.get("EventItemTitle") or "").strip(), matter=i.get("EventItemMatterFile"),
                              matter_name=i.get("EventItemMatterName"), matter_type=i.get("EventItemMatterType"),
                              action=i.get("EventItemActionName"), result=i.get("EventItemPassedFlagName"),
                              agenda_note=i.get("EventItemAgendaNote"), minutes_note=i.get("EventItemMinutesNote"))
                         for i in raw if (i.get("EventItemTitle") or "").strip()]
                if items:
                    lines = [f"{c['name']} - {board} - {meeting} {e.get('EventTime') or ''}".strip(), ""]
                    for it in items:
                        lines.append(" ".join(x for x in (it["number"] or "", it["matter"] or "", it["title"]) if x))
                        for extra in ("agenda_note", "minutes_note"):
                            if it[extra]: lines.append("   " + re.sub(r"<[^>]+>", " ", it[extra]).strip())
                        if it["action"] or it["result"]:
                            lines.append(f"   -> {it['action'] or ''} {it['result'] or ''}".rstrip())
                    text = "\n".join(lines)
                    is_new = doc_id not in index
                    index[doc_id] = dict(municipality=c["name"], county=c["county"], board=board, kind="agenda items",
                                         meeting_date=meeting, meeting_time=e.get("EventTime"), event_id=e["EventId"],
                                         url=f"{api}/events/{e['EventId']}/eventitems", source_page=e.get("EventInSiteURL"),
                                         agenda_status=e.get("EventAgendaStatusName"), minutes_status=e.get("EventMinutesStatusName"),
                                         agenda_pdf=e.get("EventAgendaFile"), minutes_pdf=e.get("EventMinutesFile"),
                                         modified_utc=e["EventLastModifiedUtc"], agenda_items=items,
                                         sha256=hashlib.sha256(text.encode()).hexdigest(), chars=len(text),
                                         collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                    open(os.path.join(text_dir, doc_id + ".txt"), "w").write(text)
                    added += 1
                    print(f"  {'+' if is_new else '~'} {meeting} {board}: {len(items)} agenda items")
                    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)

        # 2) the agenda / minutes PDFs, only where the file host's robots.txt allows it
        for kind, url, pub in (("agenda", e["EventAgendaFile"], e["EventAgendaLastPublishedUTC"]),
                               ("minutes", e["EventMinutesFile"], e["EventMinutesLastPublishedUTC"])):
            if not url or (e["EventId"], kind, pub) in stored:
                continue
            host = "{0.scheme}://{0.netloc}".format(urlparse(url))
            if host in blocked:
                continue
            try:
                sess = file_sess.setdefault(host, PoliteSession(host))
                content = sess.get(url).content
                if not content.startswith(b"%PDF"):
                    raise ValueError("not a PDF")
            except PermissionError:
                blocked.add(host); print(f"  [{key}] {host} robots.txt disallows PDFs; using agenda items only"); continue
            except Exception as ex:
                print(f"  ! {meeting} {board} {kind}: {ex}"); continue
            sha = hashlib.sha256(content).hexdigest()
            if sha in seen_hash:  # re-published but identical: remember the new timestamp
                if sha in by_hash: index[by_hash[sha]]["published_utc"] = pub
                continue
            text, n = pdf_text(content)
            needs_ocr = len(text.strip()) < 50 * n
            if needs_ocr:
                text = ocr_text(content)
            pdf_id = f"{kind}_{slug}_{meeting}_{sha[:10]}"
            index[pdf_id] = dict(municipality=c["name"], county=c["county"], board=board, kind=kind,
                                 meeting_date=meeting, meeting_time=e.get("EventTime"), event_id=e["EventId"],
                                 url=url, source_page=e.get("EventInSiteURL"), published_utc=pub,
                                 sha256=sha, pages=n, chars=len(text.strip()), ocr=needs_ocr,
                                 collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            open(os.path.join(text_dir, pdf_id + ".txt"), "w").write(text)
            seen_hash.add(sha); by_hash[sha] = pdf_id; added += 1
            print(f"  + {meeting} {board} {kind} PDF ({n}p{', OCR' if needs_ocr else ''})")
            json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    json.dump(index, open(index_path, "w"), indent=1, sort_keys=True)
    return added


if __name__ == "__main__":
    for k in sys.argv[1:] or list(CITIES):
        try:
            n = collect(k); record_health(k, new=n); print("SUMMARY", k, n)
        except Exception as ex:  # one city being down shouldn't stop the others
            print(f"[{k}] SKIPPED: {ex}"); record_health(k, error=ex)
