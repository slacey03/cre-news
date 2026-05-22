"""
Eastern PA Commercial Real Estate News Scraper
Pulls from RSS feeds + Google News, filters by geography + CRE relevance,
scores articles, outputs JSON for the dashboard.
"""

import feedparser
import requests
import json
import re
import time
from datetime import datetime, timezone
from dateutil import parser as dateparser
from urllib.parse import quote_plus, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib

# ---------- CONFIG ----------

# Direct RSS feeds from Eastern PA outlets that publish them.
# Mix of statewide, regional, and city/business journals.
RSS_FEEDS = [
    # Lehigh Valley
    ("Lehigh Valley Business", "https://www.lvb.com/feed/"),
    ("WFMZ 69 News - Business", "https://www.wfmz.com/search/?f=rss&t=article&c=news/business&l=25&s=start_time&sd=desc"),
    ("WFMZ 69 News - Local", "https://www.wfmz.com/search/?f=rss&t=article&c=news/local&l=50&s=start_time&sd=desc"),
    ("Lehigh Valley News", "https://www.lehighvalleynews.com/news.rss"),

    # Philadelphia region
    ("Philadelphia Inquirer - Real Estate", "https://www.inquirer.com/arc/outboundfeeds/rss/category/real-estate/?outputType=xml"),
    ("Philadelphia Inquirer - Business", "https://www.inquirer.com/arc/outboundfeeds/rss/category/business/?outputType=xml"),
    ("Philadelphia Business Journal", "https://www.bizjournals.com/philadelphia/news/rss.xml"),
    ("Billy Penn", "https://billypenn.com/feed/"),

    # Statewide / Harrisburg / Lancaster / Reading / Berks
    ("PennLive", "https://www.pennlive.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("Central Penn Business Journal", "https://www.cpbj.com/feed/"),
    ("LancasterOnline - Business", "https://lancasteronline.com/search/?f=rss&t=article&c=business&l=25&s=start_time&sd=desc"),
    ("Reading Eagle - Business", "https://www.readingeagle.com/business/feed/"),
    ("Spotlight PA", "https://www.spotlightpa.org/feeds/articles.rss"),

    # Trade press relevant to PA
    ("ROI-NJ (regional)", "https://www.roi-nj.com/feed/"),
    ("REBusinessOnline - Northeast", "https://rebusinessonline.com/category/northeast/feed/"),
]

# Google News RSS searches — these catch articles from outlets we don't directly subscribe to.
# Each query is constructed to bias toward Eastern PA + CRE topics.
GOOGLE_NEWS_QUERIES = [
    '"Lehigh Valley" (retail OR "shopping center" OR lease OR developer)',
    '"Lehigh Valley" (zoning OR rezoning OR "land development")',
    '"Allentown" OR "Bethlehem" OR "Easton" (commercial real estate OR development)',
    '"Berks County" OR "Reading PA" (commercial OR development OR zoning)',
    '"Lancaster County" (commercial real estate OR industrial OR development)',
    '"Northampton County" PA (development OR commercial OR zoning)',
    '"Bucks County" PA (commercial real estate OR development)',
    '"Montgomery County" PA (commercial real estate OR retail)',
    '"Chester County" PA (commercial real estate OR development)',
    '"Eastern Pennsylvania" (warehouse OR industrial OR distribution OR logistics)',
    'PPL Center OR "NIZ Allentown" (development OR investment)',
    '"Lehigh Valley" (medical office OR healthcare real estate)',
    'Pennsylvania (groundbreaking OR "broke ground") (retail OR office OR industrial)',
]

# Geography filter — article must mention at least one of these to be Eastern PA relevant.
EASTERN_PA_TERMS = [
    # Counties
    "lehigh county", "northampton county", "berks county", "bucks county",
    "montgomery county", "chester county", "delaware county", "philadelphia county",
    "lancaster county", "schuylkill county", "carbon county", "monroe county",
    "pike county", "wayne county", "lackawanna county", "luzerne county",
    "dauphin county", "york county", "cumberland county", "lebanon county",
    # Cities/boroughs in Eastern PA
    "allentown", "bethlehem", "easton", "reading", "lancaster", "harrisburg",
    "philadelphia", "scranton", "wilkes-barre", "pottstown", "norristown",
    "doylestown", "west chester", "media pa", "king of prussia", "exton",
    "quakertown", "emmaus", "macungie", "hellertown", "nazareth", "phillipsburg",
    "stroudsburg", "hazleton", "pottsville", "lebanon pa", "york pa",
    # Regional terms
    "lehigh valley", "eastern pa", "eastern pennsylvania", "southeastern pa",
    "southeastern pennsylvania", "northeastern pa", "northeastern pennsylvania",
    "south central pa", "philadelphia region", "delaware valley",
    "poconos", "main line",
    # Anchor institutions / known projects
    "ppl center", "niz", "neighborhood improvement zone", "lehigh university",
    "lafayette college", "muhlenberg college",
]

# Topic relevance — article must hit on CRE / development / zoning themes.
CRE_TERMS = {
    # High-value terms (weight 3)
    "retail leasing": 3, "commercial real estate": 3, "shopping center": 3,
    "ground breaking": 3, "broke ground": 3, "lease signed": 3,
    "rezoning": 3, "zoning change": 3, "land development plan": 3,
    "investment sale": 3, "sold for": 3, "trade for": 3,
    "medical office building": 3, "industrial park": 3, "distribution center": 3,
    "mixed-use development": 3, "build-to-suit": 3, "tenant rep": 3,
    "site plan": 3, "subdivision plan": 3,

    # Medium-value (weight 2)
    "retail": 2, "warehouse": 2, "industrial": 2, "office space": 2,
    "developer": 2, "development": 2, "construction": 2, "lease": 2,
    "tenant": 2, "landlord": 2, "broker": 2, "real estate": 2,
    "zoning": 2, "planning commission": 2, "township": 2, "borough": 2,
    "redevelopment": 2, "anchor tenant": 2, "big box": 2, "strip mall": 2,
    "acquisition": 2, "acquired": 2, "purchased": 2, "investor": 2,
    "groundbreaking": 2, "expansion": 2, "opening": 2, "relocating": 2,

    # Supporting context (weight 1)
    "property": 1, "building": 1, "facility": 1, "store": 1,
    "restaurant": 1, "grocery": 1, "supermarket": 1, "drugstore": 1,
    "pharmacy": 1, "bank branch": 1, "dollar general": 1, "dollar tree": 1,
    "walmart": 1, "target": 1, "wawa": 1, "sheetz": 1, "aldi": 1,
    "starbucks": 1, "chipotle": 1, "jersey mike": 1, "chick-fil-a": 1,
    "amazon": 1, "fedex": 1, "ups ": 1,
}

# Hard-exclude topics — these typically aren't useful CRE intel.
EXCLUDE_TERMS = [
    "obituary", "obituaries", "high school sports", "college sports",
    "horoscope", "celebrity", "movie review", "concert review",
    "recipe", "food recipe", "weather forecast",
]

# Per-feed timeout
TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (CRE-NewsBot/1.0; +https://example.com/bot)"


# ---------- FUNCTIONS ----------

def normalize_text(s):
    return (s or "").lower()


def fetch_feed(name, url):
    """Fetch and parse a single RSS feed."""
    try:
        headers = {"User-Agent": USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        if resp.status_code != 200:
            return name, [], f"HTTP {resp.status_code}"
        parsed = feedparser.parse(resp.content)
        return name, parsed.entries, None
    except Exception as e:
        return name, [], str(e)


def fetch_google_news(query):
    """Build a Google News RSS URL for a query and fetch it."""
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    return fetch_feed(f"Google News: {query[:50]}", url)


def article_score(title, summary):
    """Score article relevance. Returns (geo_hit, topic_score, matched_terms)."""
    text = normalize_text(title + " " + summary)

    # Hard exclude
    if any(term in text for term in EXCLUDE_TERMS):
        return False, 0, []

    # Geography check
    geo_matches = [term for term in EASTERN_PA_TERMS if term in text]
    geo_hit = len(geo_matches) > 0

    # Topic scoring
    topic_score = 0
    matched_terms = []
    for term, weight in CRE_TERMS.items():
        if term in text:
            topic_score += weight
            matched_terms.append(term)

    return geo_hit, topic_score, matched_terms[:8]  # cap matched terms shown


def clean_summary(html):
    """Strip HTML tags and clean whitespace from feed summary."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def article_id(url, title):
    """Stable ID for deduplication."""
    base = (url or "") + "|" + (title or "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


def parse_entry(source_name, entry):
    """Normalize one feed entry."""
    title = entry.get("title", "").strip()
    link = entry.get("link", "").strip()
    summary = clean_summary(entry.get("summary", "") or entry.get("description", ""))

    # Try multiple date fields
    pub_raw = entry.get("published") or entry.get("updated") or entry.get("pubDate") or ""
    try:
        pub_dt = dateparser.parse(pub_raw) if pub_raw else None
        if pub_dt and pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    except Exception:
        pub_dt = None

    # Domain for display
    domain = ""
    try:
        domain = urlparse(link).netloc.replace("www.", "")
    except Exception:
        pass

    return {
        "id": article_id(link, title),
        "title": title,
        "link": link,
        "summary": summary,
        "source": source_name,
        "domain": domain,
        "published": pub_dt.isoformat() if pub_dt else None,
        "published_ts": pub_dt.timestamp() if pub_dt else 0,
    }


def run_scraper(max_workers=8):
    """Fetch all feeds in parallel, score, dedupe, return article list."""
    all_articles = {}
    feed_status = []

    # Build full task list: direct feeds + google news queries
    tasks = list(RSS_FEEDS)
    for q in GOOGLE_NEWS_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
        tasks.append((f"Google News: {q[:60]}", url))

    print(f"Fetching {len(tasks)} feeds with {max_workers} workers...")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_feed, name, url): name for name, url in tasks}
        for fut in as_completed(futures):
            name, entries, err = fut.result()
            feed_status.append({
                "source": name,
                "count": len(entries),
                "error": err,
            })
            if err:
                print(f"  ✗ {name}: {err}")
                continue
            print(f"  ✓ {name}: {len(entries)} entries")

            for entry in entries:
                art = parse_entry(name, entry)
                if not art["title"] or not art["link"]:
                    continue

                geo_hit, topic_score, matched = article_score(art["title"], art["summary"])

                # Keep only Eastern PA relevant AND topic-relevant articles
                if not geo_hit or topic_score < 2:
                    continue

                art["topic_score"] = topic_score
                art["matched_terms"] = matched

                # Dedupe by ID, keep highest score
                existing = all_articles.get(art["id"])
                if not existing or existing["topic_score"] < topic_score:
                    all_articles[art["id"]] = art

    # Sort by composite: recency + score
    articles = list(all_articles.values())
    now_ts = datetime.now(timezone.utc).timestamp()

    def composite(a):
        age_days = max(0, (now_ts - a["published_ts"]) / 86400) if a["published_ts"] else 999
        recency = max(0, 30 - age_days) / 30  # 0..1, newer is better
        return a["topic_score"] * 2 + recency * 10

    for a in articles:
        a["rank_score"] = round(composite(a), 2)

    articles.sort(key=lambda a: a["rank_score"], reverse=True)

    return articles, feed_status


def main():
    start = time.time()
    articles, status = run_scraper()
    elapsed = round(time.time() - start, 1)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": elapsed,
        "article_count": len(articles),
        "feed_status": status,
        "articles": articles,
    }

    with open("articles.json", "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n✓ Saved {len(articles)} articles to articles.json ({elapsed}s)")
    print(f"  Top sources: {sorted(set(a['source'] for a in articles[:20]))[:5]}")


if __name__ == "__main__":
    main()
