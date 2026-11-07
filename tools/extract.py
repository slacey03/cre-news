"""
AI extraction: read collected documents from Supabase and write structured project records.
Usage: python tools/extract.py trial 20   -> diverse sample (one recent planning/zoning doc per town)
       python tools/extract.py pending N  -> next N documents not yet extracted
Writes rows to `projects`, sets documents.extracted_at, and logs token use and cost.
"""
import json, os, sys, time
from datetime import datetime, timezone
import anthropic
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(__file__))
from db import connect

MODELS = ["claude-sonnet-5-5", "claude-sonnet-5"]
PRICE_IN, PRICE_OUT = 2.0, 10.0          # USD per million tokens (Sonnet, standard)
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
    "use": "Proposed or existing use (e.g. 'self storage', 'drive-thru coffee', 'warehouse', 'single-family home')",
    "category": "One of: commercial, retail, restaurant, office, medical, industrial/warehouse, multifamily, mixed-use, residential subdivision, residential minor (shed/fence/deck/addition on one home), institutional, agricultural, infrastructure, other",
    "stage": "One of: sketch/concept, preliminary plan, final plan, plan approved, hearing scheduled, hearing continued, decision granted, decision denied, withdrawn, extension/time request, recorded, ordinance/rezoning, other",
    "outcome": "Vote or decision if this document records one (e.g. 'Approved 5-0 with conditions'), else null",
    "evidence": "Short verbatim quote (<=200 chars) from the document supporting this record",
}
TOOL = {
    "name": "record_projects",
    "description": "Record every real-estate / land-use matter in the document (zoning appeals, variances, special exceptions, conditional uses, subdivisions, land developments, rezonings, time extensions, sidewalk/curb waivers, redevelopment property actions). Skip routine items (minutes approval, bills, personnel, proclamations, general budget items).",
    "input_schema": {"type": "object", "properties": {
        "projects": {"type": "array", "items": {"type": "object",
                     "properties": {k: {"type": ["string", "null"], "description": v} for k, v in FIELDS.items()},
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
PENDING_SQL = "select * from documents where extracted_at is null and body is not null order by meeting_date desc nulls last limit %s"


def main(mode="trial", limit=20):
    client = anthropic.Anthropic()
    model = None
    conn = connect()
    conn.execute("alter table projects add column if not exists category text"); conn.commit()
    cur = conn.cursor(row_factory=__import__("psycopg").rows.dict_row)
    docs = cur.execute(TRIAL_SQL if mode == "trial" else PENDING_SQL, (int(limit),)).fetchall()
    print(f"{len(docs)} documents to extract ({mode})")
    tin = tout = nproj = 0
    for d in docs:
        body = d["body"][:MAX_CHARS]
        header = (f"Municipality: {d['municipality']} ({d['county']} County)\nBoard: {d['board']}\nDocument type: {d['kind']}\n"
                  f"Meeting date: {d['meeting_date']}\nSource: {d['url']}\n\n--- DOCUMENT TEXT ---\n")
        msg = None
        for m in ([model] if model else MODELS):
            try:
                msg = client.messages.create(model=m, max_tokens=8000, system=SYSTEM, tools=[TOOL],
                                             tool_choice={"type": "auto"},
                                             messages=[{"role": "user", "content": header + body}])
                model = m; break
            except anthropic.NotFoundError:
                continue
            except anthropic.APIError as e:
                print(f"  ! {d['id']}: {e}"); time.sleep(5); break
        if msg is None:
            continue
        tin += msg.usage.input_tokens; tout += msg.usage.output_tokens
        out = next((b.input for b in msg.content if b.type == "tool_use"), None)
        if out is None:
            print(f"  ! {d['id']}: model did not call the tool; skipped"); continue
        projects = out.get("projects") or []
        with conn.transaction():
            conn.execute("delete from projects where document_id = %s", (d["id"],))
            for p in projects:
                conn.execute("""insert into projects (document_id, municipality_key, board, meeting_date, plan_number, name,
                                 address, parcel, owner, applicant, request, zoning, size, use, category, stage, outcome, raw)
                                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                             (d["id"], d["municipality_key"], d["board"], d["meeting_date"], p.get("plan_number"), p.get("name"),
                              p.get("address"), p.get("parcel"), p.get("owner"), p.get("applicant"), p.get("request"),
                              p.get("zoning"), p.get("size"), p.get("use"), p.get("category"), p.get("stage"), p.get("outcome"), Jsonb(p)))
            conn.execute("update documents set extracted_at = now() where id = %s", (d["id"],))
        nproj += len(projects)
        print(f"\n## {d['municipality']} | {d['board']} | {d['kind']} | {d['meeting_date']} | {len(projects)} projects "
              f"| {msg.usage.input_tokens} in / {msg.usage.output_tokens} out tokens")
        if out.get("notes"): print(f"   note: {out['notes']}")
        for p in projects:
            print("   - " + json.dumps({k: v for k, v in p.items() if v}, ensure_ascii=False))
    cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
    print(f"\nTOTAL: {len(docs)} documents, {nproj} project records, model {model}, "
          f"{tin} input + {tout} output tokens = ${cost:.2f} (standard pricing; batch mode is half)")
    conn.close()


if __name__ == "__main__":
    main(*(sys.argv[1:] or ["trial", "20"]))
