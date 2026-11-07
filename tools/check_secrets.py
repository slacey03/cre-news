"""One-off: confirm required secrets exist and the Anthropic key works (never prints values)."""
# re-run 2026-11-07: Supabase connection test (after URL fix)
import os, json, urllib.request
for name in ("ANTHROPIC_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
    print(name, "present" if os.environ.get(name) else "missing")
key = os.environ.get("ANTHROPIC_API_KEY")
if key:
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        data=json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 5,
                         "messages": [{"role": "user", "content": "Reply with OK"}]}).encode())
    try:
        r = json.load(urllib.request.urlopen(req, timeout=60))
        print("Anthropic API test: OK, model replied:", r["content"][0]["text"].strip()[:20])
    except urllib.error.HTTPError as e:
        print("Anthropic API test FAILED:", e.code, e.read().decode()[:200])

# Supabase: read-only connection test (lists the API schema; writes nothing)
u, k = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
if u and k:
    req = urllib.request.Request(u + "/rest/v1/", headers={"apikey": k, "Authorization": "Bearer " + k})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=60))
        print("Supabase test: OK, connected; existing tables:", sorted((r.get("paths") or {}).keys())[:20])
    except urllib.error.HTTPError as e:
        print("Supabase test FAILED:", e.code, e.read().decode()[:200])
    except Exception as e:
        print("Supabase test FAILED:", type(e).__name__, str(e)[:200])
