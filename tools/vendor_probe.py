"""One-off probe: what do the vendor agenda platforms return? Saves raw samples to probe/out/."""
import json, os, requests
UA = "ZoningTrack/0.1 (+https://zoningtrack.com; public-records collector)"
OUT = os.path.join(os.path.dirname(__file__), "..", "probe", "out"); os.makedirs(OUT, exist_ok=True)
s = requests.Session(); s.headers["User-Agent"] = UA
def save(name, method, url, **kw):
    try:
        r = s.request(method, url, timeout=60, **kw)
        body = r.text
        print(f"{name}: {r.status_code} {r.headers.get('content-type')} {len(body)}b")
        open(os.path.join(OUT, name), "w").write(f"# {method} {url}\n# {r.status_code} {r.headers.get('content-type')}\n" + body[:60000])
    except Exception as e:
        print(f"{name}: ERROR {e}"); open(os.path.join(OUT, name), "w").write(f"# {url}\nERROR {e}")
for host in ["allentownpa.legistar.com", "webapi.legistar.com", "eatwp.diligent.community", "hellertownborough.diligent.community", "go.boarddocs.com"]:
    save(f"robots_{host}.txt", "GET", f"https://{host}/robots.txt")
L = "https://webapi.legistar.com/v1/allentownpa"
save("legistar_bodies.json", "GET", f"{L}/bodies")
save("legistar_events.json", "GET", f"{L}/events", params={"$filter": "EventDate ge datetime'2026-06-01'", "$orderby": "EventDate desc", "$top": "40"})
save("legistar_calendar.html", "GET", "https://allentownpa.legistar.com/Calendar.aspx")
for t in ["eatwp", "hellertownborough"]:
    D = f"https://{t}.diligent.community"
    save(f"diligent_{t}_types.html", "GET", f"{D}/Portal/MeetingTypeList.aspx")
    save(f"diligent_{t}_meetings.json", "GET", f"{D}/Services/MeetingsService.svc/meetings", params={"from": "2026-05-01", "to": "2026-12-31", "loadall": "false"}, headers={"Accept": "application/json"})
B = "https://go.boarddocs.com/pa/lowersaucontwp/Board.nsf"
save("boarddocs_public.html", "GET", f"{B}/Public")
save("boarddocs_meetings.json", "POST", f"{B}/BD-GetMeetingsList?open", data={"current_committee_id": ""})
