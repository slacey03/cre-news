"""One-off research: what MyTown (CC BY 4.0 dataset, credit MyTown / theboringparts.com) covers in Pennsylvania."""
import csv, gzip, os, shutil, sqlite3, urllib.request
URL = "https://huggingface.co/datasets/jazzypajamas/mytown-local-gov-meetings/resolve/main/mytown.db.gz"
urllib.request.urlretrieve(URL, "mytown.db.gz")
print("downloaded", os.path.getsize("mytown.db.gz") // 1_000_000, "MB")
with gzip.open("mytown.db.gz") as f, open("mytown.db", "wb") as o:
    shutil.copyfileobj(f, o)
db = sqlite3.connect("mytown.db")
os.makedirs("research/out", exist_ok=True)
schema = open("research/out/schema.txt", "w")
for (t,) in db.execute("select name from sqlite_master where type in ('table','view') order by name"):
    cols = [r[1] for r in db.execute(f'pragma table_info("{t}")')]
    n = db.execute(f'select count(*) from "{t}"').fetchone()[0]
    schema.write(f"{t} ({n} rows): {', '.join(cols)}\n")
    state_col = next((c for c in cols if c.lower() in ("state", "state_code", "state_abbr", "region", "province")), None)
    if state_col:
        rows = db.execute(f'select * from "{t}" where upper("{state_col}") in ("PA","PENNSYLVANIA")').fetchall()
        if rows:
            with open(f"research/out/pa_{t}.csv", "w", newline="") as fh:
                w = csv.writer(fh); w.writerow(cols); w.writerows(rows[:20000])
            schema.write(f"   -> {len(rows)} Pennsylvania rows saved\n")
schema.close()
print(open("research/out/schema.txt").read())
