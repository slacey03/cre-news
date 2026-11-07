"""
Collection health check. Runs after each daily collection.

Reads data/<source>/health.json (one entry per run, written by the collectors) and documents.json, and flags:
  ALERT  - the source errored on its last 2 runs in a row
  ALERT  - the listing page found 0 documents although it normally finds some (site likely redesigned/moved)
  ALERT  - the source did not run at all in the last 36 hours
  ALERT  - no new documents for 75+ days (boards meet at least monthly)
  WARN   - a single error (often a temporary outage), or listing count dropped by 70%+ vs normal,
           or no new documents for 45+ days
Writes data/health.json and health_report.md; exits 0. The workflow turns ALERTs into a GitHub issue.
"""
import glob, json, os, statistics, sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
NOW = datetime.now(timezone.utc)
COLLECT_CONCLUSION = os.environ.get("COLLECT_CONCLUSION", "success")


def parse(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def check(key):
    path = os.path.join(ROOT, "data", key)
    hist = json.load(open(os.path.join(path, "health.json"))) if os.path.exists(os.path.join(path, "health.json")) else []
    docs = json.load(open(os.path.join(path, "documents.json"))) if os.path.exists(os.path.join(path, "documents.json")) else {}
    name = next((v.get("municipality") for v in docs.values() if v.get("municipality")), key)
    last_new = max((parse(v.get("collected_at") or "") for v in docs.values() if v.get("collected_at")), default=None)
    out = dict(key=key, name=name, level="ok", problems=[], runs=len(hist), documents=len(docs),
               last_new=last_new.date().isoformat() if last_new else None)
    def flag(level, msg):
        out["problems"].append(f"{level}: {msg}")
        if level == "ALERT" or out["level"] == "ok":
            out["level"] = level.lower()
    if not hist:
        return out  # health tracking starts with the next run
    last = hist[-1]
    out.update(last_run=last["run_at"], status=last["status"], found=last.get("found"), new=last.get("new"))
    ran = parse(last["run_at"])
    if ran and NOW - ran > timedelta(hours=36):
        flag("ALERT", f"did not run since {ran:%b %d}")
    if last["status"] == "error":
        streak = 0
        for h in reversed(hist):
            if h["status"] != "error": break
            streak += 1
        flag("ALERT" if streak >= 2 else "WARN", f"error on {streak} run(s) in a row: {last.get('error')}")
    prev = [h["found"] for h in hist[:-1][-10:] if h.get("status") == "ok" and h.get("found") is not None]
    if last["status"] == "ok" and last.get("found") is not None and prev:
        normal = statistics.median(prev)
        if last["found"] == 0 and normal > 0:
            flag("ALERT", f"listing page found 0 documents (normally {normal:g}); the site may have been redesigned or moved")
        elif normal >= 5 and last["found"] < 0.3 * normal:
            flag("WARN", f"listing found {last['found']} documents, normally {normal:g}")
    if last_new:
        days = (NOW - last_new).days
        if days >= 75:
            flag("ALERT", f"no new documents in {days} days")
        elif days >= 45:
            flag("WARN", f"no new documents in {days} days")
    return out


def save_db(results):
    """Latest health per source in Supabase (private table) for a future status panel."""
    sys.path.insert(0, os.path.dirname(__file__))
    from db import connect
    from psycopg.types.json import Jsonb
    conn = connect()
    conn.execute("""create table if not exists source_health (key text primary key, name text, level text, problems jsonb,
                    found int, new int, documents int, last_new date, last_run timestamptz, checked_at timestamptz default now());
                    alter table source_health enable row level security;""")
    for r in results:
        conn.execute("""insert into source_health (key, name, level, problems, found, new, documents, last_new, last_run, checked_at)
                        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                        on conflict (key) do update set name=excluded.name, level=excluded.level, problems=excluded.problems,
                          found=excluded.found, new=excluded.new, documents=excluded.documents, last_new=excluded.last_new,
                          last_run=excluded.last_run, checked_at=now()""",
                     (r["key"], r["name"], r["level"], Jsonb(r["problems"]), r.get("found"), r.get("new"), r["documents"],
                      r.get("last_new"), r.get("last_run")))
    conn.commit(); conn.close()


def main():
    keys = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(ROOT, "data", "*", "documents.json")))
    keys += sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(ROOT, "data", "*", "health.json"))
                   if os.path.basename(os.path.dirname(p)) not in keys)
    results = [check(k) for k in keys]
    alerts = [r for r in results if r["level"] == "alert"]
    warns = [r for r in results if r["level"] == "warn"]
    if COLLECT_CONCLUSION not in ("success", "skipped"):
        alerts.insert(0, dict(key="collection-run", name="Daily collection run", level="alert",
                              problems=[f"ALERT: the daily collection workflow ended with '{COLLECT_CONCLUSION}'"]))
    summary = dict(checked_at=NOW.isoformat(timespec="seconds"), sources=len(results), alerts=len(alerts),
                   warnings=len(warns), results=results)
    json.dump(summary, open(os.path.join(ROOT, "data", "health.json"), "w"), indent=1)

    lines = [f"**Collection health — {NOW:%b %d, %Y %H:%M} UTC**", "",
             f"{len(results)} sources checked · {len(alerts)} alert(s) · {len(warns)} warning(s)", ""]
    for title, group in (("Alerts (need attention)", alerts), ("Warnings (watching)", warns)):
        if group:
            lines += [f"### {title}"]
            for r in group:
                lines += [f"- **{r['name']}** (`{r['key']}`)"] + [f"  - {p}" for p in r["problems"]]
            lines.append("")
    lines += ["<details><summary>All sources</summary>", "", "| Source | Status | Found | New | Last new doc | Docs |",
              "|---|---|---|---|---|---|"]
    for r in results:
        v = lambda x: "" if x is None else x
        lines.append(f"| {r['name']} | {r['level']} | {v(r.get('found'))} | {v(r.get('new'))} | {r.get('last_new') or ''} | {r['documents']} |")
    lines += ["", "</details>"]
    open(os.path.join(ROOT, "health_report.md"), "w").write("\n".join(lines))
    print("\n".join(lines[:60]))
    if os.environ.get("SUPABASE_DB_URL"):
        save_db(results)
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
        f.write(f"alerts={len(alerts)}\nwarnings={len(warns)}\n")


if __name__ == "__main__":
    main()
