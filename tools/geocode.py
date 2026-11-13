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
COUNTY_FIPS = {"lehigh": "077", "northampton": "095", "berks": "011"}
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

# Berks County: mailing cities by municipality key (names like Heidelberg/Washington also exist in the Lehigh Valley)
POSTAL_BY_KEY = {
    "albany-berks": ["Kempton", "New Tripoli"], "alsace-berks": ["Temple", "Reading", "Oley"], "amity-berks": ["Douglassville", "Birdsboro"],
    "bern-berks": ["Reading", "Leesport"], "bethel-berks": ["Bethel"], "brecknock-berks": ["Mohnton", "Denver"],
    "caernarvon-berks": ["Morgantown", "Elverson"], "centre-berks": ["Mohrsville", "Leesport", "Bernville"],
    "colebrookdale-berks": ["Boyertown", "Gilbertsville"], "cumru-berks": ["Reading", "Shillington", "Mohnton"],
    "district-berks": ["Boyertown", "Barto"], "douglass-berks": ["Boyertown", "Gilbertsville"], "earl-berks": ["Boyertown", "Oley"],
    "exeter-berks": ["Reading", "Birdsboro", "Douglassville"], "greenwich-berks": ["Lenhartsville", "Kempton", "Krumsville"],
    "heidelberg-berks": ["Robesonia", "Womelsdorf"], "hereford-berks": ["Hereford", "Bally", "Barto"],
    "jefferson-berks": ["Bethel", "Bernville", "Robesonia"], "longswamp-berks": ["Mertztown", "Topton", "Alburtis"],
    "lower-alsace-berks": ["Reading", "Mount Penn"], "lower-heidelberg-berks": ["Reading", "Wernersville", "Sinking Spring"],
    "maidencreek-berks": ["Blandon", "Fleetwood", "Leesport"], "marion-berks": ["Womelsdorf", "Stouchsburg"],
    "maxatawny-berks": ["Kutztown"], "muhlenberg-berks": ["Reading", "Temple", "Laureldale"],
    "north-heidelberg-berks": ["Robesonia", "Bernville", "Womelsdorf"], "oley-berks": ["Oley"], "ontelaunee-berks": ["Leesport", "Reading"],
    "penn-berks": ["Bernville", "Leesport"], "perry-berks": ["Shoemakersville", "Hamburg", "Mohrsville"], "pike-berks": ["Oley", "Boyertown"],
    "richmond-berks": ["Fleetwood", "Virginville"], "robeson-berks": ["Birdsboro", "Mohnton", "Elverson"], "rockland-berks": ["Fleetwood", "Oley"],
    "ruscombmanor-berks": ["Fleetwood"], "south-heidelberg-berks": ["Wernersville", "Sinking Spring", "Robesonia"],
    "spring-berks": ["Reading", "Sinking Spring", "Wernersville"], "tilden-berks": ["Hamburg"],
    "tulpehocken-berks": ["Myerstown", "Bethel", "Rehrersburg"], "union-berks": ["Douglassville", "Birdsboro"],
    "upper-bern-berks": ["Shartlesville", "Hamburg"], "upper-tulpehocken-berks": ["Strausstown", "Shartlesville"],
    "washington-berks": ["Barto", "Bally", "Boyertown"], "windsor-berks": ["Hamburg"],
    "reading": ["Reading"], "mount-penn-berks": ["Reading"], "kenhorst-berks": ["Reading"], "laureldale-berks": ["Reading"],
    "west-reading-berks": ["Reading"], "wyomissing-berks": ["Wyomissing", "Reading"], "st-lawrence-berks": ["Reading"],
    "lyons-berks": ["Lyon Station"], "new-morgan-berks": ["Morgantown"], "centerport-berks": ["Centerport", "Shoemakersville"],
}

SETUP = """
create table if not exists geocode_cache (
  addr_key text primary key, street text, town text, status text, lat double precision, lon double precision,
  matched text, county_fips text, checked_at timestamptz default now());
alter table geocode_cache enable row level security;
alter table pipeline add column if not exists lat double precision;
alter table pipeline add column if not exists lon double precision;
"""


def key_of(street, mkey):
    return re.sub(r"\s+", " ", f"{street}|{mkey}".lower()).strip()


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
        k = key_of(street, mkey or town)
        project_keys[k].append(pid)
        c = cache.get(k)
        if c and (c[1] == "pinned" or now - c[4] < RETRY_AFTER):
            continue
        fips = COUNTY_FIPS.get(county_of.get(mkey, "").replace(" county", "").strip())
        want[k] = dict(street=street, town=town, parsed=parsed, fips={fips} if fips else DEFAULT_FIPS,
                       cities=[town.title()] + [c for c in POSTAL_BY_KEY.get(mkey) or POSTAL.get(town, []) if c.lower() != town])
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
