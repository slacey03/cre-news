"""MOCKUP ONLY: geocode pipeline addresses with the free US Census batch geocoder and write the mockup's data files.
Data files stay on the runner (never committed); only counts are printed and saved."""
import csv, io, json, os, re, sys, collections
import requests
sys.path.insert(0, "tools")
from db import connect
import addr

OUT = "mockup-map/build"
os.makedirs(OUT, exist_ok=True)
conn = connect()
cur = conn.execute("select * from pipeline")
cols = [d.name for d in cur.description]
rows = [dict(zip(cols, r)) for r in cur.fetchall()]
cur = conn.execute("select * from pipeline_teaser"); tcols = [d.name for d in cur.description]
teaser = [dict(zip(tcols, r)) for r in cur.fetchall()]
cur = conn.execute("select * from market_pulse order by month"); mcols = [d.name for d in cur.description]
pulse = [dict(zip(mcols, r)) for r in cur.fetchall()]
conn.close()
print("pipeline rows:", len(rows))

LV = {"077", "095"}  # Lehigh, Northampton county FIPS
cand = []
for r in rows:
    a = (r.get("address") or "").strip()
    parsed = addr.parse(a)
    if not parsed:
        continue
    street = a.split(",")[0].strip()
    town = (addr.town_names(r.get("municipality")) or [""])[0].title()
    cand.append((r["id"], street, town, parsed))
print("rows with a house-number address:", len(cand))

# Mailing cities used by addresses in each municipality (townships rarely are postal cities)
POSTAL = {
 "allentown": ["Allentown"], "bethlehem": ["Bethlehem"], "easton": ["Easton"],
 "south whitehall": ["Allentown", "Orefield"], "whitehall": ["Whitehall", "Allentown", "Coplay"],
 "north whitehall": ["Schnecksville", "Orefield", "Coplay", "Slatington"],
 "lower macungie": ["Macungie", "Emmaus", "Allentown", "Alburtis", "Wescosville"],
 "upper macungie": ["Breinigsville", "Allentown", "Fogelsville", "Orefield", "Trexlertown"],
 "upper milford": ["Emmaus", "Zionsville", "Macungie", "Coopersburg", "Center Valley"],
 "lower milford": ["Coopersburg", "Zionsville", "Emmaus"], "upper saucon": ["Center Valley", "Coopersburg", "Bethlehem"],
 "lower saucon": ["Hellertown", "Bethlehem", "Riegelsville"], "salisbury": ["Allentown", "Emmaus"],
 "hanover": ["Allentown", "Bethlehem"], "bethlehem": ["Bethlehem", "Easton"],
 "lower nazareth": ["Nazareth", "Bethlehem", "Easton"], "upper nazareth": ["Nazareth"], "nazareth": ["Nazareth"],
 "palmer": ["Easton"], "forks": ["Easton"], "wilson": ["Easton"], "williams": ["Easton", "Riegelsville"],
 "plainfield": ["Pen Argyl", "Wind Gap", "Bangor", "Nazareth"], "bushkill": ["Nazareth", "Bath"],
 "moore": ["Bath", "Danielsville", "Nazareth", "Walnutport"], "east allen": ["Bath", "Northampton", "Bethlehem"],
 "allen": ["Northampton", "Catasauqua", "Whitehall"], "lehigh": ["Walnutport", "Northampton", "Slatington", "Cherryville"],
 "washington": ["Slatington", "Bangor"], "heidelberg": ["Germansville", "Slatington", "New Tripoli"],
 "lynn": ["New Tripoli", "Kempton"], "weisenberg": ["New Tripoli", "Fogelsville", "Kutztown"], "lowhill": ["New Tripoli", "Orefield"],
 "fountain hill": ["Bethlehem"], "freemansburg": ["Bethlehem"], "upper mount bethel": ["Mount Bethel", "Bangor", "Portland"],
 "lower mount bethel": ["Martins Creek", "Bangor", "Easton"], "west easton": ["Easton"], "glendon": ["Easton"],
 "chapman": ["Bath"], "north catasauqua": ["Catasauqua"], "east bangor": ["Bangor"], "roseto": ["Bangor"],
}
coords, why, last = {}, collections.Counter(), {}
todo = list(range(len(cand)))
for k in range(6):  # pass 0: municipality name; passes 1+: its mailing cities in turn
    batch = []
    for i in todo:
        rid, street, town, parsed = cand[i]
        cities = [town] + [c for c in POSTAL.get(town.lower(), []) if c.lower() != town.lower()]
        if k < len(cities):
            batch.append((i, cities[k]))
    if not batch:
        break
    buf = io.StringIO(); w = csv.writer(buf)
    for i, city in batch:
        w.writerow([i, cand[i][1], city, "PA", ""])
    res = requests.post("https://geocoding.geo.census.gov/geocoder/geographies/addressbatch",
                        files={"addressFile": ("a.csv", buf.getvalue(), "text/csv")},
                        data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"}, timeout=600)
    res.raise_for_status()
    for line in csv.reader(io.StringIO(res.text)):
        if not line or not line[0].isdigit():
            continue
        i = int(line[0]); rid, street, town, parsed = cand[i]
        if len(line) < 6 or line[2] != "Match":
            last[i] = "no match" if len(line) < 3 or line[2] == "No_Match" else "ambiguous (tie)"; continue
        matched = addr.parse(line[4].split(",")[0])
        county = line[9] if len(line) > 9 else ""
        if not any(addr.same(p, m) for p in parsed for m in matched):
            last[i] = "matched a different address (rejected)"; continue
        if county not in LV:
            last[i] = "outside Lehigh/Northampton (rejected)"; continue
        lon, lat = map(float, line[5].split(","))
        coords[rid] = (lat, lon); last[i] = "pinned"
    todo = [i for i in todo if last.get(i) != "pinned"]
    print(f"pass {k}: tried {len(batch)}, pinned so far {len(coords)}")
why = collections.Counter(last.values())
print("geocode results:", dict(why))
print("still unmatched by municipality:", dict(collections.Counter(cand[i][2] for i in todo).most_common(12)))

MINOR = {"residential minor", "property maintenance", "zoning text or map amendment"}
notable = [r for r in rows if r.get("category") not in MINOR]
stats = (f"projects: {len(rows)}\nwith house-number address: {len(cand)}\ngeocoder: {dict(why)}\n"
         f"pinned: {len(coords)} of {len(rows)} ({100*len(coords)/len(rows):.0f}%)\n"
         f"notable (map default view) pinned: {sum(r['id'] in coords for r in notable)} of {len(notable)} "
         f"({100*sum(r['id'] in coords for r in notable)/len(notable):.0f}%)\n")
print(stats)
open("mockup-map/out/stats.txt", "w").write(stats)
for r in rows + teaser:
    if r["id"] in coords:
        r["lat"], r["lon"] = coords[r["id"]]
json.dump(rows, open(f"{OUT}/pipeline.json", "w"), default=str)
json.dump(teaser, open(f"{OUT}/teaser.json", "w"), default=str)
json.dump(pulse, open(f"{OUT}/pulse.json", "w"), default=str)
