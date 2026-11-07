"""One-off research: what MyTown (CC BY 4.0 dataset, credit MyTown / theboringparts.com) covers in Pennsylvania.
Step 2: Pennsylvania jurisdictions, platforms, meeting counts and freshness (remote reads, no full download)."""
import os, duckdb
B = "https://huggingface.co/datasets/jazzypajamas/mytown-local-gov-meetings/resolve/main/data/"
con = duckdb.connect(); con.execute("install httpfs; load httpfs;")
os.makedirs("research/out", exist_ok=True)
con.execute(f"create table m as select * from read_parquet('{B}municipalities.parquet')")
con.execute("create table pa as select * from m where upper(state) in ('PA','PENNSYLVANIA')")
con.execute(f"""create table mt as select muni_id, body_name, meeting_date, agenda_url, minutes_url, id
               from read_parquet('{B}meetings.parquet') where muni_id in (select id from pa)""")
con.execute(f"""create table br as select meeting_id from read_parquet('{B}briefs.parquet')
               where meeting_id in (select id from mt)""")
con.execute(f"""create table dc as select meeting_id from read_parquet('{B}decisions.parquet')
               where meeting_id in (select id from mt)""")
con.execute(f"""copy (
  select pa.slug, pa.name, pa.kind, pa.platform,
         count(mt.id) meetings,
         count(*) filter (where mt.meeting_date >= '2026-01-01') meetings_2026,
         max(mt.meeting_date) filter (where mt.meeting_date <= '2026-11-07') latest_meeting,
         count(distinct mt.body_name) boards,
         string_agg(distinct mt.body_name, ' | ') board_names,
         count(*) filter (where mt.id in (select meeting_id from br)) with_brief,
         count(*) filter (where mt.id in (select meeting_id from dc)) with_decisions,
         min(mt.agenda_url) sample_source
  from pa left join mt on mt.muni_id = pa.id
  group by all order by latest_meeting desc nulls last) to 'research/out/pa_coverage.csv' (header)""")
s = []
s.append(f"PA jurisdictions: {con.execute('select count(*) from pa').fetchone()[0]}")
s.append("by platform: " + str(con.execute("select platform, count(*) from pa group by 1 order by 2 desc").fetchall()))
s.append("by kind: " + str(con.execute("select kind, count(*) from pa group by 1 order by 2 desc").fetchall()))
s.append(f"PA meetings total: {con.execute('select count(*) from mt').fetchone()[0]}, with brief: {con.execute('select count(*) from br').fetchone()[0]}, with decisions: {con.execute('select count(*) from dc').fetchone()[0]}")
s.append("PA jurisdictions with a meeting in last 30/90 days: " + str(con.execute("""select count(distinct muni_id) filter (where meeting_date between '2026-10-08' and '2026-11-07'),
       count(distinct muni_id) filter (where meeting_date between '2026-08-09' and '2026-11-07') from mt""").fetchone()))
s.append("zoning/planning boards in PA: " + str(con.execute("""select count(distinct muni_id), count(*) from mt
       where body_name ilike '%zoning%' or body_name ilike '%planning%'""").fetchone()))
try:
    s.append("PA business formation rows: " + str(con.execute(f"""select grain, source, count(*), max(formation_date)
       from read_parquet('{B}business_formations.parquet') where upper(state) in ('PA','PENNSYLVANIA') group by 1,2""").fetchall()))
except Exception as e: s.append(f"bf error {e}")
open("research/out/pa_summary.txt", "w").write("\n".join(s) + "\n"); print("\n".join(s))
