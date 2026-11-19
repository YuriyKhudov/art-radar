"""Read public publisher metadata while keeping catalogue changes separate from news."""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from radar import editorial_sources as sources

ARTICLE = {"Article", "NewsArticle", "BlogPosting", "ReportageNewsArticle"}


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.links, self.structure, self.headings, self.dates = {}, [], [], [], []
        self.script = None
        self.heading = None

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "meta" and fields.get("content"):
            key = fields.get("property") or fields.get("name") or fields.get("itemprop")
            if key:
                self.meta.setdefault(key.lower(), fields["content"])
        if tag == "a" and fields.get("href"):
            self.links.append(fields["href"])
        if tag == "time" and fields.get("datetime"):
            self.dates.append(fields["datetime"])
            if fields.get("itemprop") == "datePublished":
                self.meta.setdefault("article:published_time", fields["datetime"])
        if tag in {"h1", "h2", "h3"}:
            self.heading = []
        if tag == "script" and (fields.get("type") or "").lower() == "application/ld+json":
            self.script = []

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3"} and self.heading is not None:
            self.headings.append(sources.clean("".join(self.heading)))
            self.heading = None
        if tag == "script" and self.script is not None:
            try:
                self.structure.append(json.loads("".join(self.script)))
            except ValueError:
                pass
            self.script = None

    def handle_data(self, text):
        if self.script is not None:
            self.script.append(text)
        elif self.heading is not None:
            self.heading.append(text)


def records(structure):
    for node in structure:
        if isinstance(node, list):
            yield from records(node)
        elif isinstance(node, dict):
            yield node
            yield from records(node.get("@graph", []))


def parse(text):
    page = Page()
    page.feed(text)
    return page


def named_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return sources.timestamp(value)
    except (ValueError, TypeError, OverflowError):
        return None


def observation(source, url, title, evidence, published, events=(), catalogue=()):
    key = sources.canonical(url)
    content = {"title": title, "evidence": evidence, "published": published, "events": events,
               "catalogue": catalogue}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"id": f"{source['id']}:{key}", "url": url, "title": title, "published": published,
            "digest": digest, "event_dates": events}


def public_rules(source):
    robots_url = f"https://{urlsplit(source['url']).netloc}/robots.txt"
    checker = RobotFileParser(robots_url)
    try:
        raw, _ = sources.request(robots_url, source["domains"], 128_000)
    except HTTPError as error:
        if error.code not in {404, 410}:
            raise
        raw = b""
    checker.parse(raw.decode("utf-8", errors="replace").splitlines())
    return checker


def page_item(page, source, url):
    node = next((n for n in records(page.structure)
                 if ARTICLE.intersection([n.get("@type")] if isinstance(n.get("@type"), str) else n.get("@type") or [])), {})
    title = sources.clean(node.get("headline") or page.meta.get("og:title") or next(iter(page.headings), source["name"]))
    evidence = sources.clean(node.get("description") or page.meta.get("og:description") or page.meta.get("description"))
    published = named_date(node.get("datePublished") or page.meta.get("article:published_time"))
    events = {n["startDate"] for n in records(page.structure) if isinstance(n.get("startDate"), str)}
    if source.get("event_index"):
        events.update(date for date in page.dates if named_date(date))
    artwork = [n.get("name") for n in records(page.structure) if n.get("@type") == "VisualArtwork"]
    catalogue = sorted({text for text in [*page.headings, *artwork] if isinstance(text, str) and text})[:80]
    seen = observation(source, url, title, evidence, published, sorted(events), catalogue)
    # Date-modified and the timing of an exhibition cannot establish a publication date.
    if not published or len(evidence.split()) < 8 or not (node or page.meta.get("article:published_time")):
        return None, seen
    item = sources.entry(source, title=title, url=url, text=evidence, date=published, key=seen["id"],
                         kind="article", origin=f"Публичные метаданные · {source['url']}",
                         extra={"language": source["lang"], "monitor_child": url != source["url"]})
    return item, seen


def monitor(config):
    active = [source for source in config["sources"] if source.get("page_monitor") and source.get("enabled")]

    def get(source):
        result = {"source": source["id"], "name": source["name"], "endpoint": source["url"], "mode": "page",
                  "owner": source.get("artist_id")}
        try:
            rules = public_rules(source)
            root = source["url"]
            if not rules.can_fetch("ArtRadarJournal", root):
                return [], {**result, "status": "Закрыт для автоматического чтения", "count": 0, "observations": []}
            raw, _ = sources.request(root, source["domains"], 2_000_000)
            if source.get("format") == "feed":
                items = list(sources.feed_entries(raw, {**source, "feed": root}))
                seen = [observation(source, item["url"], item["title"], item["evidence"], item["published"])
                        for item in items]
            else:
                page = parse(raw.decode("utf-8", errors="replace"))
                article, observed = page_item(page, source, root)
                items, seen = ([article] if article else []), [observed]
                pattern = source.get("child_paths")
                links = sorted({urljoin(root, href).split("#")[0] for href in page.links if href and href[0] != "#"})
                links = [url for url in links if pattern and sources.allowed(url, source["domains"])
                         and re.search(pattern, urlsplit(url).path, re.I)]
                for url in links[:3]:
                    if not rules.can_fetch("ArtRadarJournal", url) or sources.canonical(url) == sources.canonical(root):
                        continue
                    try:
                        data, _ = sources.request(url, source["domains"], 2_000_000)
                        entry, note = page_item(parse(data.decode("utf-8", errors="replace")), source, url)
                    except (OSError, ValueError):
                        continue
                    seen.append(note)
                    if entry:
                        items.append(entry)
            return items, {**result, "status": "ok", "count": len(items), "observations": seen}
        except (OSError, ValueError, sources.ET.ParseError) as error:
            status = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
            return [], {**result, "status": status, "count": 0, "observations": []}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(get, active))
    return [item for items, _ in results for item in items], [report for _, report in results]
