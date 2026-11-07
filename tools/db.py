"""Shared Supabase Postgres connection (parses SUPABASE_DB_URL by hand so any password symbols work)."""
import os, sys
import psycopg


def connect():
    url = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not url.startswith(("postgresql://", "postgres://")):
        sys.exit(f"SUPABASE_DB_URL must start with postgresql:// (present={bool(url)})")
    rest = url.split("://", 1)[1]
    cred, _, hostpart = rest.rpartition("@")
    user, _, pw = cred.partition(":")
    hostport, _, dbname = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    dbname = dbname.split("?")[0] or "postgres"
    tries = [pw] + ([pw[1:-1]] if pw.startswith("[") and pw.endswith("]") else [])
    for i, p in enumerate(tries):
        try:
            return psycopg.connect(host=host, port=int(port or 5432), dbname=dbname, user=user, password=p,
                                   sslmode="require", prepare_threshold=None, connect_timeout=30)
        except psycopg.OperationalError as e:
            print(f"DB connect attempt {i+1} failed: {(str(e).replace(p, '***') if p else str(e))[:300]}")
    sys.exit(1)
