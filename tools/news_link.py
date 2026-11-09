"""
Link news articles to tracked projects by EXACT street address. No guessing.

For each notable project with a street address (not searched in the last REFRESH_DAYS):
  1. web-search the exact address + municipality (Claude's web search tool),
  2. keep results from approved news outlets only,
  3. fetch each article ourselves (robots-aware) and require that its text contains the project's exact house number
     + street (tools/addr.py) AND names the same municipality - otherwise it is rejected,
  4. ask Claude to confirm the article reports on development/land use at that address (it can only reject), and to
     summarize it in its own words and list facts the article states explicitly (acres, square feet, zoning).
Accepted links are stored in Supabase `project_news`; link_projects.py attaches them to pipeline records.
Usage: python tools/news_link.py [limit] [only-municipality-key-or-project-id ...]
"""
import json, os, re, sys, time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import anthropic
from bs4 import BeautifulSoup
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import addr as A
from civicplus import PoliteSession
from db import connect

MODELS = ["claude-sonnet-5-5", "claude-sonnet-5"]
REFRESH_DAYS = int(os.environ.get("NEWS_REFRESH_DAYS", "30"))   # re-check active projects this often
ACTIVE_DAYS = int(os.environ.get("NEWS_ACTIVE_DAYS", "90"))     # ...if they had board activity this recently
REGIONAL = ("lvpc", "lehigh-county", "northampton-county")  # town unknown -> never searched
NOTABLE = ("retail", "restaurant", "office", "medical", "hotel/hospitality", "industrial/warehouse", "multifamily",
           "mixed-use", "redevelopment property", "residential subdivision", "institutional")
NEWS_DOMAINS = {
    "lehighvalleynews.com": "Lehigh Valley News", "mcall.com": "The Morning Call", "lehighvalleylive.com": "LehighValleyLive",
    "wfmz.com": "WFMZ-TV 69 News", "lehighvalleypublicmedia.org": "Lehigh Valley Public Media", "lvb.com": "Lehigh Valley Business",
    "sauconsource.com": "Saucon Source", "patch.com": "Patch", "pennlive.com": "PennLive", "inquirer.com": "Philadelphia Inquirer",
    "readingeagle.com": "Reading Eagle", "poconorecord.com": "Pocono Record", "tapinto.net": "TAPinto", "69news.com": "WFMZ-TV 69 News",
    "wlvr.org": "Lehigh Valley Public Media", "bizjournals.com": "Philadelphia Business Journal", "therecordherald.com": "The Record Herald",
    "pressenterpriseonline.com": "Press Enterprise", "thehomenewspa.com": "The Home News", "bethlehempress.com": "Bethlehem Press",
    "tnonline.com": "Times News", "pahomepage.com": "PA Home Page", "wnep.com": "WNEP", "lehighvalleystyle.com": "Lehigh Valley Style",
    "therealdeal.com": "The Real Deal", "rebusinessonline.com": "REBusiness", "connectcre.com": "Connect CRE",
    "lvpnews.com": "LVP News", "spotlightpa.org": "Spotlight PA", "whyy.org": "WHYY",
}

SEARCH_PROMPT = ("Search the web for news articles that mention the exact street address \"{addr}\" in {town}, "
                 "Pennsylvania (development, construction, zoning, planning, a business opening or a sale at that "
                 "address). Search news outlets such as lehighvalleynews.com, mcall.com, lehighvalleylive.com, wfmz.com, "
                 "lehighvalleypublicmedia.org, lvb.com and sauconsource.com. Do not explain; just run the searches.")
VERIFY_TOOL = {"name": "verdict", "description": "Verdict on whether the article is about this project's property.",
               "input_schema": {"type": "object", "required": ["about_this_property"], "properties": {
                   "about_this_property": {"type": "boolean", "description": "True only if a main subject of the article is development, construction, zoning/planning approval, a sale, or a business opening AT this exact property. False if the address appears only as a meeting location, government office, polling place, contact/mailing address, event venue, in a list or roundup item unrelated to land use, or in passing."},
                   "published": {"type": ["string", "null"], "description": "Article publication date YYYY-MM-DD if shown"},
                   "headline": {"type": ["string", "null"]},
                   "summary": {"type": ["string", "null"], "description": "1-2 sentences in your own words (do not copy the article) on what it reports about this property"},
                   "acres": {"type": ["number", "null"], "description": "Only if the article states the site acreage"},
                   "square_feet": {"type": ["number", "null"], "description": "Only if the article states building square footage"},
                   "zoning": {"type": ["string", "null"], "description": "Only if the article states the zoning district"}}}}

SQL = """
create table if not exists project_news (
  id bigserial primary key, municipality_key text, addr_key text, matched_address text, url text, outlet text,
  headline text, published date, summary text, facts jsonb, found_at timestamptz default now(),
  unique (municipality_key, addr_key, url));
alter table project_news enable row level security;
drop policy if exists members_read_news on project_news;
create policy members_read_news on project_news for select to authenticated using (public.is_member());
create table if not exists news_searches (municipality_key text, addr_key text, searched_at timestamptz default now(),
  results int, accepted int, primary key (municipality_key, addr_key));
alter table news_searches enable row level security;
"""


def outlet_of(url):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for d, name in NEWS_DOMAINS.items():
        if host == d or host.endswith("." + d):
            return name
    return None


def fetch_once(sess, host, url):
    """One polite attempt (robots rules + crawl delay), no retries: a refused or paywalled article is simply skipped."""
    s = sess.get(host)
    if s is False:
        raise ConnectionError("site unreachable earlier in this run")
    if s is None:
        try:
            s = sess[host] = PoliteSession(host)
        except Exception:
            sess[host] = False  # don't keep retrying an unreachable site
            raise
    if not s.rp.can_fetch(s.s.headers["User-Agent"], url):
        raise PermissionError("robots.txt disallows")
    wait = s.last + s.delay - time.time()
    if wait > 0:
        time.sleep(wait)
    r = s.s.get(url, timeout=30)
    s.last = time.time()
    r.raise_for_status()
    return r.text


def article_text(html, url=""):
    soup = BeautifulSoup(html, "html.parser")
    pub = None
    for prop in ("article:published_time", "og:article:published_time", "datePublished", "pubdate", "date", "dc.date"):
        m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop}) or soup.find("meta", attrs={"itemprop": prop})
        if m and m.get("content") and re.match(r"20\d\d-\d\d-\d\d", m["content"]):
            pub = m["content"][:10]; break
    if not pub:  # JSON-LD (used by WFMZ and many news sites)
        for sc in soup.find_all("script", type="application/ld+json"):
            m = re.search(r'"datePublished"\s*:\s*"(20\d\d-\d\d-\d\d)', sc.string or sc.get_text() or "")
            if m:
                pub = m.group(1); break
    if not pub:
        t = soup.find("time", attrs={"datetime": re.compile(r"^20\d\d-\d\d-\d\d")})
        if t: pub = t["datetime"][:10]
    if not pub:
        m = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", url)
        if m: pub = "-".join(m.groups())
    for t in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        t.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in body.find_all(["p", "h1", "h2", "li"])]
    text = "\n".join(p for p in paras if p)
    title = (soup.find("meta", property="og:title") or {}).get("content") or (soup.title.get_text(strip=True) if soup.title else "")
    return text, title, pub


def notable_projects(conn, only):
    rows = conn.cursor(row_factory=dict_row).execute(
        "select id, municipality_key, municipality, name, address, category, first_seen, last_seen from pipeline "
        "where address ~ '\\d' and category = any(%s) and municipality_key <> all(%s)",
        (list(NOTABLE), list(REGIONAL))).fetchall()
    if only:
        rows = [r for r in rows if r["id"] in only or r["municipality_key"] in only]
    out = []
    for r in rows:
        for a in A.parse(r["address"]):
            out.append((r, a))
    return out


def verify(client, models, project, street, town, url, text, title, pub):
    """Claude's verdict (can only reject). Returns (verdict dict or None, model used, usage)."""
    for m in models:
        try:
            v = client.messages.create(model=m, max_tokens=800, tools=[VERIFY_TOOL], tool_choice={"type": "auto"},
                                       messages=[{"role": "user", "content":
                                                  f"Project: {project} at {street}, {town}, PA.\n"
                                                  f"Article URL: {url}\nTitle: {title}\nPublished: {pub}\n\n{text[:14000]}\n\n"
                                                  "Be strict: if the address is only where a meeting was held, a municipal building, a polling place, "
                                                  "a contact address, or mentioned in passing, answer false. Call the verdict tool once."}])
            return next((b.input for b in v.content if b.type == "tool_use"), None), m, v.usage
        except anthropic.NotFoundError:
            continue
    return None, None, None


def reverify():
    """Re-check every stored link with the current (stricter) rules; fix dates; drop duplicates and unknown-town links."""
    conn = connect(); conn.execute(SQL); conn.commit()
    n = conn.execute("delete from project_news where municipality_key = any(%s)", (list(REGIONAL),)).rowcount
    print(f"removed {n} links filed under a regional body instead of a town")
    rows = conn.cursor(row_factory=dict_row).execute("select * from project_news order by id").fetchall()
    client, models, sess = anthropic.Anthropic(), list(MODELS), {}
    kept = dropped = unchecked = dups = 0
    seen = set()
    tin = tout = 0
    for row in rows:
        dup_key = (row["municipality_key"], row["addr_key"], (row["outlet"] or "").lower(), (row["headline"] or "").lower().strip())
        if dup_key in seen:
            conn.execute("delete from project_news where id = %s", (row["id"],)); conn.commit(); dups += 1; continue
        street, _, town = row["matched_address"].partition(", ")
        a = (A.parse(street) or [None])[0]
        url = row["url"]; host = "{0.scheme}://{0.netloc}".format(urlparse(url))
        html = None
        for attempt in (1, 2):
            try:
                html = fetch_once(sess, host, url); break
            except Exception as e:
                err = type(e).__name__
                if sess.get(host) is False: break
                time.sleep(10)
        if html is None:
            print(f"  ? could not re-check (kept as is): {row['headline'][:70]} ({err})"); unchecked += 1
            seen.add(dup_key); continue
        text, title, pub = article_text(html, url)
        ok = a is not None and any(A.same(a, x) for x in A.parse(text, need_suffix=True)) and A.text_mentions_town(text, town)
        out = None
        if ok:
            out, m, u = verify(client, models, "a development project", street, town, url, text, title, pub)
            if m: models = [m]
            if u: tin += u.input_tokens; tout += u.output_tokens
        if not ok or not out or not out.get("about_this_property"):
            conn.execute("delete from project_news where id = %s", (row["id"],)); conn.commit(); dropped += 1
            print(f"  - removed: {row['matched_address']}: {row['headline'][:80]}")
            continue
        published = out.get("published") or pub
        try:
            published = date.fromisoformat(published[:10]) if published else None
        except ValueError:
            published = None
        facts = {k: out.get(k) for k in ("acres", "square_feet", "zoning") if out.get(k) is not None}
        conn.execute("update project_news set published=%s, headline=%s, summary=%s, facts=%s where id=%s",
                     (published, out.get("headline") or row["headline"], out.get("summary") or row["summary"], Jsonb(facts), row["id"]))
        conn.commit(); kept += 1; seen.add(dup_key)
        print(f"  ok {row['matched_address']}: {published} {(out.get('headline') or row['headline'])[:70]}")
    print(f"REVERIFY: {kept} kept, {dropped} removed, {dups} duplicates removed, {unchecked} could not be re-checked; "
          f"{tin} in + {tout} out tokens = ${tin/1e6*2 + tout/1e6*10:.2f}")


def main(limit=40, *only):
    if str(limit) == "reverify":
        return reverify()
    limit = int(limit)
    conn = connect(); conn.execute(SQL); conn.commit()
    cutoff = datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)
    searched = {(m, k): t for m, k, t in conn.execute("select municipality_key, addr_key, searched_at from news_searches").fetchall()}
    active_since = date.today() - timedelta(days=ACTIVE_DAYS)
    todo, seen = [], set()
    for r, a in notable_projects(conn, set(only)):
        k = (r["municipality_key"], A.key(a))
        if k in seen:
            continue
        t = searched.get(k)
        # never searched -> search; searched before -> re-check only if stale AND the project is still active
        if t and (t > cutoff or not r["last_seen"] or r["last_seen"] < active_since):
            continue
        seen.add(k); todo.append((r, a))
    todo.sort(key=lambda x: str(x[0]["first_seen"] or ""), reverse=True)
    todo = todo[:limit]
    print(f"{len(todo)} project addresses to search (limit {limit})")

    client = anthropic.Anthropic()
    model, sess = None, {}
    usage = dict(tin=0, tout=0, searches=0)
    accepted_total = 0
    for r, a in todo:
        street = f"{a[0]} {(a[1] or '').upper() + ' ' if a[1] else ''}{a[2].title()}{' ' + a[3].title() if a[3] else ''}"
        town = r["municipality"]
        # 1) search
        msg = None
        for m in ([model] if model else MODELS):
            try:
                msg = client.messages.create(model=m, max_tokens=1500,
                                             tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                                             messages=[{"role": "user", "content": SEARCH_PROMPT.format(addr=street, town=town)}])
                model = m; break
            except anthropic.NotFoundError:
                continue
            except anthropic.APIError as e:
                print(f"  ! search {street}: {e}"); break
        if msg is None:
            continue
        usage["tin"] += msg.usage.input_tokens; usage["tout"] += msg.usage.output_tokens
        stu = getattr(msg.usage, "server_tool_use", None)
        usage["searches"] += getattr(stu, "web_search_requests", 0) or 0
        urls = []
        for b in msg.content:
            if b.type == "web_search_tool_result" and isinstance(b.content, list):
                urls += [x.url for x in b.content if getattr(x, "url", None)]
        urls = list(dict.fromkeys(u for u in urls if outlet_of(u) and not u.lower().endswith(".pdf")))
        accepted = 0
        for url in urls[:8]:
            # 2-3) fetch ourselves and require the exact address + municipality in the article text
            host = "{0.scheme}://{0.netloc}".format(urlparse(url))
            try:
                html = fetch_once(sess, host, url)
            except Exception as e:
                print(f"  - {street}: skip {urlparse(url).netloc} ({type(e).__name__})"); continue
            text, title, pub = article_text(html, url)
            hits = [x for x in A.parse(text, need_suffix=True) if A.same(a, x)]
            if not hits or not A.text_mentions_town(text, town):
                print(f"  - {street}: no exact address+town in text: {url[:90]}"); continue
            # 4) Claude confirms (can only reject) and summarizes in its own words
            out, _, u = verify(client, [model], r["name"], street, town, url, text, title, pub)
            if u: usage["tin"] += u.input_tokens; usage["tout"] += u.output_tokens
            if not out or not out.get("about_this_property"):
                print(f"  - {street}: address present but article not about this property: {url[:90]}"); continue
            published = out.get("published") or pub
            try:
                published = date.fromisoformat(published[:10]) if published else None
            except ValueError:
                published = None
            facts = {k: out.get(k) for k in ("acres", "square_feet", "zoning") if out.get(k) is not None}
            conn.execute("""insert into project_news (municipality_key, addr_key, matched_address, url, outlet, headline, published, summary, facts)
                            values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            on conflict (municipality_key, addr_key, url) do update set headline=excluded.headline,
                              published=excluded.published, summary=excluded.summary, facts=excluded.facts""",
                         (r["municipality_key"], A.key(a), f"{street}, {town}", url, outlet_of(url),
                          out.get("headline") or title, published, out.get("summary"), Jsonb(facts)))
            accepted += 1
            print(f"  + {street}, {town}: {outlet_of(url)} {published} - {(out.get('headline') or title)[:80]}")
        conn.execute("""insert into news_searches (municipality_key, addr_key, searched_at, results, accepted) values (%s,%s,now(),%s,%s)
                        on conflict (municipality_key, addr_key) do update set searched_at=now(), results=excluded.results,
                          accepted=excluded.accepted""", (r["municipality_key"], A.key(a), len(urls), accepted))
        conn.commit(); accepted_total += accepted
        print(f"# {street}, {town} [{r['category']}]: {len(urls)} news results, {accepted} linked")
    cost = usage["tin"] / 1e6 * 2 + usage["tout"] / 1e6 * 10 + usage["searches"] * 0.01
    print(f"TOTAL {len(todo)} addresses searched, {accepted_total} articles linked; {usage['searches']} web searches, "
          f"{usage['tin']} in + {usage['tout']} out tokens = ${cost:.2f} (${cost / max(len(todo), 1):.3f} per address)")


if __name__ == "__main__":
    main(*sys.argv[1:])
