"""Новые статьи из RSS-лент арт-изданий (список — в sources_seed.json, раздел "rss")."""

import datetime as dt
import email.utils
import html
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from radar.notify import SEED_FILE, db, log

ATOM = "{http://www.w3.org/2005/Atom}"
# Платные разделы внутри бесплатных изданий (Artnet News Pro).
PAID_CATEGORIES = {"The Back Room", "The Art Detective", "Artnet News Pro", "Pro"}


def feeds():
    return json.loads(SEED_FILE.read_text(encoding="utf-8")).get("rss", [])


def _date(text):
    if not text:
        return ""
    text = text.strip()
    try:
        d = email.utils.parsedate_to_datetime(text)  # RSS: "Thu, 15 Oct 2026 15:00:00 +0000"
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom
        except ValueError:
            return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat()


def _plain(text, limit=280):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def fetch_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ArtRadar/0.1"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        root = ET.fromstring(resp.read())
    items = []
    for it in root.iter("item"):  # RSS 2.0
        if {c.text.strip() for c in it.iter("category") if c.text} & PAID_CATEGORIES:
            continue  # колонки только для подписчиков — программа бесплатная, их не показываем
        items.append({"link": (it.findtext("link") or "").strip(), "title": _plain(it.findtext("title"), 300),
                      "summary": _plain(it.findtext("description")), "published": _date(it.findtext("pubDate"))})
    for e in root.iter(f"{ATOM}entry"):  # Atom
        link = e.find(f"{ATOM}link")
        items.append({"link": link.get("href", "") if link is not None else "",
                      "title": _plain(e.findtext(f"{ATOM}title"), 300),
                      "summary": _plain(e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content")),
                      "published": _date(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"))})
    return [i for i in items if i["link"] and i["title"]]


def collect_news():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS articles (
        link TEXT PRIMARY KEY, source TEXT, category TEXT, title TEXT, summary TEXT,
        published_at TEXT, first_seen_at TEXT, initial INTEGER)""")
    conn.execute("CREATE TABLE IF NOT EXISTS feeds_initialized (url TEXT PRIMARY KEY, initialized_at TEXT)")

    def fetch(feed):
        try:
            return feed, fetch_feed(feed["url"])
        except (urllib.error.URLError, ET.ParseError, TimeoutError, ValueError) as e:
            log(f"Не удалось прочитать ленту «{feed['name']}»: {e}")
            return feed, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, feeds()))

    now = dt.datetime.now().isoformat(timespec="seconds")
    three_days_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
    for feed, items in results:
        if items is None:
            continue
        first_time = conn.execute("SELECT 1 FROM feeds_initialized WHERE url=?", (feed["url"],)).fetchone() is None
        for i in items:
            # При первом подключении ленты новыми считаются только статьи за последние 3 дня.
            initial = int(first_time and (not i["published"] or i["published"] < three_days_ago))
            conn.execute("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?)",
                         (i["link"], feed["name"], feed.get("category", ""), i["title"], i["summary"],
                          i["published"] or now, now, initial))
        if first_time:
            conn.execute("INSERT INTO feeds_initialized VALUES (?,?)", (feed["url"], now))
        conn.commit()


def recent_articles(limit=400):
    # Только издания, которые сейчас есть в каталоге: убранные (например, платные) больше не показываются.
    names = [f["name"] for f in feeds()]
    rows = db().execute("SELECT link, source, category, title, summary, published_at, initial FROM articles "
                        f"WHERE source IN ({','.join('?' * len(names))}) ORDER BY published_at DESC LIMIT ?",
                        (*names, limit)).fetchall()
    return [{"id": link, "source": s, "category": c, "title": t, "summary": sm, "published": p, "initial": bool(ini)}
            for link, s, c, t, sm, p, ini in rows]
