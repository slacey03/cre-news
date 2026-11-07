"""Create/refresh the schema and upsert every collected document into Supabase Postgres.
Reads data/<key>/documents.json + text files. Idempotent: safe to run after every collection."""
import glob, json, os, sys
import psycopg
from psycopg.types.json import Jsonb

ROOT = os.path.join(os.path.dirname(__file__), "..")
COLS = {"municipality", "county", "board", "kind", "meeting_date", "url", "source_page", "sha256", "pages",
        "chars", "ocr", "rolling", "agenda_items", "collected_at"}

def main():
    url = os.environ.get("SUPABASE_DB_URL", "").strip()
    # safe format check (never prints the password)
    from urllib.parse import urlsplit
    sp = urlsplit(url) if "://" in url else None
    print(f"SUPABASE_DB_URL: present={bool(url)} length={len(url)} starts={url[:13]!r} "
          f"host={sp.hostname if sp else None} port={sp.port if sp else None} user_set={bool(sp and sp.username)} "
          f"password_set={bool(sp and sp.password)} placeholder_left={'YOUR-PASSWORD' in url}")
    if not url.startswith(("postgresql://", "postgres://")):
        sys.exit("SUPABASE_DB_URL must start with postgresql://")
    with psycopg.connect(url, prepare_threshold=None) as conn:
        conn.execute(open(os.path.join(ROOT, "db", "schema.sql")).read())
        n = 0
        for idx in sorted(glob.glob(os.path.join(ROOT, "data", "*", "documents.json"))):
            key = os.path.basename(os.path.dirname(idx))
            docs = json.load(open(idx))
            if not docs:
                continue
            first = next(iter(docs.values()))
            conn.execute("""insert into municipalities (key, name, county) values (%s,%s,%s)
                            on conflict (key) do update set name=excluded.name, county=excluded.county, updated_at=now()""",
                         (key, first.get("municipality"), first.get("county")))
            for doc_id, v in docs.items():
                tp = os.path.join(os.path.dirname(idx), "text", doc_id + ".txt")
                body = open(tp, errors="replace").read().replace("\x00", "") if os.path.exists(tp) else None
                title = v.get("title") or (v.get("text") if isinstance(v.get("text"), str) else None)
                meta = {k: x for k, x in v.items() if k not in COLS and k not in ("title", "text")}
                conn.execute("""
                    insert into documents (id, municipality_key, municipality, county, board, kind, meeting_date, title, url,
                        source_page, sha256, pages, chars, ocr, rolling, agenda_items, meta, body, collected_at)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict (id) do update set board=excluded.board, kind=excluded.kind, meeting_date=excluded.meeting_date,
                        title=excluded.title, url=excluded.url, source_page=excluded.source_page, sha256=excluded.sha256,
                        pages=excluded.pages, chars=excluded.chars, ocr=excluded.ocr, rolling=excluded.rolling,
                        agenda_items=excluded.agenda_items, meta=excluded.meta, body=excluded.body,
                        collected_at=excluded.collected_at, loaded_at=now(),
                        extracted_at = case when documents.sha256 is distinct from excluded.sha256 then null else documents.extracted_at end
                    """,
                    (f"{key}/{doc_id}", key, v.get("municipality"), v.get("county"), v.get("board"), v.get("kind"),
                     v.get("meeting_date") or None, title, v.get("url"), v.get("source_page"), v.get("sha256"),
                     v.get("pages"), v.get("chars"), v.get("ocr"), v.get("rolling"),
                     Jsonb(v["agenda_items"]) if v.get("agenda_items") is not None else None, Jsonb(meta), body,
                     v.get("collected_at")))
                n += 1
        conn.commit()
        tot = conn.execute("select count(*), count(distinct municipality_key) from documents").fetchone()
        print(f"Loaded {n} documents. Supabase now holds {tot[0]} documents from {tot[1]} municipalities.")

if __name__ == "__main__":
    main()
# run: 2026-11-07T12:09
