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
REFRESH_DAYS = int(os.environ.get("NEWS_REFRESH_DAYS", "30"))
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
                   "about_this_property": {"type": "boolean", "description": "True only if the article reports on development, construction, zoning/planning, a business, or a sale AT this exact address"},
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


def article_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        t.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in body.find_all(["p", "h1", "h2", "li"])]
    text = "\n".join(p for p in paras if p)
    title = (soup.find("meta", property="og:title") or {}).get("content") or (soup.title.get_text(strip=True) if soup.title else "")
    pub = None
    for prop in ("article:published_time", "og:article:published_time", "datePublished"):
        m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop}) or soup.find("meta", attrs={"itemprop": prop})
        if m and m.get("content"):
            pub = m["content"][:10]; break
    return text, title, pub


def notable_projects(conn, only):
    rows = conn.cursor(row_factory=dict_row).execute(
        "select id, municipality_key, municipality, name, address, category, first_seen from pipeline "
        "where address ~ '\\d' and category = any(%s)", (list(NOTABLE),)).fetchall()
    if only:
        rows = [r for r in rows if r["id"] in only or r["municipality_key"] in only]
    out = []
    for r in rows:
        for a in A.parse(r["address"]):
            out.append((r, a))
    return out


def main(limit=40, *only):
    limit = int(limit)
    conn = connect(); conn.execute(SQL); conn.commit()
    cutoff = datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)
    done = {(m, k) for m, k, t in conn.execute("select municipality_key, addr_key, searched_at from news_searches").fetchall() if t > cutoff}
    todo, seen = [], set()
    for r, a in notable_projects(conn, set(only)):
        k = (r["municipality_key"], A.key(a))
        if k in done or k in seen:
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
            text, title, pub = article_text(html)
            hits = [x for x in A.parse(text, need_suffix=True) if A.same(a, x)]
            if not hits or not A.text_mentions_town(text, town):
                print(f"  - {street}: no exact address+town in text: {url[:90]}"); continue
            # 4) Claude confirms (can only reject) and summarizes in its own words
            v = client.messages.create(model=model, max_tokens=800, tools=[VERIFY_TOOL], tool_choice={"type": "auto"},
                                       messages=[{"role": "user", "content":
                                                  f"Project: {r['name']} at {street}, {town}, PA ({r['category']}).\n"
                                                  f"Article URL: {url}\nTitle: {title}\nPublished: {pub}\n\n{text[:14000]}\n\n"
                                                  "Call the verdict tool once."}])
            usage["tin"] += v.usage.input_tokens; usage["tout"] += v.usage.output_tokens
            out = next((b.input for b in v.content if b.type == "tool_use"), None)
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
