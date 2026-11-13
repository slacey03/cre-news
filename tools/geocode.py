"""
Map pins: geocode project street addresses with the free US Census geocoder and put lat/lon on the pipeline table.

Strict, same as news linking: a pin is only stored when the geocoder's matched address has the same house number and
street name as ours, and lies in the project's county. No street-only, town-only or nearby guesses.

Townships are rarely mailing cities ("Lower Saucon" mail goes to Hellertown), so an address that doesn't match with
the municipality name is retried with that municipality's mailing cities. Every lookup is cached in geocode_cache
(private table), so an address is only sent once; misses are retried after 30 days.

Runs after link_projects.py (which rebuilds the pipeline table each run).
"""
import collections, csv, io, os, re, sys, time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(__file__))
from db import connect
import addr

URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
COUNTY_FIPS = {"lehigh": "077", "northampton": "095"}
DEFAULT_FIPS = set(COUNTY_FIPS.values())
RETRY_AFTER = timedelta(days=30)

# Mailing cities used by addresses in each municipality (tried in order after the municipality's own name)
POSTAL = {
    "allentown": ["Allentown"], "bethlehem": ["Bethlehem", "Easton"], "easton": ["Easton"],
    "south whitehall": ["Allentown", "Orefield"], "whitehall": ["Whitehall", "Allentown", "Coplay"],
    "north whitehall": ["Schnecksville", "Orefield", "Coplay", "Slatington"],
    "lower macungie": ["Macungie", "Emmaus", "Allentown", "Alburtis", "Wescosville"],
    "upper macungie": ["Breinigsville", "Allentown", "Fogelsville", "Orefield", "Trexlertown"],
    "upper milford": ["Emmaus", "Zionsville", "Macungie", "Coopersburg", "Center Valley"],
    "lower milford": ["Coopersburg", "Zionsville", "Emmaus"], "upper saucon": ["Center Valley", "Coopersburg", "Bethlehem"],
    "lower saucon": ["Hellertown", "Bethlehem", "Riegelsville"], "salisbury": ["Allentown", "Emmaus"],
    "hanover": ["Allentown", "Bethlehem"], "lower nazareth": ["Nazareth", "Bethlehem", "Easton"],
    "upper nazareth": ["Nazareth"], "nazareth": ["Nazareth"], "palmer": ["Easton"], "forks": ["Easton"],
    "wilson": ["Easton"], "williams": ["Easton", "Riegelsville"], "plainfield": ["Pen Argyl", "Wind Gap", "Bangor", "Nazareth"],
    "bushkill": ["Nazareth", "Bath"], "moore": ["Bath", "Danielsville", "Nazareth", "Walnutport"],
    "east allen": ["Bath", "Northampton", "Bethlehem"], "allen": ["Northampton", "Catasauqua", "Whitehall"],
    "lehigh": ["Walnutport", "Northampton", "Slatington", "Cherryville"], "washington": ["Slatington", "Bangor"],
    "heidelberg": ["Germansville", "Slatington", "New Tripoli"], "lynn": ["New Tripoli", "Kempton"],
    "weisenberg": ["New Tripoli", "Fogelsville", "Kutztown"], "lowhill": ["New Tripoli", "Orefield"],
    "fountain hill": ["Bethlehem"], "freemansburg": ["Bethlehem"], "upper mount bethel": ["Mount Bethel", "Bangor", "Portland"],
    "lower mount bethel": ["Martins Creek", "Bangor", "Easton"], "west easton": ["Easton"], "glendon": ["Easton"],
    "chapman": ["Bath"], "north catasauqua": ["Catasauqua"], "east bangor": ["Bangor"], "roseto": ["Bangor"],
}

SETUP = """
create table if not exists geocode_cache (
  addr_key text primary key, street text, town text, status text, lat double precision, lon double precision,
  matched text, county_fips text, checked_at timestamptz default now());
alter table geocode_cache enable row level security;
alter table pipeline add column if not exists lat double precision;
alter table pipeline add column if not exists lon double precision;
"""


def key_of(street, town):
    return re.sub(r"\s+", " ", f"{street}|{town}".lower()).strip()


def census(rows):
    """rows: [(i, street, city)] -> {i: csv line}"""
    buf = io.StringIO(); w = csv.writer(buf)
    for i, street, city in rows:
        w.writerow([i, street, city, "PA", ""])
    for attempt in range(3):
        try:
            r = requests.post(URL, files={"addressFile": ("a.csv", buf.getvalue(), "text/csv")},
                              data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"}, timeout=600)
            r.raise_for_status()
            return {int(l[0]): l for l in csv.reader(io.StringIO(r.text)) if l and l[0].isdigit()}
        except Exception as e:
            print(f"  geocoder attempt {attempt + 1} failed: {e}")
            time.sleep(20)
    return None


def main():
    conn = connect()
    conn.execute(SETUP); conn.commit()
    county_of = {k: (c or "").lower() for k, c in conn.execute("select key, county from municipalities").fetchall()}
    rows = conn.execute("select id, address, municipality, municipality_key from pipeline where address is not null").fetchall()
    cache = {r[0]: r for r in conn.execute("select addr_key, status, lat, lon, checked_at from geocode_cache").fetchall()}
    now = datetime.now(timezone.utc)

    # one lookup per distinct (street, town); projects are matched back by key
    want, project_keys = {}, collections.defaultdict(list)
    for pid, address, muni, mkey in rows:
        parsed = addr.parse(address)
        if not parsed:
            continue
        street = address.split(",")[0].strip()
        town = (addr.town_names(muni) or [""])[0]
        if not town:
            continue
        k = key_of(street, town)
        project_keys[k].append(pid)
        c = cache.get(k)
        if c and (c[1] == "pinned" or now - c[4] < RETRY_AFTER):
            continue
        fips = COUNTY_FIPS.get(county_of.get(mkey, "").replace(" county", "").strip())
        want[k] = dict(street=street, town=town, parsed=parsed, fips={fips} if fips else DEFAULT_FIPS,
                       cities=[town.title()] + [c for c in POSTAL.get(town, []) if c.lower() != town])
    print(f"{len(project_keys)} distinct project addresses; {len(want)} to look up now ({len(project_keys) - len(want)} cached)")

    keys = list(want)
    result = {k: dict(status="no match") for k in keys}
    todo = list(range(len(keys)))
    failed = False
    for k_pass in range(6):
        batch = [(i, want[keys[i]]["street"], want[keys[i]]["cities"][k_pass]) for i in todo if k_pass < len(want[keys[i]]["cities"])]
        if not batch:
            break
        lines = census(batch)
        if lines is None:
            print("geocoder unavailable; misses will be retried next run"); failed = True; break
        for i, line in lines.items():
            w = want[keys[i]]
            if len(line) < 6 or line[2] != "Match":
                result[keys[i]] = dict(status="ambiguous" if len(line) > 2 and line[2] == "Tie" else "no match"); continue
            matched = addr.parse(line[4].split(",")[0])
            county = line[9] if len(line) > 9 else ""
            if not any(addr.same(p, m) for p in w["parsed"] for m in matched):
                result[keys[i]] = dict(status="different address"); continue
            if county not in w["fips"]:
                result[keys[i]] = dict(status="wrong county"); continue
            lon, lat = map(float, line[5].split(","))
            result[keys[i]] = dict(status="pinned", lat=lat, lon=lon, matched=line[4], county=county)
        todo = [i for i in todo if result[keys[i]]["status"] != "pinned"]
        print(f"  pass {k_pass}: tried {len(batch)}, still unmatched {len(todo)}")
    if failed:  # don't remember misses from an incomplete run
        result = {k: r for k, r in result.items() if r["status"] == "pinned"}
    if result:
        with conn.cursor() as cur:
            cur.executemany("""insert into geocode_cache (addr_key, street, town, status, lat, lon, matched, county_fips, checked_at)
                               values (%s,%s,%s,%s,%s,%s,%s,%s,now())
                               on conflict (addr_key) do update set status=excluded.status, lat=excluded.lat, lon=excluded.lon,
                                 matched=excluded.matched, county_fips=excluded.county_fips, checked_at=now()""",
                            [(k, want[k]["street"], want[k]["town"], r["status"], r.get("lat"), r.get("lon"), r.get("matched"), r.get("county"))
                             for k, r in result.items()])
        conn.commit()
        print("new lookups:", dict(collections.Counter(r["status"] for r in result.values())))

    pins = {r[0]: (r[1], r[2]) for r in conn.execute("select addr_key, lat, lon from geocode_cache where status = 'pinned'").fetchall()}
    updates = [(pins[k][0], pins[k][1], pid) for k, pids in project_keys.items() if k in pins for pid in pids]
    with conn.transaction():
        conn.execute("update pipeline set lat = null, lon = null")
        with conn.cursor() as cur:
            cur.executemany("update pipeline set lat = %s, lon = %s where id = %s", updates)
    total = conn.execute("select count(*), count(lat) from pipeline").fetchone()
    print(f"map pins: {total[1]} of {total[0]} projects ({100 * total[1] / max(total[0], 1):.0f}%)")
    conn.close()


if __name__ == "__main__":
    main()
