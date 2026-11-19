"""Search official catalogues using subjects documented by the original publisher."""

import json
import re
from functools import lru_cache
from urllib.parse import unquote, urlencode, urljoin

from radar import artist_pages, artist_radar, editorial_sources as sources, journal_articles


COMMONS = "commons.wikimedia.org"
CHICAGO = "api.artic.edu"
WIKIDATA = "www.wikidata.org"
AGENT = "ArtRadarJournal/1.1 (https://github.com/YuriyKhudov/art-radar)"
NAME = re.compile(r"\b[А-ЯЁ][а-яё-]{3,}(?:\s+(?:ван|де|да|фон|дель))?\s+[А-ЯЁ][а-яё-]{2,}\b")
QUOTED = re.compile(r"«([^»\n]{5,95})»|„([^“\n]{5,95})“")
MEDIUM = re.compile(r"\b(?:oil|paintings?|canvas|watercolou?r|drawings?|etching|lithograph|гравюр\w*|живопис\w*|холст|масло|акварел\w*|пастел\w*|гуаш\w*)\b", re.I)
REJECT = re.compile(r"\b(?:detail|cropped|postcard|poster|sketchbook page|building|facade|flag|map|signature|logo|афиша|фасад|открытка|почтовая марка|логотип|фрагмент)\b", re.I)
ARTIST_ALIASES = {
    "суриков": "Vasily Surikov", "серов": "Valentin Serov", "врубел": "Mikhail Vrubel",
    "репин": "Ilya Repin", "левитан": "Isaac Levitan", "шишкин": "Ivan Shishkin",
    "куинджи": "Arkhip Kuindzhi", "айвазовск": "Ivan Aivazovsky", "перов": "Vasily Perov",
    "ван гог": "Vincent van Gogh", "моне": "Claude Monet", "мане": "Édouard Manet",
    "гоген": "Paul Gauguin", "сезанн": "Paul Cézanne", "дега": "Edgar Degas",
    "ренуар": "Pierre-Auguste Renoir", "матисс": "Henri Matisse", "рембрандт": "Rembrandt",
    "вермеер": "Johannes Vermeer", "кандинск": "Wassily Kandinsky", "малевич": "Kazimir Malevich",
    "фальк": "Robert Falk", "рубенс": "Peter Paul Rubens", "веласкес": "Diego Velázquez",
    "пикассо": "Pablo Picasso", "климт": "Gustav Klimt", "босх": "Hieronymus Bosch",
    "брейгел": "Pieter Bruegel the Elder", "ван эйк": "Jan van Eyck", "да винчи": "Leonardo da Vinci",
    "рафаэл": "Raphael", "джотто": "Giotto", "боттичелли": "Sandro Botticelli", "герик": "Théodore Géricault",
}
LICENSES = {"cc-zero": "CC0", "cc-by-4.0": "CC BY 4.0", "cc-by-sa-4.0": "CC BY-SA 4.0",
            "pd": "Public Domain"}
LICENSE_URLS = {"cc-zero": "https://creativecommons.org/publicdomain/zero/1.0/",
                "cc-by-4.0": "https://creativecommons.org/licenses/by/4.0/",
                "cc-by-sa-4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
                "pd": "https://commons.wikimedia.org/wiki/Commons:Public_domain"}


def display(value):
    return sources.clean(value if isinstance(value, str) else "")


def equivalent(text, name):
    normalized = artist_radar.normalize(text)
    return bool(name and artist_radar.contains(normalized, artist_radar.normalize(name)))


def same_artist(credit, authors):
    return (any(equivalent(credit, name) for name in authors)
            or any(name in authors and re.search(r"(?<!\w)" + stem + r"[а-яё]{0,5}(?!\w)", credit, re.I)
                   for stem, name in ARTIST_ALIASES.items()))


def search_terms(card, topic, page, config):
    item = card.item
    title = item.get("original_title") or item["title"]
    evidence = item.get("evidence", "")
    custom = config.get("presentation", {}).get("cards", {}).get(card.key, {})
    info = item.get("artist_radar") or {}
    authors = [name for name in (custom.get("feature_artist"), info.get("artist")) if name]
    for artist in config.get("artist_radar", {}).get("artists", []):
        if artist["ru"] in authors:
            authors.append(artist["original"])
    authors.extend(NAME.findall(title))
    named_works = [a or b for a, b in QUOTED.findall(title)]
    contexts = [title, evidence]
    if page:
        contexts.extend(s for s in page.headings[1:14] if 5 < len(s) < 100)
        for node in artist_pages.records(page.structure):
            typ = node.get("@type")
            if typ == "VisualArtwork" and isinstance(node.get("name"), str):
                named_works.append(node["name"])
            if typ in ("VisualArtwork", "ExhibitionEvent", "Event"):
                for field in ("creator", "performer", "contributor"):
                    nodes = node.get(field) or []
                    authors.extend(n["name"] for n in (nodes if isinstance(nodes, list) else [nodes])
                                   if isinstance(n, dict) and isinstance(n.get("name"), str))
    for context in contexts[:10]:
        named_works.extend(a or b for a, b in QUOTED.findall(context))
    if topic in {"exhibition", "fair", "photography"}:
        participants = re.findall(r"(?:работы|произведения|участники:|фотографии|картины)\s+([^.!?]{4,130})", evidence, re.I)
        for participant in participants:
            authors.extend(NAME.findall(participant)[:3])
    for context in (title, *authors):
        for stem, original in ARTIST_ALIASES.items():
            if original not in authors and re.search(r"(?<!\w)" + stem + r"[а-яё]{0,5}(?!\w)", context, re.I):
                authors.insert(0, original)
    authors = list(dict.fromkeys(authors))[:6]
    works = [work for work in dict.fromkeys(named_works) if len(work.split()) >= 2 or len(work) >= 7][:4]
    # Keep named works ahead of subject/creator fallback, and leave quotes out of API syntax.
    terms = [(work, "work") for work in works[:2]]
    terms += [(name, "artist") for name in authors[:3]]
    if topic in {"event", "fair", "exhibition", "photography", "technique"}:
        terms.append((title[:110], "topic"))
    return [(cleaned, purpose) for term, purpose in terms if (cleaned := re.sub(r'[^\w -]', " ", term).strip())], authors, works


@lru_cache(maxsize=512)
def commons_query(term, file=False):
    args = {"action": "query", "format": "json", "formatversion": 2, "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600, "iiextmetadatalanguage": "ru"}
    if file:
        args["titles"] = term
    else:
        args.update(generator="search", gsrnamespace=6, gsrlimit=12, gsrsearch='"' + term + '" filetype:bitmap')
    body, _ = sources.request(f"https://{COMMONS}/w/api.php?{urlencode(args)}", [COMMONS], 2_000_001, user_agent=AGENT)
    data = json.loads(body)
    if "error" in data:
        raise ValueError("Wikimedia API")
    return data.get("query", {}).get("pages", [])


@lru_cache(maxsize=128)
def wikidata_work(title):
    def api(**parameters):
        url = f"https://{WIKIDATA}/w/api.php?{urlencode(dict(format='json', **parameters))}"
        return json.loads(sources.request(url, ["wikidata.org"], 800_001, user_agent=AGENT)[0])

    query = api(action="wbsearchentities", search=title, language="ru", uselang="ru", limit=5)
    ids = [r["id"] for r in query.get("search", []) if re.fullmatch(r"Q\d+", r.get("id", ""))
           and artist_radar.normalize(r.get("label", "")) == artist_radar.normalize(title)]
    if not ids:
        return [], []
    entities = api(action="wbgetentities", ids="|".join(ids), props="claims|labels", languages="ru|en")["entities"]
    aliases, files = [], []
    for item in entities.values():
        claims = item.get("claims", {})
        types = {row.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id") for row in claims.get("P31", [])}
        if not types & {"Q3305213", "Q838948", "Q18761202", "Q93184", "Q125191", "Q11060274"} or not claims.get("P170"):
            continue
        if label := item.get("labels", {}).get("en", {}).get("value"):
            aliases.append(label)
        for row in claims.get("P18", [])[:1]:
            file = row.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(file, str):
                files.append("File:" + file)
    return aliases, files


def commons_asset(row, topic, authors, works, term, purpose):
    image = next(iter(row.get("imageinfo", [])), {})
    meta = image.get("extmetadata", {})

    def field(key):
        return display(meta.get(key, {}).get("value"))

    key, creator, caption = field("License"), field("Artist"), field("ImageDescription")
    filename = row.get("title", "").removeprefix("File:")
    file_url = image.get("thumburl") or image.get("url", "")
    origin = image.get("descriptionurl", "")
    if (key not in LICENSES or not creator or key == "pd" and field("Copyrighted") != "False"
            or field("Restrictions") or image.get("mime") not in {"image/jpeg", "image/png", "image/webp"}
            or not sources.allowed(file_url, ["upload.wikimedia.org", "thumb.wikimedia.org"])
            or not sources.allowed(origin, [COMMONS]) or REJECT.search(filename)):
        return None
    category = field("Categories")
    object_name = re.split(r"(?:title|label) QS:", field("ObjectName"), maxsplit=1)[0].strip() or filename.rsplit(".", 1)[0]
    identity = " ".join((object_name, filename, caption[:400]))
    matched_creator = same_artist(creator, authors)
    matched_work = any(equivalent(identity, work) for work in works)
    matched_topic = equivalent(identity, term) and purpose == "topic"
    role = None
    if topic == "photography" and matched_creator and (matched_work or re.search(
            r"photograph(?:ic series|ic print| from the series)|фотографическ\w+ сери|фотография из серии", caption, re.I)):
        role = "art_photograph"
    elif topic in {"event", "exhibition", "fair"} and matched_topic and re.search(
            r"installation view|exhibition view|вид экспозиции", caption, re.I):
        role = "exhibition_view"
    elif topic == "technique" and matched_topic and re.search(
            r"painting in progress|work in progress|процесс работы|процесс рисования", caption, re.I):
        role = "creative_process"
    elif topic == "technique" and matched_topic and re.search(r"finished (?:painting|drawing)|результат урока", caption, re.I):
        role = "technique_result"
    elif topic in {"artist", "art", "fair", "exhibition", "architecture"} and (matched_work or matched_creator) and (
            MEDIUM.search(" ".join((caption, category, filename))) or "PD-Art (" in category) and (
            matched_creator or purpose in {"file", "work"}) and not REJECT.search(caption[:120]):
        role = "artwork"
    if not role:
        return None
    return {"url": file_url, "source": origin, "credit": f"{creator[:105]} · {object_name[:105]}",
            "license": LICENSES[key], "license_url": LICENSE_URLS[key], "role": role,
            "registry": "Wikimedia Commons", "work": object_name, "caption": caption[:150]}


@lru_cache(maxsize=128)
def chicago_query(term):
    args = {"q": term, "query[term][is_public_domain]": "true", "limit": 12,
            "fields": "id,title,artist_title,image_id,is_public_domain,artwork_type_title,credit_line"}
    data, _ = sources.request(f"https://{CHICAGO}/api/v1/artworks/search?{urlencode(args)}", [CHICAGO],
                              400_001, user_agent=AGENT, headers={"AIC-User-Agent": AGENT})
    return json.loads(data)["data"]


def chicago_asset(record, authors, works):
    creator = record.get("artist_title") or ""
    work = record.get("title") or ""
    image = record.get("image_id") or ""
    if (record.get("is_public_domain") is not True or not re.fullmatch(r"[0-9a-f-]{36}", image)
            or not isinstance(record.get("id"), int)
            or not same_artist(creator, authors)
            or not (record.get("artwork_type_title") in {"Painting", "Drawing and Watercolor", "Print"}
                    or any(equivalent(work, name) for name in works))):
        return None
    return {"url": f"https://www.artic.edu/iiif/2/{image}/full/1686,/0/default.jpg",
            "source": f"https://www.artic.edu/artworks/{record['id']}", "work": work,
            "credit": " · ".join(filter(None, (creator, work, "Art Institute of Chicago", record.get("credit_line"))))[:250],
            "license": "CC0", "license_url": LICENSE_URLS["cc-zero"], "role": "artwork",
            "registry": "Art Institute of Chicago"}


def search(card, topic, page, config, read_page=journal_articles.public_page, robots=None):
    terms, authors, works = search_terms(card, topic, page, config)
    results, trace, seen = [], [], set()
    title = card.item.get("original_title") or card.item["title"]
    named_subject = bool(re.search(r"разбор одного шедевра", title, re.I) and works)
    for href in (page.links if page else []):
        if sources.allowed(href, [COMMONS]) and (marker := re.search(r"/wiki/(File:[^?#]+)", unquote(href))):
            terms.insert(0, (marker[1].replace("_", " "), "file"))
    if named_subject:
        try:
            aliases, files = wikidata_work(works[0])
            trace.append({"provider": "Wikidata", "query": works[0], "basis": "work", "matched": len(files)})
            works += aliases
            terms = [(file, "file") for file in files[:2]] + [(name, "work") for name in aliases[:1]] + terms
        except (OSError, KeyError, TypeError, ValueError):
            trace.append({"provider": "Wikidata", "query": works[0], "status": "unavailable"})
    for term, purpose in terms[:5]:
        try:
            records = commons_query(term, file=purpose == "file")
            matches = [asset for row in records if (asset := commons_asset(row, topic, authors, works, term, purpose))]
            if named_subject:
                matches = [asset for asset in matches if any(equivalent(asset["work"], work) for work in works)]
            trace.append({"provider": "Wikimedia Commons", "query": term, "basis": purpose,
                          "found": len(records), "matched": len(matches)})
            for asset in matches:
                if asset["source"] not in seen:
                    seen.add(asset["source"])
                    results.append(asset)
        except (OSError, ValueError, TypeError):
            trace.append({"provider": "Wikimedia Commons", "query": term, "basis": purpose, "status": "unavailable"})
        if len(results) >= 5:
            break
    if not named_subject and topic in {"art", "artist", "exhibition", "fair"} and len(results) < 5:
        for term in [name for name, purpose in terms if purpose == "artist" and not sources.CYRILLIC.search(name)][:2]:
            try:
                records = chicago_query(term)
                matches = [asset for row in records if (asset := chicago_asset(row, authors, works))]
                results.extend(matches[:5])
                trace.append({"provider": "Art Institute of Chicago", "query": term, "basis": "artist",
                              "found": len(records), "matched": len(matches)})
            except (OSError, KeyError, ValueError, TypeError):
                trace.append({"provider": "Art Institute of Chicago", "query": term, "status": "unavailable"})
    # Official archives linked by the publisher still need individual ImageObject grants.
    from radar.journal_visuals import publisher_images, role_for
    domains = config.get("illustration_archive_domains", [])
    links = [urljoin(card.item["url"], href) for href in (page.links if page else [])]
    original_domain = card.source.get("domains", [])
    for url in list(dict.fromkeys(url for url in links if sources.allowed(url, domains)
                                 and not sources.allowed(url, original_domain)))[:2]:
        try:
            linked = read_page(url, domains, robots if robots is not None else {})
            matches = [asset for asset in publisher_images(linked[1], url, domains + config["image_domains"])
                       if role_for(asset, topic) and any(equivalent(asset["caption"], work) for work in works)] if linked else []
            results.extend({**asset, "registry": "Официальный архив"} for asset in matches)
            trace.append({"provider": "Официальный архив", "query": url, "matched": len(matches)})
        except (ImportError, OSError, ValueError):
            trace.append({"provider": "Официальный архив", "query": url, "status": "unavailable"})
    return results[:10], trace
