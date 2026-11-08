"""
Link project mentions (one row per document in `projects`) into one record per real project (`pipeline`).

Within each municipality, two mentions are the same project when they share
  - a file / case / plan number (normalized), or
  - a tax parcel number, or
  - a street address (house number + street name), or
  - the same distinctive project name (after removing words like 'preliminary', 'plan', 'land development').
Each pipeline record carries the best-known details, a cleaned category, the current stage, and a timeline.
Rebuilt from scratch on every run (deterministic ids), so it is always consistent with `projects`.
"""
import hashlib, json, os, re, sys
from collections import Counter, defaultdict

STREET_SUFFIX = r"(street|st|road|rd|avenue|ave|av|boulevard|blvd|drive|dr|lane|ln|pike|highway|hwy|court|ct|circle|cir|place|pl|way|terrace|ter|parkway|pkwy|trail|tr|alley|aly)"
DIRECTIONS = {"n": "n", "north": "n", "s": "s", "south": "s", "e": "e", "east": "e", "w": "w", "west": "w"}
NAME_NOISE = set("""the a an of and for at on in to plan plans preliminary final prelim sketch amended revised land development
    subdivision lot line adjustment minor major waiver waivers request requests review variance variances special exception
    conditional use appeal hearing application app update status rfp project proposed proposal approval approve
    extension time sidewalk curb postponement llc inc co corp lp company""".split())
GENERIC_NAMES = {"zoning ordinance amendment", "ordinance amendment", "zoning amendment", "saldo amendment", "comprehensive plan",
                 "zoning text amendment", "text amendment", "ordinance", "budget"}
CATEGORY_FIX = {"zoning text amendment": "zoning text or map amendment"}
CATEGORY_RULES = [  # (regex over name/use/request, category) applied when the model said 'other' or nothing
    (r"certificate of appropriateness|\bhbdo\b|historic", "historic review"),
    (r"\brfp\b|redevelopment authority|preferred developer|authority[- ]owned|blighted|vacant property", "redevelopment property"),
    (r"data center|warehouse|distribution|logistics|self[- ]storage|flex|manufactur|industrial|truck terminal", "industrial/warehouse"),
    (r"apartment|townhouse|townhome|multi-?family|dwelling units|senior living|\bunits\b", "multifamily"),
    (r"restaurant|coffee|brew|pizza|cafe|café|diner|wawa|sheetz|drive-?thru", "restaurant"),
    (r"medical|hospital|clinic|dental|st\.? luke|lvhn|urgent care|physician", "medical"),
    (r"hotel|motel|banquet|event venue|wedding", "hotel/hospitality"),
    (r"retail|store|shop|plaza|shopping center|auto sales|car wash|gas station|bank|dollar|tire|convenience", "retail"),
    (r"\boffice\b", "office"),
    (r"church|school|library|fire (company|station)|firehouse|municipal building|zoo|public park|park (and|&) rec|playground|cemetery", "institutional"),
    (r"farm|agricultur|preservation easement|stable|riding", "agricultural"),
    (r"sewer|sewage|water main|stormwater|pump station|utility|ugi|ppl|cell tower|telecommunication|solar", "infrastructure/utility"),
    (r"\blots?\b|subdivision|single[- ]family", "residential subdivision"),
    (r"rezon|zoning map|zoning (text|ordinance)|ordinance (no\.?|amend)|amendment to the zoning", "zoning text or map amendment"),
]
STAGE_RANK = {"sketch/concept": 1, "ordinance/rezoning": 1, "preliminary plan": 2, "hearing scheduled": 2, "hearing continued": 3,
              "final plan": 4, "extension/time request": 5, "plan approved": 6, "decision granted": 6, "decision denied": 6,
              "withdrawn": 6, "recorded": 7}


def norm_plan(s):
    s = re.sub(r"[^A-Z0-9]", "", (s or "").upper())
    return s if len(s) >= 3 and not s.isdigit() or len(s) >= 5 else None


def plan_keys(s):
    parts = re.split(r"\s*(?:/|;|,| and | & )\s*", s or "")
    return {k for k in (norm_plan(p) for p in parts) if k}


def parcel_keys(s):
    parts = re.split(r"\s*(?:,|;|&| and )\s*", s or "")
    out = set()
    for p in parts:
        k = re.sub(r"[^A-Z0-9]", "", p.upper())
        if len(k) >= 6 and re.search(r"\d", k):
            out.add(k)
    return out


def addr_keys(address, name=None):
    """House number + street name, ignoring street type and direction: '1035 W Turner Street' / '1035 Turner St' ->
    '1035 turner'; 'Route 378' -> '3507 route 378'. The address field may omit the street type; inside a project
    name we require one, so 'OS-104 Conservation Easement' is not mistaken for an address."""
    out = set()
    word = r"(?:(?:north|south|east|west|[nsew])\.?\s+)?(?:(?:st|saint|mt|mount)\.?\s+)?((?:route|rt\.?|sr|state route|pa)\s*\d+[a-z]?|[a-z][a-z0-9'.]*|\d+(?:st|nd|rd|th))"
    for t, need_suffix in ((address, False), (name, True)):
        if not t:
            continue
        rx = r"(?<![-\d])\b(\d{1,6})(?:\s*[-–]\s*\d{1,6})?[a-z]?\s+" + word + (r"(?:\s+[a-z'.]+){0,2}?\s+" + STREET_SUFFIX + r"\b" if need_suffix else "")
        for m in re.finditer(rx, t.lower()):
            street = re.sub(r"[^a-z0-9 ]", "", m.group(2)).replace("rt ", "route ").replace("state route", "route").replace("sr", "route", 1) if re.match(r"(rt|sr|state route|pa)\b", m.group(2)) else re.sub(r"[^a-z0-9]", "", m.group(2))
            street = re.sub(r"^(route|pa)\s*", "route ", street)
            if street in ("the", "a", "and", "of", "to") or len(street) < 2:
                continue
            out.add(f"{m.group(1)} {street}")
    return out


def name_key(name):
    t = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    toks = [w for w in t.split() if w not in NAME_NOISE and not re.fullmatch(r"(phase|ph)?", w)]
    k = " ".join(toks)
    if len(k) < 5 or k in GENERIC_NAMES or not re.search(r"[a-z]{3}", k):
        return None
    return k


REGISTRY = os.path.join(os.path.dirname(__file__), "..", "registry", "lehigh_valley.json")


def muni_resolver():
    """Map a free-text municipality ('Lower Macungie Township', 'Washington Twp., Northampton County') to a registry key."""
    reg = [m for m in json.load(open(REGISTRY)) if m.get("type") not in ("county", "regional")]
    def norm(t):
        t = (t or "").lower()
        t = re.sub(r"\b(township|twp|borough|boro|city of|city|county|co|pa|pennsylvania|of the)\b\.?", " ", t)
        return " ".join(re.sub(r"[^a-z ]", " ", t).split())
    by = defaultdict(list)
    for m in reg:
        by[norm(m["name"])].append(m)
    def resolve(text):
        if not text:
            return None
        t = text.lower()
        head = norm(re.split(r"[,(;]", text)[0])
        cands = by.get(head) or [m for k, ms in by.items() for m in ms if k and re.search(rf"\b{re.escape(k)}\b", norm(text))]
        if not cands:
            return None
        if len(cands) > 1:
            kind = "township" if re.search(r"\b(township|twp)\b", t) else "borough" if re.search(r"\b(borough|boro)\b", t) \
                else "city" if re.search(r"\bcity\b", t) else None
            if kind:
                cands = [m for m in cands if m.get("type") == kind] or cands
            cty = [m for m in cands if (m.get("county") or "").lower() in t]
            cands = cty or cands
            # prefer the longest name match ('lower macungie' over 'macungie')
            cands.sort(key=lambda m: -len(norm(m["name"])))
        return cands[0]
    return resolve, {m["key"]: m for m in reg}


class UF:
    def __init__(s): s.p = {}
    def find(s, x):
        s.p.setdefault(x, x)
        while s.p[x] != x:
            s.p[x] = s.p[s.p[x]]; x = s.p[x]
        return x
    def union(s, a, b): s.p[s.find(a)] = s.find(b)


MAINTENANCE = r"\broof\b|repaint|painting|\bquote\b|\bbids?\b|repairs?\b|lawn|snow removal|boiler|hvac|gutter|board[- ]?up|clean[- ]?out"


def categorize(cats, texts, names=()):
    # upkeep of authority-owned buildings (roof quotes, repaint bids): judged on the name only
    if any(re.search(MAINTENANCE, n or "", re.I) for n in names) and not any(c and c != "other" for c in cats):
        return "property maintenance"
    good = [CATEGORY_FIX.get(c, c) for c in cats if c and c != "other"]
    if good:
        top = Counter(good).most_common()
        # prefer a specific category over 'residential minor' if both appear
        return top[0][0]
    blob = " ".join(t for t in texts if t)
    for rx, cat in CATEGORY_RULES:
        if re.search(rx, blob, re.I):
            return cat
    return "other"


def build(rows):
    """rows: dicts with id, municipality_key, meeting_date, plan_number, name, address, parcel, ... Returns (pipeline, mapping)."""
    uf = UF()
    owners = defaultdict(list)  # key -> row ids
    # a plan number shared by different addresses in one document is a batch approval (e.g. one resolution
    # approving five certificates), not a project id: don't link on it
    doc_plan_addrs = defaultdict(set)
    for r in rows:
        for k in plan_keys(r.get("plan_number")):
            doc_plan_addrs[(r["document_id"], k)].add(frozenset(addr_keys(r.get("address"), r.get("name"))))
    batch_keys = {(d, k) for (d, k), a in doc_plan_addrs.items() if len(a) > 1}
    for r in rows:
        m = r["municipality_key"]
        keys = {("plan", k) for k in plan_keys(r.get("plan_number")) if (r["document_id"], k) not in batch_keys}
        keys |= {("parcel", k) for k in parcel_keys(r.get("parcel"))}
        keys |= {("addr", k) for k in addr_keys(r.get("address"), r.get("name"))}
        nk = name_key(r.get("name"))
        if nk and (r.get("category") or "") not in ("zoning text or map amendment", "zoning text amendment") or (nk and r.get("plan_number")):
            keys.add(("name", nk))
        r["_keys"] = keys
        uf.find(r["id"])
        for k in keys:
            owners[(m,) + k].append(r["id"])
    for ids in owners.values():
        for i in ids[1:]:
            uf.union(ids[0], i)

    groups = defaultdict(list)
    for r in rows:
        groups[uf.find(r["id"])].append(r)

    pipeline, mapping = [], {}
    for g in groups.values():
        g.sort(key=lambda r: (r.get("meeting_date") or "", r["id"]))
        m = g[0]["municipality_key"]
        anchor = sorted(f"{k[0]}:{k[1]}" for r in g for k in r["_keys"]) or [f"row:{g[0]['document_id']}:{g[0]['name']}"]
        pid = hashlib.md5(f"{m}|{anchor[0]}".encode()).hexdigest()[:16]

        def latest(field):
            for r in reversed(g):
                if r.get(field) not in (None, ""):
                    return r[field]
            return None

        def common(field):
            vals = [r[field] for r in g if r.get(field)]
            return Counter(vals).most_common(1)[0][0] if vals else None

        stages = [r.get("stage") for r in g if r.get("stage") and r.get("stage") != "other"]
        meaningful = [r for r in g if r.get("stage") and r.get("stage") != "other"]
        cur_stage = (meaningful[-1]["stage"] if meaningful else (g[-1].get("stage") or "other"))
        timeline = [dict(date=r.get("meeting_date"), board=r.get("board"), kind=r.get("kind"), stage=r.get("stage"),
                         outcome=r.get("outcome"), request=r.get("request"), document_id=r.get("document_id"),
                         url=r.get("url"), source_page=r.get("source_page"))
                    for r in g]
        rec = dict(
            id=pid, municipality_key=m, municipality=g[0].get("municipality"),
            name=common("name"), address=common("address") or latest("address"), parcel=latest("parcel"),
            plan_numbers=sorted({r["plan_number"] for r in g if r.get("plan_number")}),
            owner=latest("owner"), applicant=latest("applicant"), zoning=latest("zoning"), use=latest("use"),
            size=latest("size"), acres=latest("acres"), square_feet=latest("square_feet"),
            category=categorize([r.get("category") for r in g], [r.get("name") for r in g] + [r.get("use") for r in g] + [r.get("request") for r in g],
                                [r.get("name") for r in g]),
            stage=cur_stage, outcome=latest("outcome"), request=latest("request"),
            boards=sorted({r["board"] for r in g if r.get("board")}),
            first_seen=g[0].get("meeting_date"), last_seen=g[-1].get("meeting_date"), mentions=len(g),
            timeline=timeline)
        pipeline.append(rec)
        for r in g:
            mapping[r["id"]] = pid
    return pipeline, mapping


SQL_SETUP = """
create table if not exists pipeline (
  id text primary key, municipality_key text references municipalities(key), municipality text,
  name text, address text, parcel text, plan_numbers text[], owner text, applicant text, zoning text, use text, size text,
  acres numeric, square_feet numeric, category text, stage text, outcome text, request text, boards text[],
  first_seen date, last_seen date, mentions int, timeline jsonb, updated_at timestamptz default now());
alter table pipeline enable row level security;
alter table projects add column if not exists pipeline_id text;
alter table pipeline add column if not exists news jsonb;
alter table pipeline add column if not exists news_first date;
create index if not exists pipeline_muni on pipeline (municipality_key, last_seen desc);
create index if not exists pipeline_cat on pipeline (category, last_seen desc);
"""


def attach_news(conn, rows, pipeline, mapping):
    """Attach news linked by exact address (tools/news_link.py) to each project. Strict: the article's matched address
    must be the same house number + street as one of the project's own addresses, in the same municipality."""
    import addr as A
    if not conn.execute("select to_regclass('public.project_news')").fetchone()[0]:
        return
    news = conn.execute("""select municipality_key, matched_address, url, outlet, headline, published::text, summary, facts
                           from project_news order by published nulls last""").fetchall()
    by_muni = defaultdict(list)
    for n in news:
        parsed = A.parse(n[1].split(",")[0], need_suffix=False)
        if parsed:
            by_muni[n[0]].append((parsed[0], n))
    own = defaultdict(set)  # pipeline id -> its addresses
    for r in rows:
        for a in A.parse(r.get("address") or ""):
            own[mapping[r["id"]]].add(a)
    linked = 0
    for p in pipeline:
        items, seen = [], set()
        for a in own.get(p["id"], ()):
            for na, n in by_muni.get(p["municipality_key"], ()):
                if A.same(a, na) and n[2] not in seen:
                    seen.add(n[2])
                    items.append(dict(matched_address=n[1], url=n[2], outlet=n[3], headline=n[4], date=n[5],
                                      summary=n[6], facts=n[7] or {}))
        items.sort(key=lambda x: x["date"] or "9999")
        p["news"] = items
        p["news_first"] = next((x["date"] for x in items if x["date"]), None)
        linked += bool(items)
    print(f"news: {len(news)} linked articles attached to {linked} projects")


def main():
    sys.path.insert(0, os.path.dirname(__file__))
    from db import connect
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    conn = connect()
    conn.execute(SQL_SETUP); conn.execute("alter table projects add column if not exists project_municipality text"); conn.commit()
    rows = conn.cursor(row_factory=dict_row).execute("""
        select p.id, p.document_id, p.municipality_key, p.board, p.meeting_date::text as meeting_date, p.plan_number, p.name,
               p.address, p.parcel, p.owner, p.applicant, p.request, p.zoning, p.size, p.acres::float as acres,
               p.square_feet::float as square_feet, p.use, p.category, p.stage, p.outcome, d.kind, d.municipality,
               d.url, d.source_page, p.project_municipality
        from projects p join documents d on d.id = p.document_id""").fetchall()
    resolve, reg = muni_resolver()
    regional = {r["municipality_key"] for r in rows if r["municipality_key"] in ("lvpc", "lehigh-county", "northampton-county")}
    for r in rows:  # file projects from regional documents (LVPC, counties) under the town they are in
        if r["municipality_key"] in regional:
            m = resolve(r.get("project_municipality"))
            if m:
                r["municipality_key"], r["municipality"] = m["key"], m["name"]
    pipeline, mapping = build(rows)
    attach_news(conn, rows, pipeline, mapping)
    with conn.transaction():
        for k in {p["municipality_key"] for p in pipeline} - {x[0] for x in conn.execute("select key from municipalities").fetchall()}:
            m = reg.get(k, {"name": k, "county": None})
            conn.execute("insert into municipalities (key, name, county) values (%s,%s,%s) on conflict do nothing",
                         (k, m["name"], m.get("county")))
        conn.execute("delete from pipeline")
        with conn.cursor() as cur:
            cur.executemany("""insert into pipeline (id, municipality_key, municipality, name, address, parcel, plan_numbers, owner, applicant,
                    zoning, use, size, acres, square_feet, category, stage, outcome, request, boards, first_seen, last_seen, mentions, timeline, news, news_first)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(p["id"], p["municipality_key"], p["municipality"], p["name"], p["address"], p["parcel"], p["plan_numbers"],
                  p["owner"], p["applicant"], p["zoning"], p["use"], p["size"], p["acres"], p["square_feet"], p["category"],
                  p["stage"], p["outcome"], p["request"], p["boards"], p["first_seen"], p["last_seen"], p["mentions"],
                  Jsonb(p["timeline"]), Jsonb(p.get("news") or []), p.get("news_first")) for p in pipeline])
            cur.executemany("update projects set pipeline_id = %s where id = %s", [(v, k) for k, v in mapping.items()])
    conn.execute(open(os.path.join(os.path.dirname(__file__), "..", "db", "access.sql")).read()); conn.commit()
    print("access rules applied (public teaser + members-only pipeline)")
    report(rows, pipeline)


def report(rows, pipeline):
    print(f"{len(rows)} mentions -> {len(pipeline)} projects")
    print("categories:", Counter(p["category"] for p in pipeline).most_common())
    print("stages:", Counter(p["stage"] for p in pipeline).most_common())
    multi = sorted((p for p in pipeline if p["mentions"] > 1), key=lambda p: -p["mentions"])
    print(f"{len(multi)} projects appear in more than one document")
    for p in multi[:15]:
        print(f"  {p['mentions']:>2}x {p['municipality_key']}: {p['name']} | {p['address']} | {p['category']} | {p['stage']}")


if __name__ == "__main__":
    if len(sys.argv) > 1:  # local test: python link_projects.py export.jsonl
        rows = [json.loads(l) for l in open(sys.argv[1])]
        pipeline, _ = build(rows)
        report(rows, pipeline)
        if len(sys.argv) > 2:
            json.dump(pipeline, open(sys.argv[2], "w"), default=str, indent=0)
    else:
        main()
