#!/usr/bin/env python3
"""
Builds index.html for Blenheim Chalcot's Daily Tech Briefing from live RSS feeds.

No LLM involvement: every headline/summary/link comes verbatim from the
feed's own <title>/<description>/<link>, filtered by the feed's own
published/updated timestamp. This avoids the hallucinated-URL failure mode
of asking an LLM to "search the web" and report back.
"""

import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

LOOKBACK_HOURS = 24
MAX_ITEMS_PER_SECTION = 4
MIN_ITEMS_PER_SECTION = 2
SCRAPE_TIMEOUT_SECONDS = 6
SCRAPE_MIN_CHARS = 80
PROMO_KEYWORDS = [
    "giveaway",
    "sweepstakes",
    "coupon",
    "promo code",
    "discount code",
    "enter to win",
    "win a ",
    "/deals/",
]
USER_AGENT = (
    "Mozilla/5.0 (compatible; BCDailyBriefingBot/1.0; "
    "+https://github.com/WilliamGitty/bc-daily-briefing)"
)

TECHCRUNCH_AI = "https://techcrunch.com/category/artificial-intelligence/feed/"
VERGE_AI = "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"
ARSTECHNICA_AI = "https://arstechnica.com/ai/feed/"

BBC_TECH = "http://feeds.bbci.co.uk/news/technology/rss.xml"
VERGE_TECH = "https://www.theverge.com/rss/tech/index.xml"
ARSTECHNICA_TECH = "https://feeds.arstechnica.com/arstechnica/index"

TECHCRUNCH_ENTERPRISE = "https://techcrunch.com/category/enterprise/feed/"

TECHCRUNCH_FINTECH = "https://techcrunch.com/category/fintech/feed/"
FINEXTRA = "https://www.finextra.com/rss/headlines.aspx"
FINTECH_TIMES = "https://thefintechtimes.com/feed/"

EDTECH_MAGAZINE = "https://edtechmagazine.com/higher/rss.xml"

TECHCRUNCH_STARTUPS = "https://techcrunch.com/category/startups/feed/"
SIFTED = "https://sifted.eu/feed"

HACKER_NEWS = "https://feeds.feedburner.com/TheHackersNews"
KREBS_SECURITY = "https://krebsonsecurity.com/feed/"

SECTIONS = [
    {"key": "ai", "title": "AI", "feeds": [TECHCRUNCH_AI, VERGE_AI, ARSTECHNICA_AI]},
    {"key": "tech", "title": "Tech", "feeds": [BBC_TECH, VERGE_TECH, ARSTECHNICA_TECH]},
    {
        "key": "enterprise",
        "title": "Enterprise Software & AI at Work",
        "feeds": [TECHCRUNCH_ENTERPRISE],
    },
    {
        "key": "fintech",
        "title": "Fintech",
        "feeds": [TECHCRUNCH_FINTECH, FINEXTRA, FINTECH_TIMES],
    },
    {"key": "edtech", "title": "Edtech", "feeds": [EDTECH_MAGAZINE]},
    {
        "key": "startups_vc",
        "title": "Startups & Venture Capital",
        "feeds": [TECHCRUNCH_STARTUPS, SIFTED],
    },
    {"key": "cybersecurity", "title": "Cybersecurity", "feeds": [HACKER_NEWS, KREBS_SECURITY]},
]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(raw):
    if not raw:
        return ""
    text = html.unescape(TAG_RE.sub("", raw))
    text = WS_RE.sub(" ", text).strip()
    return text


MAX_SUMMARY_CHARS = 500


def trim_summary(text):
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    length = 0
    for part in parts:
        if out and length + len(part) > MAX_SUMMARY_CHARS:
            break
        out.append(part)
        length += len(part) + 1
    return " ".join(out).strip()


def scrape_article_paragraph(url):
    """Fetch the linked article and pull its opening real body text.

    Returns None on any failure so callers can fall back to the feed's own
    teaser summary. Never fabricates text - only extracts what's actually
    on the page.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=SCRAPE_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "aside", "footer", "header", "figure"]):
            tag.decompose()
        container = soup.find("article") or soup.body
        if container is None:
            return None
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > 40]
        paragraphs = list(dict.fromkeys(paragraphs))
        if not paragraphs:
            return None
        return trim_summary(" ".join(paragraphs))
    except requests.RequestException:
        return None


def is_live_blog(link):
    return "/live/" in link.lower()


def entry_published(entry):
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def source_name(entry, feed_url):
    host = urlparse(entry.get("link", feed_url)).netloc
    host = host.replace("www.", "")
    known = {
        "bbc.co.uk": "BBC News",
        "techcrunch.com": "TechCrunch",
        "theverge.com": "The Verge",
        "arstechnica.com": "Ars Technica",
        "finextra.com": "Finextra",
        "thefintechtimes.com": "The Fintech Times",
        "edtechmagazine.com": "EdTech Magazine",
        "sifted.eu": "Sifted",
        "thehackernews.com": "The Hacker News",
        "krebsonsecurity.com": "Krebs on Security",
    }
    for k, v in known.items():
        if k in host:
            return v
    return host or "Source"


def fetch_recent_items(feed_url, cutoff, keyword=None):
    parsed = feedparser.parse(feed_url)
    now = datetime.now(timezone.utc)
    items = []
    seen_links = set()
    for entry in parsed.entries:
        published = entry_published(entry)
        if published is None or published < cutoff:
            continue
        # A genuine news article can't have a future publish date. Some
        # feeds (e.g. Finextra) mix in event/webinar listings whose "date"
        # field is the event date, not a publish timestamp - reject those.
        if published > now:
            continue
        title = clean_text(entry.get("title", ""))
        summary = trim_summary(clean_text(entry.get("summary", entry.get("description", ""))))
        link = entry.get("link", "")
        if not link.lower().startswith(("http://", "https://")):
            continue
        if link in seen_links:
            continue
        seen_links.add(link)
        promo_haystack = f"{title} {link}".lower()
        if any(kw in promo_haystack for kw in PROMO_KEYWORDS):
            continue
        if keyword:
            haystack = f"{title} {summary}".lower()
            if keyword.lower() not in haystack:
                continue
        items.append(
            {
                "title": title,
                "summary": summary,
                "link": link,
                "published": published,
                "source": source_name(entry, feed_url),
            }
        )
    items.sort(key=lambda i: i["published"], reverse=True)
    return items


def select_diverse(candidates):
    """Pick up to MAX_ITEMS_PER_SECTION items, round-robin across sources.

    Candidates must already be sorted by recency (most recent first). This
    stops any single prolific outlet from filling a whole section just
    because it happened to publish the most today - each source gets one
    slot per round before any source gets a second. Rolling live-blogs are
    capped at one slot total. Falls back to a single source's own stories
    if nothing else in the lookback window qualifies.
    """
    queues = {}
    order = []
    for item in candidates:
        if item["source"] not in queues:
            queues[item["source"]] = []
            order.append(item["source"])
        queues[item["source"]].append(item)

    picked = []
    live_blog_count = 0
    made_progress = True
    while len(picked) < MAX_ITEMS_PER_SECTION and made_progress:
        made_progress = False
        for source in order:
            if len(picked) >= MAX_ITEMS_PER_SECTION:
                break
            queue = queues[source]
            while queue:
                candidate = queue.pop(0)
                if is_live_blog(candidate["link"]):
                    if live_blog_count >= 1:
                        continue
                    live_blog_count += 1
                picked.append(candidate)
                made_progress = True
                break
    return picked


def build_sections():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    used_links = set()
    rendered_sections = []

    for section in SECTIONS:
        candidates = []
        for feed_url in section["feeds"]:
            candidates.extend(fetch_recent_items(feed_url, cutoff, keyword=section.get("keyword")))

        candidates.sort(key=lambda i: i["published"], reverse=True)
        deduped = [c for c in candidates if c["link"] not in used_links]
        picked = select_diverse(deduped)

        for item in picked:
            used_links.add(item["link"])
            scraped = scrape_article_paragraph(item["link"])
            if scraped and len(scraped) > max(len(item["summary"]), SCRAPE_MIN_CHARS):
                item["summary"] = scraped

        rendered_sections.append({"title": section["title"], "items": picked})

    return rendered_sections


def render_html(sections, today_str, updated_str):
    story_blocks = []
    for section in sections:
        if section["items"]:
            stories_html = "\n".join(
                f'''        <article class="story">
          <a class="headline" href="{html.escape(item['link'])}" target="_blank" rel="noopener">{html.escape(item['title'])}</a>
          <p class="summary">{html.escape(item['summary'])}</p>
          <span class="source"><a href="{html.escape(item['link'])}" target="_blank" rel="noopener">{html.escape(item['source'])}</a></span>
        </article>'''
                for item in section["items"]
            )
        else:
            stories_html = '        <p class="no-story">No qualifying story in the last 24 hours.</p>'

        story_blocks.append(
            f'''      <section class="section">
        <h2>{html.escape(section['title'])}</h2>
{stories_html}
      </section>'''
        )

    sections_html = "\n".join(story_blocks)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blenheim Chalcot Daily Tech Briefing — {today_str}</title>
<style>
  :root {{
    --navy: #1a2a4a;
    --cream: #f4f4f2;
    --muted: #767676;
    --source: #999999;
    --rule: #d8d4c8;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--cream);
    font-family: Georgia, 'Times New Roman', serif;
    color: #1a1a1a;
  }}
  .masthead {{
    background: var(--navy);
    padding: 32px 20px;
    text-align: center;
  }}
  .masthead h1 {{
    margin: 0;
    color: #ffffff;
    font-size: clamp(22px, 5vw, 32px);
    font-weight: bold;
    letter-spacing: 0.5px;
  }}
  .masthead .date {{
    display: block;
    margin-top: 8px;
    color: #c9d2e3;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 14px;
    letter-spacing: 0.5px;
  }}
  .intro {{
    max-width: 680px;
    margin: 16px auto 0;
    padding: 0 20px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 12px;
    color: var(--muted);
    font-style: italic;
    text-align: center;
  }}
  main {{
    max-width: 680px;
    margin: 0 auto;
    padding: 8px 20px 60px;
  }}
  .section {{
    margin-top: 32px;
  }}
  .section h2 {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 15px;
    color: var(--navy);
    text-transform: uppercase;
    letter-spacing: 1px;
    border-bottom: 2px solid var(--navy);
    padding-bottom: 6px;
    margin: 0 0 14px;
  }}
  .story {{
    padding: 14px 0;
    border-bottom: 1px solid var(--rule);
  }}
  .story:last-child {{
    border-bottom: none;
  }}
  .headline {{
    display: block;
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 17px;
    font-weight: bold;
    color: var(--navy);
    text-decoration: none;
    line-height: 1.35;
  }}
  .headline:hover {{
    text-decoration: underline;
  }}
  .summary {{
    margin: 6px 0 8px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 14px;
    color: var(--muted);
    line-height: 1.5;
  }}
  .source {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-variant: small-caps;
    letter-spacing: 0.5px;
  }}
  .source a {{
    color: var(--source);
    text-decoration: none;
  }}
  .source a:hover {{
    text-decoration: underline;
  }}
  .no-story {{
    padding: 6px 0 0;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 13px;
    color: var(--source);
    font-style: italic;
  }}
  footer {{
    max-width: 680px;
    margin: 0 auto 40px;
    padding: 0 20px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    color: var(--source);
    text-align: center;
  }}
</style>
</head>
<body>
  <div class="masthead">
    <h1>Blenheim Chalcot Daily Tech Briefing</h1>
    <span class="date">{today_str}, Updated as of {html.escape(updated_str)}</span>
  </div>
  <p class="intro">All stories below were published within the last 24 hours, pulled directly from source RSS feeds. Where a section has no qualifying story, that is stated explicitly.</p>
  <main>
{sections_html}
  </main>
  <footer>Built automatically from live RSS feeds &mdash; no AI-generated content.</footer>
</body>
</html>
'''


MIN_SECTIONS_WITH_CONTENT = len(SECTIONS) // 2


def main():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%A, %d %B %Y")
    now_uk = now_utc.astimezone(ZoneInfo("Europe/London"))
    updated_str = now_uk.strftime("%H:%M %Z")
    sections = build_sections()
    output = render_html(sections, today_str, updated_str)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)

    for section in sections:
        print(f"\n=== {section['title']} ({len(section['items'])} item(s)) ===")
        if not section["items"]:
            print("  (no qualifying story in the last 24 hours)")
        for item in section["items"]:
            print(f"  - {item['title']}")
            print(f"    published: {item['published'].isoformat()}")
            print(f"    link: {item['link']}")

    sections_with_content = sum(1 for s in sections if s["items"])
    if sections_with_content < MIN_SECTIONS_WITH_CONTENT:
        raise SystemExit(
            f"Only {sections_with_content}/{len(sections)} sections had any "
            f"qualifying stories (need at least {MIN_SECTIONS_WITH_CONTENT}) - "
            "this looks like a pipeline-wide failure, not a quiet news day. "
            "Failing the build so the last good deploy stays live."
        )


if __name__ == "__main__":
    main()
