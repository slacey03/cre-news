"""
AI extraction: read collected documents from Supabase and write structured project records.
Usage: python tools/extract.py trial 20   -> diverse sample (one recent planning/zoning doc per town), standard API
       python tools/extract.py pending N  -> next N documents not yet extracted, standard API
       python tools/extract.py batch N    -> collect finished batches, submit next N pending as a Message Batch (half price),
                                             then wait (up to WAIT_MIN minutes) and collect
       python tools/extract.py redo-all   -> mark every document for re-extraction (after a schema/prompt change)
Writes rows to `projects`, sets documents.extracted_at, and logs token use and cost.
"""
import json, os, sys, time
from datetime import datetime, timezone
import anthropic
from psycopg.types.json import Jsonb
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(__file__))
from db import connect

MODELS = ["claude-sonnet-5-5", "claude-sonnet-5"]
PRICE_IN, PRICE_OUT = 2.0, 10.0          # USD per million tokens (Sonnet, standard); batch = half
EXTRACT_VERSION = 2                      # bump when fields/prompt change: older extractions get redone
MAX_CHARS = 60000                        # cap very long packets (~15k tokens)

FIELDS = {
    "plan_number": "Municipal file/case/plan number exactly as written (e.g. 'ZA2026-09', 'LD 02-24', 'A-2026-00558'), else null",
    "name": "Project name as the municipality refers to it (e.g. 'Peregrine Tower', 'St. Luke's Medical Office'), else a short descriptive name",
    "address": "Street address(es) of the property, with municipality if stated",
    "parcel": "Tax parcel / PIN / tax map number(s) exactly as written, else null",
    "owner": "Property owner / landowner if named, else null",
    "applicant": "Applicant / appellant / developer if named, else null",
    "request": "What is being asked or decided, one sentence (e.g. 'Variance for 3.83 ft side setback for a garage')",
    "zoning": "Zoning district code and/or name if stated, else null",
    "size": "Acreage, square footage, units, stories, lots, etc. if stated, else null",
    "acres": "Total site/parcel acreage as a plain number (e.g. 12.5); convert from square feet if only that is given for land; null if not stated",
    "square_feet": "Building / floor area in square feet as a plain number (e.g. 10600); null if not stated",
    "use": "Proposed or existing use (e.g. 'self storage', 'drive-thru coffee', 'warehouse', 'single-family home')",
    "category": "One of: retail (stores, auto sales, gas/car wash, tire/auto service, banks), restaurant (incl. drive-thru coffee), office, medical, hotel/hospitality, industrial/warehouse (incl. flex, self storage, distillery/brewery production), multifamily (apartments, townhouses, senior living), mixed-use, residential subdivision (new lots/homes), residential minor (shed/garage/fence/deck/addition/setback on one existing home), institutional (schools, churches, government, zoo), agricultural, infrastructure/utility, zoning text or map amendment, other. Use 'other' only when none fits; infer from the use and names when reasonable.",
    "stage": "One of: sketch/concept, preliminary plan, final plan, plan approved, hearing scheduled, hearing continued, decision granted, decision denied, withdrawn, extension/time request, recorded, ordinance/rezoning, other",
    "outcome": "Vote or decision if this document records one (e.g. 'Approved 5-0 with conditions'), else null",
    "evidence": "Short verbatim quote (<=200 chars) from the document supporting this record",
}
TOOL = {
    "name": "record_projects",
    "description": "Record every real-estate / land-use matter in the document (zoning appeals, variances, special exceptions, conditional uses, subdivisions, land developments, rezonings, time extensions, sidewalk/curb waivers, redevelopment property actions). Skip routine items (minutes approval, bills, personnel, proclamations, general budget items).",
    "input_schema": {"type": "object", "properties": {
        "projects": {"type": "array", "items": {"type": "object",
                     "properties": {k: {"type": (["number", "null"] if k in ("acres", "square_feet") else ["string", "null"]), "description": v} for k, v in FIELDS.items()},
                     "required": ["name", "request", "category", "stage", "evidence"]}},
        "notes": {"type": ["string", "null"], "description": "Anything unclear about this document (e.g. scanned text unreadable)"}},
        "required": ["projects"]},
}
SYSTEM = ("You extract land-use and development project records from Pennsylvania municipal meeting documents "
          "(agendas, minutes, packets, public notices). Be exact: copy names, addresses, parcel and case numbers "
          "as written; never invent values - use null when a field is not in the document. One record per distinct "
          "property/application. Scanned text may contain OCR errors; correct obvious ones only when certain. "
          "Always answer by calling the record_projects tool exactly once (with an empty list if there are no projects).")

TRIAL_SQL = """
with ranked as (
  select id, municipality_key, row_number() over (partition by municipality_key order by
           (board ~* 'zoning|planning|notice|public')::int desc, (kind in ('minutes'))::int, meeting_date desc nulls last) rn
  from documents where coalesce(chars,0) between 300 and 400000 and body is not null)
select d.* from documents d join ranked r using (id) where r.rn = 1 order by d.municipality_key limit %s"""
PENDING_SQL = """select * from documents d where (extracted_at is null or coalesce(extract_version, 0) < """ + str(EXTRACT_VERSION) + """)
  and body is not null and coalesce(chars,0) >= 100
  and not exists (select 1 from extract_batches b, jsonb_each_text(b.doc_ids) e where b.status = 'submitted' and e.value = d.id)
  order by meeting_date desc nulls last limit %s"""


SETUP_SQL = """
alter table projects add column if not exists category text;
alter table projects add column if not exists acres numeric;
alter table projects add column if not exists square_feet numeric;
create table if not exists extract_batches (id text primary key, model text, status text, doc_ids jsonb,
  submitted_at timestamptz default now(), finished_at timestamptz, input_tokens bigint, output_tokens bigint, cost numeric);
alter table extract_batches enable row level security;
alter table documents add column if not exists extract_version int;
"""


def num(x):
    try:
        return float(str(x).replace(",", "")) if x not in (None, "") else None
    except ValueError:
        return None


def request_for(d, model):
    header = (f"Municipality: {d['municipality']} ({d['county']} County)\nBoard: {d['board']}\nDocument type: {d['kind']}\n"
              f"Meeting date: {d['meeting_date']}\nSource: {d['url']}\n\n--- DOCUMENT TEXT ---\n")
    return dict(model=model, max_tokens=8000, system=SYSTEM, tools=[TOOL], tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": header + d["body"][:MAX_CHARS]}])


def save(conn, d, content, usage):
    """Write the projects found in one model reply; returns number of projects or None if unusable."""
    out = next((b.input for b in content if b.type == "tool_use"), None)
    if out is None:
        print(f"  ! {d['id']}: model did not call the tool; skipped"); return None
    projects = out.get("projects") or []
    with conn.transaction():
        conn.execute("delete from projects where document_id = %s", (d["id"],))
        for p in projects:
            conn.execute("""insert into projects (document_id, municipality_key, board, meeting_date, plan_number, name, address,
                             parcel, owner, applicant, request, zoning, size, acres, square_feet, use, category, stage, outcome, raw)
                            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                         (d["id"], d["municipality_key"], d["board"], d["meeting_date"], p.get("plan_number"), p.get("name"),
                          p.get("address"), p.get("parcel"), p.get("owner"), p.get("applicant"), p.get("request"),
                          p.get("zoning"), p.get("size"), num(p.get("acres")), num(p.get("square_feet")), p.get("use"),
                          p.get("category"), p.get("stage"), p.get("outcome"), Jsonb(p)))
        conn.execute("update documents set extracted_at = now(), extract_version = %s where id = %s", (EXTRACT_VERSION, d["id"]))
    print(f"## {d['municipality']} | {d['board']} | {d['kind']} | {d['meeting_date']} | {len(projects)} projects"
          + (f" | {usage.input_tokens} in / {usage.output_tokens} out" if usage else ""))
    if out.get("notes"): print(f"   note: {out['notes'][:300]}")
    for p in projects:
        print("   - " + json.dumps({k: v for k, v in p.items() if v and k != "evidence"}, ensure_ascii=False)[:400])
    return len(projects)


def pick_model(client):
    for m in MODELS:
        try:
            client.models.retrieve(m); return m
        except anthropic.NotFoundError:
            continue
    sys.exit("no configured model is available")


def run_standard(conn, client, mode, limit):
    model = pick_model(client)
    cur = conn.cursor(row_factory=dict_row)
    docs = cur.execute(TRIAL_SQL if mode == "trial" else PENDING_SQL, (int(limit),)).fetchall()
    print(f"{len(docs)} documents to extract ({mode}, {model})")
    tin = tout = nproj = 0
    for d in docs:
        try:
            msg = client.messages.create(**request_for(d, model))
        except anthropic.APIError as e:
            print(f"  ! {d['id']}: {e}"); continue
        tin += msg.usage.input_tokens; tout += msg.usage.output_tokens
        nproj += save(conn, d, msg.content, msg.usage) or 0
    print(f"\nTOTAL: {len(docs)} documents, {nproj} project records, {tin} in + {tout} out tokens = "
          f"${tin/1e6*PRICE_IN + tout/1e6*PRICE_OUT:.2f} (standard pricing)")


def collect_batches(conn, client):
    """Save results of any submitted batches that have finished. Returns number still running."""
    cur = conn.cursor(row_factory=dict_row)
    running = 0
    for b in cur.execute("select * from extract_batches where status = 'submitted'").fetchall():
        info = client.messages.batches.retrieve(b["id"])
        if info.processing_status != "ended":
            c = info.request_counts
            print(f"batch {b['id']}: {info.processing_status} ({c.succeeded} done, {c.processing} processing)")
            running += 1; continue
        ids = b["doc_ids"]  # custom_id -> document id
        docs = {r["id"]: r for r in cur.execute("select * from documents where id = any(%s)", (list(ids.values()),)).fetchall()}
        tin = tout = nproj = ok = 0
        for res in client.messages.batches.results(b["id"]):
            d = docs.get(ids.get(res.custom_id))
            if not d:
                continue
            if res.result.type != "succeeded":
                print(f"  ! {d['id']}: {res.result.type}"); continue
            m = res.result.message
            tin += m.usage.input_tokens; tout += m.usage.output_tokens
            n = save(conn, d, m.content, m.usage)
            if n is not None:
                nproj += n; ok += 1
        cost = (tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT) / 2
        conn.execute("""update extract_batches set status='ended', finished_at=now(), input_tokens=%s, output_tokens=%s, cost=%s
                        where id=%s""", (tin, tout, cost, b["id"])); conn.commit()
        print(f"\nBATCH {b['id']} DONE: {ok}/{len(ids)} documents, {nproj} project records, "
              f"{tin} in + {tout} out tokens = ${cost:.2f} (batch pricing)")
    return running


def run_batch(conn, client, limit):
    collect_batches(conn, client)
    model = pick_model(client)
    cur = conn.cursor(row_factory=dict_row)
    docs = cur.execute(PENDING_SQL, (int(limit),)).fetchall()
    if docs:
        ids = {f"d{i}": d["id"] for i, d in enumerate(docs)}
        reqs = [{"custom_id": cid, "params": request_for(d, model)} for cid, d in zip(ids, docs)]
        batch = client.messages.batches.create(requests=reqs)
        conn.execute("insert into extract_batches (id, model, status, doc_ids) values (%s,%s,'submitted',%s)",
                     (batch.id, model, Jsonb(ids))); conn.commit()
        print(f"Submitted batch {batch.id}: {len(docs)} documents ({model})")
    else:
        print("No pending documents to submit.")
    deadline = time.time() + int(os.environ.get("WAIT_MIN", "50")) * 60
    while collect_batches(conn, client) and time.time() < deadline:
        time.sleep(60)


def main(mode="trial", limit="20"):
    client = anthropic.Anthropic()
    conn = connect()
    conn.execute(SETUP_SQL); conn.commit()
    if mode == "redo-all":
        n = conn.execute("update documents set extracted_at = null, extract_version = null where extracted_at is not null").rowcount
        conn.commit(); print(f"{n} documents marked for re-extraction"); return
    if mode == "batch":
        run_batch(conn, client, limit)
    else:
        run_standard(conn, client, mode, limit)
    conn.close()


if __name__ == "__main__":
    main(*sys.argv[1:])
