"""Artist-specific queries and editorial metadata for the existing journal."""

import argparse
import datetime as dt
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from html import escape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

from radar import editorial_sources as sources, translate

REGISTRY = sources.ROOT / "artists.json"
BONUS = {"main": 6, "secondary": 3, "historical": 2}
HEADINGS = {"works": "🖼 Новые работы", "studio": "🖌 В мастерской художника", "people": "🎬 Художник крупным планом",
            "technique": "💡 Секреты мастерства", "exhibition": "🎨 Художник в поле зрения",
            "research": "🔎 Художественное открытие"}
FEATURES = {
    "people": r"\b(interview|in conversation|documentary|artist talk|film|интервью|бесед\w+|документальн\w+)\b",
    "studio": r"in (?:the |his |her |their )?studio|painting process|in the workshop|в мастерской|творческ\w+ процесс",
    "exhibition": r"\b(exhibition|exhibit|solo show|retrospective|выставк\w+|вернисаж\w+|ретроспектив\w+)\b",
    "research": r"restoration|newly discovered|research|conservation|реставраци|исследовани|обнаруженн",
    "technique": r"demonstration|masterclass|painting techniques|colour palette|brushwork|мастер-класс|техник\w+|разбор",
    "works": r"\b(paintings?|portraits?|drawings?|artworks?|sketchbook|живопис\w+|картин\w+|портрет\w+|рисунк\w+)\b",
}
VISUAL_VIDEO = re.compile(r"\b(?:painting time[- ]lapse|time[- ]lapse of (?:painting|drawing)|"
                          r"watch me (?:paint|draw)|painting process from start to finish|"
                          r"silent (?:painting|drawing) demonstration)\b", re.I)
RU_SUBTITLES = re.compile(r"russian subtitles (?:are )?available|with russian subtitles|"
                           r"доступны русские субтитры|с русскими субтитрами", re.I)
NAME = r"[A-ZА-ЯЁÀ-ÖØ-Þ][a-zа-яёà-öø-ÿ'’\-]+(?:\s+(?:de |da |van |von |del )?[A-ZА-ЯЁÀ-ÖØ-Þ][a-zа-яёà-öø-ÿ'’\-]+){1,3}"
NOISE = re.compile(r"look of|about us|keep reading|membership|gift guide|art prize|buy now|promo code|промокод|скидк|"
                   r"subscribers only|members-only|paywall|только по подписке", re.I)
HISTORICAL_DISCOVERY = re.compile(r"great art explained|recreating the (?:colour|color) palette|"
                                  r"\b(?:art history|artist from the past)\b|классики живописи", re.I)
INCIDENTAL = re.compile(r"\bулиц[аеуы]\b|recreates? one of|в стиле", re.I)
FACT = re.compile(r"\b(?:oil|pastels?|charcoal|graphite|gouache|watercolou?rs?|ink|brush(?:es|work)?|"
                  r"pigments?|canvas|papers?|woodcuts?|etchings?|collage|marbl\w+|portraits?|landscapes?|drawings?|"
                  r"painting|layers?|monotypes?|carv\w+|lithograph\w+|mix\w+|paint\w+|render\w+)\b", re.I)
TECHNIQUE = re.compile(r"\b(?:oil|pastels?|charcoal|graphite|gouache|watercolou?rs?|ink|engraving|"
                       r"woodcuts?|etchings?|collag\w+|marbl\w+|acrylic|pigments?)\b", re.I)
TRANSLATION_BUDGET = 1600


def normalize(value):
    plain = "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", plain))


def contains(haystack, needle):
    return bool(needle) and f" {needle} " in f" {haystack} "


def names(artist):
    return [artist["ru"], artist["original"], *artist["aliases"]]


def configure(config, registry=None):
    registry = registry if registry is not None else sources.read(REGISTRY)
    extra = []
    for provider in registry["providers"]:
        extra.append({**provider, "domains": [provider["domain"]], "enabled": True})
    extra.append({"id": "artist-youtube", "publisher": "artist-youtube", "name": "YouTube · поиск Artist Radar",
                  "lang": "en", "domains": ["youtube.com", "youtu.be"], "enabled": True, "priority": 4.4})
    enabled = {artist["id"] for artist in registry["artists"] if artist["enabled"]}
    extra.extend({**page, "page_monitor": True, "enabled": True, "priority": 4.6}
                 for page in registry.get("page_monitors", []) if page["artist_id"] in enabled)
    for artist in registry["artists"]:
        if not artist["enabled"]:
            continue
        for profile in artist["profiles"]:
            method = profile["state"]
            if method not in {"atom", "channel_api"} and not (profile["kind"] == "vk" and profile.get("owner")):
                continue
            if method == "atom":
                host = "youtube.com"
                transport = {"feed": f"https://www.youtube.com/feeds/videos.xml?channel_id={profile['channel']}",
                             "feed_domains": [host]}
            elif method == "channel_api":
                host = "rutube.ru"
                transport = {"connector": "rutube", "channel": profile["channel"]}
            else:
                host = "vkvideo.ru"
                transport = {"connector": "vk", "owner": profile["owner"]}
            extra.append({"id": f"artist-{artist['id']}-{host}", "publisher": f"artist-{artist['id']}",
                          "name": artist["ru"], "artist_id": artist["id"], "lang": profile.get("lang", "ru"),
                          "domains": [host], "enabled": True, "creator": True, "priority": 4.6, **transport})
    return {**config, "sources": [*config["sources"], *extra], "artist_radar": registry}


def style_ids(text, registry, lang):
    text = normalize(text)
    found = []
    for style in registry["styles"]:
        terms = [normalize(term) for term in style["ru" if lang == "ru" else "en"]]
        if any(f" {term}" in f" {text}" if lang == "ru" else contains(text, term) for term in terms if term):
            found.append(style["id"])
    return found


def feature_type(title, text):
    for field in (title, text[:560]):
        for kind, cue in FEATURES.items():
            if re.search(cue, field, re.I):
                return kind
    return None


def named_artist(title, text, artists, target=None, owner=None, url=""):
    head, lead = normalize(title), normalize(text[:400])
    for artist in artists:
        if not artist["enabled"] or (target and artist["id"] != target):
            continue
        if artist["id"] == owner:
            return artist
        aliases = [normalize(name) for name in names(artist)]
        context = artist.get("identity_context", [])
        if context and not any(c in (title + " " + text + " " + url).casefold() for c in context):
            continue
        if not INCIDENTAL.search(title) and any(contains(head, name) for name in aliases):
            return artist
        for alias in aliases:
            artist_context = r"(?:artist|painter|printmaker|художни\w+|живопис\w+)\s+" + re.escape(alias) + r"\b"
            author_context = r"^" + re.escape(alias) + r"\s+(?:paint\w+|work\w+|draw\w+|пиш\w+|рису\w+)"
            if re.search(artist_context, lead) or re.search(author_context, lead):
                return artist
    return None


def discovery_name(title, text):
    """Extract a credited subject and corroborate it against the article text."""
    if HISTORICAL_DISCOVERY.search(title):
        return None
    patterns = [rf"^(?:Artist Insights\s*:\s*|Inside the Sketchbook of |A Conversation with (?:Printmaker |Artist )?)({NAME})",
                rf"^({NAME})\s*:", rf"({NAME})['’]s\b", rf"\bby ({NAME})", NAME]
    name_variants = []
    for pattern in patterns:
        for match in re.finditer(pattern, title):
            tokens = match.group(1 if match.lastindex else 0).split()
            for start in range(len(tokens) - 1):
                for stop in range(len(tokens), start + 1, -1):
                    name_variants.append(" ".join(tokens[start:stop]))
    bio = normalize(text[:3500])
    for name in name_variants:
        normalized = normalize(name)
        if len(normalized.split()) < 2 or not contains(bio, normalized):
            continue
        if not is_person_label(normalized):
            continue
        escaped = re.escape(normalized)
        person = (r"(?:artist|painter|illustrator|printmaker|sculptor|художни\w+)\s+" + escaped + r"\b|"
                  + escaped + r"\s+(?:is |paint\w+|draw\w+|work\w+|create\w+|render\w+|won |use\w+|explore\w+|"
                  r"identify\w+|combin\w+|conceiv\w+|depict\w+|пиш\w+|рису\w+|работа\w+)")
        if re.search(person, bio) or (len(normalized.split()) >= 2 and re.search(rf"\b{escaped}['’]s\b", title, re.I)):
            return name
    return None


def is_person_label(name):
    article_words = {"painting", "paintings", "art21", "workshop", "exhibition", "gallery", "festival", "portraiture",
                     "impressionism", "news", "sketchbook", "tutorial", "charcoal", "watercolor", "illustration"}
    return not article_words.intersection(name.split()) and not any(
        contains(name, term) for term in ("art prize", "art blog", "how to", "art museum", "plein air", "mixed media", "studio visit"))


def annotate(raw, config, source):
    registry = config.get("artist_radar")
    if not registry:
        return raw
    current = raw.get("artist_radar")
    if current:
        if current["stream"] == "discovery" and not is_person_label(normalize(current["artist"])):
            return {k: v for k, v in raw.items() if k not in {"artist_radar", "ru_editorial", "fact_excerpt"}}
        if current["stream"] != "priority" or any(a["id"] == current.get("artist_id") and a["enabled"]
                                                   for a in registry["artists"]):
            return raw
        return {k: v for k, v in raw.items() if k != "artist_radar"}
    title = raw.get("title", "")
    evidence = raw.get("evidence", "")
    lang = raw.get("language") or source["lang"]
    styles = style_ids(title + " " + evidence, registry, lang)
    owner = source.get("artist_id")
    if raw.get("monitor_child") and not source["publisher"].startswith("artist-"):
        owner = None
    artist = named_artist(title, evidence, registry["artists"], owner=owner, url=raw["url"])
    if not artist and not styles:
        return raw
    kind = feature_type(title, evidence) or ("studio" if owner and artist and raw["kind"] == "video" else None)
    if not kind:
        return raw
    if artist:
        info = {"stream": "priority", "artist": artist["ru"], "artist_id": artist["id"], "tier": artist["tier"],
                "historical": artist["tier"] == "historical" or artist.get("historical", False)}
    else:
        info = {"stream": "general"}
    return {**raw, "artist_radar": {**info, "style_ids": styles, "feature": kind}}


def query_plan(registry):
    for provider in registry["providers"]:
        if provider.get("named", True):
            for artist in registry["artists"]:
                if artist["enabled"]:
                    yield provider, "artist", artist["ru" if provider["lang"] == "ru" else "original"], artist["id"]
                    if artist["tier"] == "main" and provider["lang"] == "ru":
                        alternate = next((a for a in artist["aliases"] if sources.CYRILLIC.search(a)), None)
                        if alternate:
                            yield provider, "artist", alternate, artist["id"]
        for term in provider.get("discovery", []):
            yield provider, "discovery", term, None


def date_from_wp(post):
    value = post.get("date_gmt")
    if not value or value.startswith("0000"):
        return None
    try:
        return sources.timestamp(value)
    except ValueError:
        return None


def wordpress(provider, term):
    arguments = urlencode({"search": term, "per_page": 8, "_fields": "id,date_gmt,title,link,excerpt,content"})
    data = sources.get_json(f"{provider['endpoint']}?{arguments}", provider["domain"])
    if not isinstance(data, list):
        raise ValueError("Unexpected WordPress response")
    return data


def editorial_item(post, provider, intent, term, target, registry):
    content = post.get("content") or {}
    if content.get("protected") or (post.get("excerpt") or {}).get("protected"):
        return None
    url = post.get("link", "")
    title = sources.clean((post.get("title") or {}).get("rendered"))
    if not title or NOISE.search(title) or not sources.allowed(url, [provider["domain"]]):
        return None
    excerpt = sources.clean((post.get("excerpt") or {}).get("rendered"))
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", content.get("rendered", ""), re.I | re.S)
    body = " ".join(sources.clean(p) for p in paragraphs[:14])[:5000]
    evidence = f"{excerpt} {body}".strip()
    if len(evidence.split()) < 13:
        return None
    original_date = date_from_wp(post)
    if not original_date:
        return None
    lang = provider["lang"]
    styles = style_ids(title + " " + evidence, registry, lang)
    kind = feature_type(title, evidence)
    if not kind:
        return None
    matched = named_artist(title, excerpt or evidence, registry["artists"], target=target, url=url)
    if intent == "artist" and not matched:
        return None
    if matched:
        info = {"stream": "priority", "artist": matched["ru"], "artist_id": matched["id"], "tier": matched["tier"],
                "historical": matched["tier"] == "historical" or matched.get("historical", False)}
    else:
        found = discovery_name(title, evidence)
        if not found or not styles or any(contains(normalize(found), normalize(name))
                                          for artist in registry["artists"] for name in names(artist)):
            return None
        info = {"stream": "discovery", "artist": found, "artist_id": normalize(found), "tier": "discovery"}
    info.update(style_ids=styles, feature=kind)
    author_names = names(matched) if matched else [info["artist"]]
    title_subject = any(contains(normalize(title), normalize(name)) for name in author_names)
    facts = factual_excerpt([excerpt, *map(sources.clean, paragraphs[:14])], author_names, title_subject) if lang == "en" else None
    return sources.entry(provider, url=url, title=title, text=excerpt or evidence[:480],
                         date=original_date, key=str(post["id"]), kind="article", origin=provider["endpoint"],
                         extra={"artist_radar": info, "language": lang, "access": "unknown", "fact_excerpt": facts,
                                "search_matches": [{"query": term, "mode": intent, "artist": target}]})


def youtube_query(provider, artist, key, registry, now):
    term = " | ".join(dict.fromkeys([artist["ru"], artist["original"]]))
    args = urlencode({"q": term, "type": "video", "part": "snippet", "maxResults": 8, "order": "date",
                      "publishedAfter": (now - dt.timedelta(days=9)).astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
                      "key": key})
    response = sources.get_json(f"https://www.googleapis.com/youtube/v3/search?{args}", "googleapis.com")
    items = []
    for result in response["items"]:
        snippet, video_id = result["snippet"], result["id"]["videoId"]
        title, evidence = sources.clean(snippet["title"]), snippet.get("description", "")
        if not named_artist(title, evidence, registry["artists"], target=artist["id"]):
            continue
        lang = "ru" if sources.CYRILLIC.search(evidence) else "en"
        video = sources.entry(provider, url=f"https://www.youtube.com/watch?v={video_id}", title=title, text=evidence,
                              date=sources.timestamp(snippet["publishedAt"]), key=video_id, kind="video",
                              origin="https://www.youtube.com/ · YouTube Data API search.list",
                              extra={"language": lang, "search_matches": [
                                  {"query": term, "mode": "artist", "artist": artist["id"]}]})
        items.append(annotate(video, {"artist_radar": registry}, provider))
    return term, len(response["items"]), [item for item in items if item.get("artist_radar")]


def search(config, now=None, covered=()):
    registry = config["artist_radar"]
    now = now or dt.datetime.now(sources.MOSCOW)
    artists = {artist["id"]: artist for artist in registry["artists"]}
    rotating = [artist["id"] for artist in registry["artists"] if artist["enabled"] and artist["tier"] != "main"]
    current = {artist["id"] for artist in artists.values() if artist["tier"] == "main"}
    current.update(rotating[now.date().toordinal() % 3::3])
    tasks = [task for task in query_plan(registry) if task[1] == "discovery" or task[3] in current]
    key = os.getenv("JOURNAL_YOUTUBE_API_KEY")
    if key:
        yt = next(s for s in config["sources"] if s["id"] == "artist-youtube")
        active = [a["id"] for a in registry["artists"] if a["enabled"] and a["id"] not in covered]
        if active:
            start = (now.date().toordinal() * 4) % len(active)
            tasks += [(yt, "youtube", "", artist) for artist in (active[start:] + active[:start])[:4]]
    def execute(task):
        provider, intent, term, target = task
        audit = {"source": provider["id"], "name": provider["name"], "query": term, "mode": intent,
                 "artist": target, "endpoint": provider.get("endpoint", "YouTube Data API search.list")}
        try:
            if intent == "youtube":
                audit["query"], total, items = youtube_query(provider, artists[target], key, registry, now)
            else:
                posts = wordpress(provider, term)
                total = len(posts)
                items = [item for p in posts if (item := editorial_item(p, provider, intent, term, target, registry))]
            return items, {**audit, "status": "ok", "total": total, "count": len(items),
                           "matched_urls": [item["url"] for item in items]}
        except (OSError, ValueError, TypeError, KeyError) as error:
            status = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
            return [], {**audit, "status": status, "total": 0, "count": 0, "matched_urls": []}

    with ThreadPoolExecutor(max_workers=10) as pool:
        batches = list(pool.map(execute, tasks))
    merged = {}
    for batch, _ in batches:
        for item in batch:
            url = sources.canonical(item["url"])
            if url in merged:
                old = merged[url]
                if old["artist_radar"]["stream"] == "priority":
                    old["search_matches"].extend(item["search_matches"])
                    continue
                item["search_matches"] = [*old["search_matches"], *item["search_matches"]]
            merged[url] = item
    items = list(merged.values())
    if key:
        sources.youtube_details(items, key)
    audit = [a for _, a in batches]
    if not key:
        audit.append({"source": "artist-youtube", "name": "YouTube Data API", "status": "Нужен ключ API", "count": 0})
    elif covered:
        audit.append({"source": "artist-youtube", "name": "YouTube Atom", "status": "ok", "count": 0,
                      "covered_artists": sorted(covered)})
    return items, audit


def factual_excerpt(paragraphs, aliases, title_subject=False):
    subjects = {normalize(name.split()[-1]) for name in aliases if name.strip() and len(name.split()[-1]) > 2}
    matches = []
    for para in paragraphs:
        para = re.sub(r"\s+([,.!?])", r"\1", sources.CONTACT.sub("", para))
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", para)
        for index, single in enumerate(sentences):
            for sentence in [single, " ".join(sentences[index:index + 2])]:
                facts = set(term.casefold() for term in FACT.findall(sentence))
                anchored = any(contains(normalize(sentence), name) for name in subjects) or (
                    title_subject and re.match(r"The (?:[A-Z][a-z]+ )?(?:artist|painter|printmaker)\b", sentence))
                if (12 <= len(sentence.split()) and len(sentence) <= 310 and sentence.endswith((".", "!", "?"))
                        and facts and anchored and not NOISE.search(sentence)):
                    techniques = {term.casefold() for term in TECHNIQUE.findall(sentence)}
                    matches.append((len(techniques), len(facts), -len(sentence), sentence))
    return max(matches)[-1] if matches else None


def translate_articles(items, config, now, previous=()):
    catalog = {source["id"]: source for source in config["sources"]}
    cache = {item["fact_excerpt"]: item["ru_editorial"] for item in previous
             if item.get("fact_excerpt") and item.get("ru_editorial")}
    pending, spent = {}, 0
    ordered = sorted(items, key=lambda item: (item.get("published") or "", item.get("feed_id") or ""), reverse=True)
    for item in ordered:
        info = item.get("artist_radar") or {}
        published = item.get("published")
        evidence = item["evidence"]
        video = item["kind"] == "video"
        if (not info.get("artist") or not published or item.get("language", catalog[item["source"]]["lang"]) != "en"
                or video and not (VISUAL_VIDEO.search(evidence) or RU_SUBTITLES.search(evidence))):
            continue
        date = dt.datetime.fromisoformat(published)
        if date > now or (now.date() - date.astimezone(sources.MOSCOW).date()).days > config["news_days"]:
            continue
        fact = item.get("fact_excerpt") or factual_excerpt([evidence], [info["artist"]])
        if not fact:
            continue
        item["fact_excerpt"] = fact
        if fact in cache:
            item["ru_editorial"] = cache[fact]
        elif fact in pending:
            pending[fact].append(item)
        elif (size := len(fact.encode())) <= 480 and spent + size <= TRANSLATION_BUDGET:
            pending[fact] = [item]
            spent += size

    def one(fact):
        try:
            result = sources.clean(translate.ask(fact))
        except (OSError, ValueError, HTTPException):
            return None
        if (not 8 <= len(result.split()) or len(result) > min(350, len(fact) * 2)
                or NOISE.search(result) or not sources.CYRILLIC.search(result)
                or set(re.findall(r"\d+", fact)) != set(re.findall(r"\d+", result))):
            return None
        return result.rstrip(" .!?") + "."

    accepted = 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        for (fact, batch), result in zip(pending.items(), pool.map(one, pending)):
            if result:
                for item in batch:
                    item["ru_editorial"] = result
                accepted += 1
    return {"source": "artist-translation", "name": "Русская редактура · MyMemory", "status": "ok" if accepted else
            "Нет новых подтверждённых переводов", "count": accepted, "attempted": len(pending), "bytes": spent}


def russian_facts(raw, registry):
    info = raw.get("artist_radar") or {}
    artist, feature = info.get("artist"), info.get("feature")
    summary = raw.get("ru_editorial")
    if not artist or not feature or not summary or not raw.get("fact_excerpt") or not sources.CYRILLIC.search(summary):
        return None
    video = raw["kind"] == "video"
    visual = video and bool(VISUAL_VIDEO.search(raw["evidence"]))
    subtitles = video and bool(RU_SUBTITLES.search(raw["evidence"]))
    if video and not (visual or subtitles):
        return None
    labels = {"people": "Беседа", "works": "Работы", "studio": "В мастерской", "research": "Исследование",
              "exhibition": "Выставка", "technique": "Техника"}
    return {**raw, "title": f"{labels[feature]} · {artist}", "original_title": raw["title"],
            "summary": summary, "why": f"Факт из оригинала · {raw['fact_excerpt']}",
            "direction": {"works": "studio", "studio": "studio", "technique": "practice", "exhibition": "news",
                          "research": "news", "people": "people"}[feature],
            "access": raw.get("access", "unknown"), "lang": "en", "type": "news", "topics": [],
            "usefulness": 4.5, "originality": 4.5, "verified_visual": visual, "translation_checked": subtitles or not video,
            "translation": "ru_subtitles" if subtitles else "unknown" if video else "ru_text",
            "translation_source": "описание источника" if subtitles else ""}


def discoveries(items, registry, limit=10, verify=None, per_source=None):
    unique = {}
    counts = Counter()
    publishers = {p["id"]: p.get("publisher", p["id"]) for p in registry["providers"]}
    for item in sorted(items, key=lambda row: row.get("published") or "", reverse=True):
        info = item.get("artist_radar") or {}
        if info.get("stream") != "discovery" or info["artist_id"] in unique:
            continue
        if not is_person_label(normalize(info["artist"])):
            continue
        publisher = publishers.get(item["source"], item["source"])
        if per_source is not None and counts[publisher] >= per_source:
            continue
        if verify and not verify(item):
            continue
        unique[info["artist_id"]] = {"name": info["artist"], "id": info["artist_id"], "url": item["url"],
                                    "published": item["published"], "source": item["source"], "feature": info["feature"],
                                    "styles": [style["label"] for style in registry["styles"] if style["id"] in info["style_ids"]],
                                    "queries": item["search_matches"], "candidate": True,
                                    "fact_excerpt": item.get("fact_excerpt"), "ru_editorial": item.get("ru_editorial")}
        counts[publisher] += 1
        if len(unique) == limit:
            break
    return list(unique.values())


def save_search(output, items, queries, config, now):
    output.mkdir(parents=True, exist_ok=True)
    catalog = {p["id"]: p for p in config["sources"]}

    def verifies(item):
        try:
            return sources.check_url(item["url"], catalog[item["source"]]["domains"])
        except (OSError, ValueError):
            return False

    candidates = discoveries(items, config["artist_radar"], limit=100)
    input_by_url = {item["url"]: item for item in items}
    with ThreadPoolExecutor(max_workers=8) as pool:
        valid = list(pool.map(verifies, (input_by_url[row["url"]] for row in candidates)))
    verified = discoveries([input_by_url[row["url"]] for row, works in zip(candidates, valid) if works],
                           config["artist_radar"], limit=10, per_source=4)
    report = {"collected_at": now.isoformat(), "queries": queries, "items": items,
              "discoveries": verified, "discovery_links_verified": True}
    (output / "artist-search.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    cards = []
    for person in report["discoveries"]:
        styles = ", ".join(person["styles"])
        cards.append(f"<article><h2>{escape(person['name'])}</h2><p>{escape(styles)}</p>"
                     f'<p><a href="{escape(person["url"], quote=True)}">{escape(catalog[person["source"]]["name"])}</a>'
                     f" · {escape(person['published'][:10])}</p></article>")
    stats = Counter(q["status"] for q in queries)
    ordered = sorted((q for q in queries if q.get("query")), key=lambda q: (not q["count"], q["name"], q["query"]))
    lines = []
    for q in ordered:
        matched = " · ".join(f'<a href="{escape(url, quote=True)}">Материал {i}</a>'
                             for i, url in enumerate(q.get("matched_urls", []), 1))
        lines.append(f"<li><b>{escape(q['query'])}</b> · {escape(q['name'])} · {escape(q['status'])} · "
                     f"ответов {q['total']}, подтверждено {q['count']}" + (f"<br>{matched}" if matched else "") + "</li>")
    page = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Artist Radar</title><style>body{max-width:790px;margin:36px auto;padding:0 16px;font:16px/1.6 system-ui;background:#f8f5ec;color:#27251d}
a{color:#7b361f}article{border-bottom:1px solid #d9d0bb;padding:14px 0}h1,h2{font-family:Georgia,serif}li{margin:9px 0}</style>
<h1>Художники в поле зрения</h1><a href="artist-search.json">Полные результаты запросов и метаданные</a> ·
<a href="registry.html">Реестр художников и источники</a>"""
    page += f"<p>Запросов {len(ordered)} · ответили {stats['ok']} · подтверждённых страниц {len(items)}</p>"
    page += "".join(cards) + "<details><summary>Запросы по именам и техникам</summary><ul>" + "".join(lines) + "</ul></details>"
    (output / "index.html").write_text(page, encoding="utf-8")
    registry = config["artist_radar"]
    status = {"reference": "Проверенный источник", "manual": "Подтверждён, ручная проверка",
              "needs_api": "Требуется API", "atom": "YouTube Atom", "channel_api": "Публичные метаданные канала",
              "unconfirmed_account": "Принадлежность аккаунта не подтверждена",
              "unconfirmed_identity": "Личность требует сверки"}
    tiers = {"main": "Основной", "secondary": "Дополнительный", "historical": "Исторический"}
    articles = []
    for artist in registry["artists"]:
        aliases = " · ".join([artist["original"], *artist["aliases"]])
        links = []
        for profile in artist["profiles"]:
            url = escape(profile["url"], quote=True)
            links.append(f'<li><a href="{url}">{url}</a> · {escape(status.get(profile["state"], profile["state"]))}</li>')
        note = escape(artist.get("note", ""))
        mode = "Поиск включён" if artist["enabled"] else "Ожидает подтверждения личности"
        articles.append(f"<article><h2>{escape(artist['ru'])}</h2><p>{escape(aliases)}<br>"
                        f"{tiers[artist['tier']]} · {mode}</p><p>{note}</p><ul>{''.join(links)}</ul></article>")
    registry_page = page.split("<h1>", 1)[0] + '<h1>Реестр Artist Radar</h1><a href="index.html">Результаты поиска</a>'
    (output / "registry.html").write_text(registry_page + "".join(articles), encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("journal-review/artist-search"))
    args = parser.parse_args()
    config = configure(sources.read(sources.ROOT / "sources.json"))
    now = dt.datetime.now(sources.MOSCOW)
    items, queries = search(config, now)
    save_search(args.out, items, queries, config, now)
    print(f"{len(items)} материалов · {args.out / 'index.html'}")


if __name__ == "__main__":
    main()
