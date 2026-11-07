"""One-off research: what MyTown (CC BY 4.0 dataset, credit MyTown / theboringparts.com) covers in Pennsylvania.
Step 1: list the dataset's files and table schemas without downloading them."""
import json, os, urllib.request
import duckdb
API = "https://huggingface.co/api/datasets/jazzypajamas/mytown-local-gov-meetings/tree/main/data?recursive=true"
files = json.load(urllib.request.urlopen(API))
os.makedirs("research/out", exist_ok=True)
con = duckdb.connect(); con.execute("install httpfs; load httpfs;")
out = open("research/out/files.txt", "w")
for f in files:
    if f.get("type") != "file": continue
    line = f"{f['path']}  {f.get('size',0)//1_000_000} MB"
    if f["path"].endswith(".parquet"):
        url = "https://huggingface.co/datasets/jazzypajamas/mytown-local-gov-meetings/resolve/main/" + f["path"]
        try:
            cols = con.execute(f"describe select * from read_parquet('{url}')").fetchall()
            line += "\n    cols: " + ", ".join(f"{c[0]}:{c[1]}" for c in cols)
            n = con.execute(f"select count(*) from read_parquet('{url}')").fetchone()[0]
            line += f"\n    rows: {n}"
        except Exception as e:
            line += f"\n    error: {e}"
    out.write(line + "\n"); print(line, flush=True)
out.close()
