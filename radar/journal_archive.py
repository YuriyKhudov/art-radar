"""Collect evergreen Russian articles from publishers' public subject indexes."""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

from radar import editorial_sources as sources, journal_articles


def collect(config):
    indexes = [(source, url) for source in config["sources"] if source.get("enabled")
               for url in source.get("archive_indexes", [])]

    def harvest(pair):
        source, url = pair
        report = {"source": f"{source['id']}-archive:{urlsplit(url).path}", "name": source["name"],
                  "endpoint": url, "status": "ok", "count": 0}
        try:
            page = journal_articles.public_page(url, source["domains"], {})
            if not page:
                return [], {**report, "status": "Публичный индекс недоступен"}
            return parse(page[0], source, url, report)
        except (ImportError, OSError, ValueError) as error:
            return [], {**report, "status": type(error).__name__}

    with ThreadPoolExecutor(max_workers=4) as executor:
        batches = list(executor.map(harvest, indexes))
    return [item for items, _ in batches for item in items], [row for _, row in batches]


def parse(text, source, index, report):
    from lxml import html

    document = html.fromstring(text)
    items, seen = [], set()
    for node in document.xpath('//li[contains(concat(" ", normalize-space(@class), " "), " result-item ")]'):
        for link in node.xpath('./a[@href][./h3]'):
            url = urljoin(index, link.get("href"))
            if (not sources.allowed(url, source["domains"]) or not re.fullmatch(r"/(?:mag/[\w-]+|materials/\d+)",
                                                                                urlsplit(url).path)
                    or url in seen):
                continue
            title = sources.clean(link.xpath("string(./h3)"))
            evidence = sources.clean(node.xpath("string(./p)"))
            if not title or len(evidence.split()) < 7:
                continue
            seen.add(url)
            items.append(sources.entry(source, url=url, title=title, text=evidence, date=None, key=url,
                                       kind="article", origin=f"Тематический указатель · {index}",
                                       extra={"archive_index": index, "language": "ru"}))
    return items, {**report, "count": len(items)}
