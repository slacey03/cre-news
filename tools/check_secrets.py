"""One-off: confirm required secrets exist and the Anthropic key works (never prints values)."""
# re-run 2026-11-07: after credits added
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
