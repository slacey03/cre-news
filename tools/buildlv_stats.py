"""
BuildLV market statistics: LVPC's monthly Subdivision + Land Development report (lvpc.org/buildlv) is a one-page
infographic with regional totals. This job finds new monthly reports, has Claude read each page image into fixed
fields, checks the parts add up to the printed totals, and stores one row per month in Supabase `market_stats`.
Robots-aware and paced (shared PoliteSession). Quarterly / mid-year roll-ups are skipped.
"""
import base64, hashlib, io, json, os, re, sys
from datetime import date
from urllib.parse import urljoin, unquote

import anthropic
from bs4 import BeautifulSoup
from pdf2image import convert_from_bytes
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
from civicplus import PoliteSession
from db import connect

PAGE = "https://lvpc.org/buildlv"
MODELS = ["claude-sonnet-5-5", "claude-sonnet-5"]
SINCE = date(2025, 1, 1)
MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
UNIT_TYPES = ["single_family_detached", "townhouses", "apartments", "twins", "assisted_living", "manufactured_homes", "condos"]
SQFT_TYPES = ["commercial", "retail", "public_quasi_public", "industrial", "warehouse", "office", "transportation", "agricultural", "recreational"]
REVIEW_TYPES = ["subdivision_lot_line", "development", "stormwater", "environment", "municipal_ordinances_maps_plans",
                "data_support_requests", "transportation"]

N = {"type": ["number", "null"]}
TOOL = {"name": "record_report", "description": "Record the figures printed on this LVPC BuildLV monthly report.",
        "input_schema": {"type": "object", "required": ["report_month", "units_total", "nonres_sqft_total"], "properties": {
            "report_month": {"type": "string", "description": "Month the report covers, as YYYY-MM"},
            "lehigh_plans": N, "lehigh_acres": N, "northampton_plans": N, "northampton_acres": N,
            "reviews": {"type": "object", "description": "Regional totals by review type", "properties": {k: N for k in REVIEW_TYPES}},
            "units_total": N, "units": {"type": "object", "properties": {k: N for k in UNIT_TYPES}},
            "nonres_sqft_total": N,
            "sqft": {"type": "object", "description": "Square feet by type; warehouse is a subset of industrial",
                     "properties": {k: N for k in SQFT_TYPES}},
            "notes": {"type": ["string", "null"], "description": "Any figure that was unreadable or ambiguous"}}}}
PROMPT = ("This is the Lehigh Valley Planning Commission's monthly BuildLV Subdivision + Land Development report. "
          "Read every printed figure exactly. Plans/reviews and acres by county; regional totals by review type; residential "
          "units total and by type; non-residential square feet total and by type (e.g. '1.46M' = 1460000, '98.3K' = 98300). "
          "Use null for anything not printed. Do not compute or guess. Call record_report once.")
SQL = """
create table if not exists market_stats (
  month date not null, source text not null default 'LVPC BuildLV', region text default 'Lehigh Valley',
  lehigh_plans int, lehigh_acres numeric, northampton_plans int, northampton_acres numeric,
  reviews jsonb, units_total int, units jsonb, nonres_sqft_total bigint, sqft jsonb,
  checks jsonb, notes text, url text, file_sha text, collected_at timestamptz default now(),
  primary key (source, month));
alter table market_stats enable row level security;
drop policy if exists members_read_market on market_stats;
create policy members_read_market on market_stats for select to authenticated using (public.is_member());
"""


def month_from_name(name):
    n = unquote(name).lower()
    if re.search(r"\bq\d|mid ?year|semi|annual|quarter", n):
        return None  # roll-ups, not monthly
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*", n)
    y = re.search(r"20(2\d)", n) or re.search(r"[_ ](2\d)\b", n)
    if not m:
        return None
    return date(2000 + int(y.group(1)) if y else date.today().year, MON[m.group(1)], 1)


def checks(r):
    """Parts should add up to printed totals (warehouse is a subset of industrial)."""
    out = {}
    u = [v for v in (r.get("units") or {}).values() if isinstance(v, (int, float))]
    if r.get("units_total") is not None and u:
        out["units_sum_ok"] = abs(sum(u) - r["units_total"]) <= 1
    s = {k: v for k, v in (r.get("sqft") or {}).items() if isinstance(v, (int, float)) and k != "warehouse"}
    if r.get("nonres_sqft_total") and s:
        out["sqft_sum_pct_off"] = round(abs(sum(s.values()) - r["nonres_sqft_total"]) / r["nonres_sqft_total"] * 100, 1)
        out["sqft_sum_ok"] = out["sqft_sum_pct_off"] <= 3  # infographic rounds (e.g. 98.3K)
    return out


def main():
    conn = connect()
    conn.execute(SQL); conn.commit()
    have = {row[0] for row in conn.execute("select file_sha from market_stats where file_sha is not null").fetchall()}
    site = PoliteSession("https://lvpc.org")
    soup = BeautifulSoup(site.get(PAGE).text, "html.parser")
    links = {}
    for a in soup.find_all("a", href=True):
        u = urljoin(PAGE, a["href"])
        if re.search(r"\.pdf", u, re.I) and "wsimg.com" in u:  # report files live on LVPC's file host
            name = unquote(u.split("/")[-1].split("?")[0])
            mo = month_from_name(name)
            if mo and mo >= SINCE:
                links.setdefault(u.split("?")[0], (u, name, mo))
    print(f"{len(links)} monthly BuildLV reports since {SINCE:%b %Y}")
    cdn = PoliteSession("https://img1.wsimg.com")
    client = anthropic.Anthropic()
    model = None
    tin = tout = done = 0
    for u, name, mo in sorted(links.values(), key=lambda x: x[2]):
        content = cdn.get(u).content
        sha = hashlib.sha256(content).hexdigest()
        if sha in have:
            continue
        img = convert_from_bytes(content, dpi=150)[0]
        buf = io.BytesIO(); img.save(buf, format="PNG")
        msg = None
        for m in ([model] if model else MODELS):
            try:
                msg = client.messages.create(model=m, max_tokens=2000, tools=[TOOL], tool_choice={"type": "auto"},
                                             messages=[{"role": "user", "content": [
                                                 {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                              "data": base64.b64encode(buf.getvalue()).decode()}},
                                                 {"type": "text", "text": PROMPT}]}])
                model = m; break
            except anthropic.NotFoundError:
                continue
        if msg is None:
            print(f"  ! {name}: no model available"); continue
        tin += msg.usage.input_tokens; tout += msg.usage.output_tokens
        r = next((b.input for b in msg.content if b.type == "tool_use"), None)
        if not r:
            print(f"  ! {name}: no figures returned"); continue
        rm = re.match(r"(20\d\d)-(\d\d)", r.get("report_month") or "")
        month = date(int(rm.group(1)), int(rm.group(2)), 1) if rm else mo
        if month != mo:
            print(f"  note: {name} file name says {mo:%b %Y}, report says {month:%b %Y} - using the report")
        ck = checks(r)
        num = lambda k: r.get(k)
        conn.execute("""insert into market_stats (month, lehigh_plans, lehigh_acres, northampton_plans, northampton_acres, reviews,
                          units_total, units, nonres_sqft_total, sqft, checks, notes, url, file_sha)
                        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        on conflict (source, month) do update set lehigh_plans=excluded.lehigh_plans, lehigh_acres=excluded.lehigh_acres,
                          northampton_plans=excluded.northampton_plans, northampton_acres=excluded.northampton_acres,
                          reviews=excluded.reviews, units_total=excluded.units_total, units=excluded.units,
                          nonres_sqft_total=excluded.nonres_sqft_total, sqft=excluded.sqft, checks=excluded.checks,
                          notes=excluded.notes, url=excluded.url, file_sha=excluded.file_sha, collected_at=now()""",
                     (month, num("lehigh_plans"), num("lehigh_acres"), num("northampton_plans"), num("northampton_acres"),
                      Jsonb(r.get("reviews") or {}), num("units_total"), Jsonb(r.get("units") or {}),
                      num("nonres_sqft_total"), Jsonb(r.get("sqft") or {}), Jsonb(ck), r.get("notes"), u, sha))
        conn.commit(); done += 1
        print("ROW " + json.dumps(dict(month=month.isoformat(), file=name, checks=ck, **{k: r.get(k) for k in
              ("lehigh_plans", "lehigh_acres", "northampton_plans", "northampton_acres", "units_total", "units",
               "nonres_sqft_total", "sqft", "reviews", "notes")})))
    print(f"TOTAL {done} reports read, {tin} in + {tout} out tokens = ${tin/1e6*2 + tout/1e6*10:.2f}")


if __name__ == "__main__":
    main()
