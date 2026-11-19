"""Build independent artwork notes from licensed catalogue records."""

import re
from dataclasses import replace

from radar import editorial, editorial_sources as sources, journal_articles


SOURCE = {"id": "open-art-collection", "publisher": "open-art-collection", "name": "Wikimedia Commons",
          "domains": ["commons.wikimedia.org"], "lang": "ru", "origin": "INT"}
DATE = re.compile(r"(?:между (\d{4}) и (\d{4})|(\d{4}))(?: date QS:.*)?$", re.I)
MEDIUM = ((r"oil on canvas|холст[, ]+масло|масло на холсте", "масло на холсте"),
          (r"oil on panel|масло на доске", "масло на деревянной панели"),
          (r"lithograph|литографи", "литография"), (r"watercolou?r|акварел", "акварель"))


def entry(asset):
    record = asset.get("catalogue") or {}
    if (asset.get("role") != "artwork" or asset.get("registry") != "Wikimedia Commons"
            or not sources.allowed(asset.get("source", ""), SOURCE["domains"])
            or not re.fullmatch(r"[a-f0-9]{64}", asset.get("sha256", ""))):
        return None
    artist = record.get("creator", "").strip()
    title = record.get("title", "").strip(" «»„“\"")
    date = DATE.fullmatch(record.get("created", ""))
    facts = " | ".join((record.get("details", ""), record.get("categories", "")))
    if (not 4 <= len(title) <= 95 or not 5 <= len(artist) <= 65 or len(artist.split()) > 5
            or not journal_articles.is_russian(artist) or not journal_articles.is_russian(title)
            or re.search(r"\b(?:or|follower|after|unknown|копия|подражатель|неизвестный)\b", artist, re.I)
            or not date or "PD-Art (" not in facts or "disputed copyright" in facts.casefold()):
        return None
    start, end, year = date.groups()
    if any(not 1000 <= int(part) <= 1949 for part in (start, end, year) if part):
        return None
    material = next((label for pattern, label in MEDIUM if re.search(pattern, facts, re.I)), None)
    return {"artist": artist, "title": title, "years": year or f"{start}–{end}", "medium": material, "image": asset}


def discoveries(cards, history):
    used = {sources.canonical(url) for item in history for url in item.get("collection_sources", [])}
    result, added = [], set()
    for card in cards:
        review = card.item.get("journal_visual") or {}
        if card.direction != "archive" or review.get("subject") not in {"art", "artist"}:
            continue
        works = [work for image in review.get("images", []) if (work := entry(image))
                 and sources.canonical(image["source"]) not in used]
        if not works:
            continue
        lead = works[0]
        url = lead["image"]["source"]
        key = sources.canonical(url)
        if key in added:
            continue
        unique = {work["title"].casefold(): work for work in reversed(works) if work["artist"] == lead["artist"]}
        works = sorted(unique.values(), key=lambda work: work != lead)[:3]
        note = [f'«{work["title"]}» · {work["years"]}' + (f' · {work["medium"]}' if work["medium"] else "")
                for work in works]
        if len(works) == 1:
            summary = f'Год создания · {lead["years"]}.' if "–" not in lead["years"] else f'Период создания · {lead["years"]}.'
            if lead["medium"]:
                summary += f' Техника · {lead["medium"]}.'
        else:
            summary = "\n".join(note)
        title = f'{lead["artist"]} · {lead["title"]}' + (" и другие работы" if len(works) > 1 else "")
        media = [work["image"] for work in works]
        item = {"url": url, "title": title, "summary": summary, "source": SOURCE["id"], "kind": "collection",
                "lang": "ru", "direction": "archive", "type": "archive", "archive_index": card.item.get("archive_index"),
                "from_reserve": True, "reserve_bucket": "archive", "provenance": "Каталог произведений Wikimedia Commons",
                "why": "Авторство, название, время создания и лицензия подтверждены публичной записью файла.",
                "collection_sources": [image["source"] for image in media],
                "journal_visual": {**review, "images": media, "quality": len(media)}}
        candidate = replace(card, item=item, key=key, source=SOURCE, words=editorial.words(title), age=None)
        if not any(editorial.duplicates(candidate, old) for old in history):
            result.append(candidate)
            added.add(key)
    return result
