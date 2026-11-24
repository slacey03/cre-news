"""Report ZoningTrack's AI spend: extraction batches recorded in the database (extract_batches.cost), by month.
News linking and BuildLV log their cost per run in the workflow logs; the cost-report workflow adds those."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from db import connect

conn = connect()
rows = conn.execute("""select to_char(date_trunc('month', coalesce(finished_at, submitted_at)), 'YYYY-MM') m,
                              count(*), coalesce(sum(cost), 0), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0),
                              min(submitted_at)::date
                       from extract_batches where status in ('ended', 'done', 'DONE', 'completed') or cost is not null
                       group by 1 order by 1""").fetchall()
for m, n, cost, tin, tout, first in rows:
    print(f"EXTRACT {m} batches={n} cost={float(cost):.2f} in={tin} out={tout} first={first}")
docs = conn.execute("select count(*), count(extracted_at) from documents").fetchone()
print(f"DOCUMENTS total={docs[0]} read={docs[1]}")
