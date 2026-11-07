"""One-off: export project records (joined with their document) to a JSON file for analysis."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from db import connect
from psycopg.rows import dict_row
conn = connect()
rows = conn.cursor(row_factory=dict_row).execute("""
  select p.id, p.document_id, p.municipality_key, p.board, p.meeting_date::text, p.plan_number, p.name, p.address, p.parcel,
         p.owner, p.applicant, p.request, p.zoning, p.size, p.acres::float, p.square_feet::float, p.use, p.category, p.stage, p.outcome,
         d.kind, d.municipality
  from projects p join documents d on d.id = p.document_id order by p.municipality_key, p.meeting_date""").fetchall()
for r in rows:
    print("ROW " + json.dumps(r, default=str, ensure_ascii=False))
print(len(rows), "rows exported")
