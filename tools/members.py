"""Apply db/access.sql and report the members list (hash prefix, note, end date, active) — never prints emails.
Also self-tests the sign-in and member checks with throwaway addresses inside a transaction that is rolled back:
an active member passes, an expired one and a non-member are refused."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from db import connect

conn = connect()
conn.execute(open(os.path.join(os.path.dirname(__file__), "..", "db", "access.sql")).read()); conn.commit()
print("access rules applied")
for h, note, exp, active in conn.execute(
        "select email_hash, note, expires_at, (expires_at is null or expires_at > now()) from members order by added_at"):
    print(f"  {h[:10]}…  active={active}  ends={exp or '-'}  {note}")

def check(email):
    conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"email": email}),))
    return conn.execute("select public.is_member(), public.can_login(%s)", (email,)).fetchone()

ok = True
with conn.transaction(force_rollback=True):
    h = lambda e: f"encode(sha256(convert_to('{e}', 'UTF8')), 'hex')"
    conn.execute(f"insert into members (email_hash, note, expires_at) values ({h('selftest-active@example.com')}, 'test', now() + interval '1 day')")
    conn.execute(f"insert into members (email_hash, note, expires_at) values ({h('selftest-expired@example.com')}, 'test', now() - interval '1 day')")
    for email, want in [("selftest-active@example.com", True), ("SelfTest-Active@Example.com", True),
                        ("selftest-expired@example.com", False), ("nobody@example.com", False)]:
        got = check(email)
        good = got == (want, want)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {email}: member={got[0]} can_sign_in={got[1]} (want {want})")
sys.exit(0 if ok else 1)
