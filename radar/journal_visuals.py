"""Select usable, licensed art before an Art Radar item can enter a release."""

import re
from urllib.parse import urljoin

from radar import artist_pages, editorial, editorial_sources as sources, journal_articles, journal_illustrations


POLICY = 2
ROLES = {
    "architecture": ("architecture", "venue", "artwork", "exhibition_view"),
    "photography": ("art_photograph",),
    "artist": ("artwork", "art_photograph"),
    "fair": ("artwork", "art_photograph"),
    "exhibition": ("artwork", "art_photograph"),
    "event": ("exhibition_view",),
    "technique": ("technique_steps", "creative_process", "technique_result"),
    "video": ("video_frame", "video_preview", "creative_process"),
    "art": ("artwork", "art_photograph", "exhibition_view", "creative_process", "technique_result"),
}
ARCHITECTURE = re.compile(r"архитектур|градостроитель|архитектор|зодчеств|\barchitecture\b|\barchitects?\b", re.I)
EVENT = re.compile(r"выставк|биеннале|экспозиц|ярмарк|фестивал|открыти|\b(?:fair|exhibition|biennial)\b", re.I)
PHOTOGRAPHY = re.compile(r"фотобиеннале|фотовыставк|фотоискусств|фотографи|фотограф[а-я]+ |photograph|photo biennial", re.I)
INCIDENTAL = re.compile(r"фасад|экстерьер|\bздани[еяю]\b|\bулиц[аеуы]\b|логотип|эмблем[ауы]|афиша|"
                        r"портретное фото|фото художни[кц]|(?:музей|галерея).{0,24}вид снаружи|"
                        r"\b(?:logo|facade|façade|exterior|street view|museum building|headshot|selfie)\b", re.I)
BRANDING = re.compile(r"логотип|эмблем[ауы]|\b(?:logo|headshot|selfie)\b", re.I)
ROLE_HINTS = (
    ("exhibition_view", r"вид экспозиции|монтаж экспозиции|выставка в зале|экспозиция выставки|exhibition view|installation view"),
    ("technique_steps", r"от эскиза до |процесс и результат|пошагов\w+.{0,25}результат|step.by.step.{0,20}finished"),
    ("creative_process", r"процесс (?:работы|рисования|написания|создания)|работает над|на пленэре|painting in progress|work in progress"),
    ("technique_result", r"готовая (?:картина|работа)|результат (?:урока|работы)|finished (?:painting|artwork|drawing)"),
    ("video_frame", r"кадр из (?:видео|фильма|ролика)|still from|video frame"),
    ("art_photograph", r"фотографическая (?:серия|работа)|фотография из серии|серия (?:аналоговых |чёрно-белых )?фотографий|photographic print|photographic series|photograph from the series"),
    ("artwork", r"холст,? масло|масло на холсте|произведение художник|живописн\w+ работа|акварель на бумаге|"
                r"\b(?:офорт|эстамп|литография|гравюра)\b|oil on canvas|watercolou?r on paper|original artwork"),
    ("architecture", r"архитектурн\w+ проект|фасад|здание|facade|façade|building|architecture"),
)
ROLE_HINTS = [(role, re.compile(pattern, re.I)) for role, pattern in ROLE_HINTS]
OPEN_LICENSES = {"https://creativecommons.org/licenses/by/4.0": "CC BY 4.0",
                 "https://creativecommons.org/licenses/by-sa/4.0": "CC BY-SA 4.0",
                 "https://creativecommons.org/publicdomain/zero/1.0": "CC0",
                 "https://creativecommons.org/publicdomain/mark/1.0": "Public Domain"}


def video_permalink(url):
    try:
        url = sources.canonical(url)
    except ValueError:
        return False
    return bool(re.fullmatch(r"https://(?:youtube\.com/watch\?v=[\w-]{11}|rutube\.ru/video/[a-f0-9]{32}|"
                             r"(?:vkvideo\.ru|vk\.com)/video-?\d+_\d+)(?:[?&].*)?", url, re.ASCII))


def subject(card, custom):
    title = card.item.get("original_title") or card.item["title"]
    info = card.item.get("artist_radar") or {}
    if PHOTOGRAPHY.search(title) and card.item["kind"] != "video":
        return "photography"
    if ARCHITECTURE.search(title) and (not EVENT.search(title) or re.search(
            r"(?:выставк\w+|экспозиц\w+)\s+(?:об? |проектов )?архитектур|архитектурн\w+ выставк", title, re.I)):
        return "architecture"
    if card.direction == "practice" or info.get("feature") == "technique":
        return "technique"
    if card.item["kind"] == "video":
        return "video"
    if re.search(r"ярмарк|\bfair\b", title, re.I):
        return "fair"
    if custom.get("feature_artist"):
        return "artist"
    if re.search(r"выставк|экспозиц|\bexhibition\b", title, re.I):
        return "exhibition"
    if EVENT.search(title):
        return "event"
    if (info.get("artist") and info.get("feature") in {"works", "people", "studio"}
            or re.search(r"творчество|портрет художника|картины художника|artist profile", title, re.I)):
        return "artist"
    return "art"


def role_for(asset, topic):
    label = " ".join(str(asset.get(key) or "") for key in ("caption", "credit", "name"))
    if BRANDING.search(label):
        return None
    role = asset.get("role")
    if not role:
        role = next((kind for kind, pattern in ROLE_HINTS if pattern.search(label)), None)
    if topic != "architecture" and INCIDENTAL.search(label) and not (
            role in {"art_photograph", "artwork"} and any(
                kind == role and pattern.search(label) for kind, pattern in ROLE_HINTS)):
        return None
    return role if role in ROLES[topic] else None


def schema_types(node):
    types = node.get("@type", [])
    if isinstance(types, str):
        types = [types]
    return {kind.rsplit("/", 1)[-1] for kind in types if isinstance(kind, str)} if isinstance(types, list) else set()


def names(value):
    values = value if isinstance(value, list) else [value]
    result = []
    for node in values:
        name = node.get("name") if isinstance(node, dict) else node
        if isinstance(name, str) and name:
            result.append(sources.clean(name))
    return result


def publisher_images(page, page_url, domains):
    """Use only image-level grants attached to this publisher's Article or VideoObject."""
    nodes = list(artist_pages.records(page.structure))
    refs = {node["@id"]: node for node in nodes if isinstance(node.get("@id"), str)}
    articles = [node for node in nodes if schema_types(node) & (artist_pages.ARTICLE | {"VideoObject", "VisualArtwork"})]
    for article in articles:
        identity = article.get("url") or article.get("mainEntityOfPage")
        if isinstance(identity, dict):
            identity = identity.get("@id") or identity.get("url")
        if isinstance(identity, str):
            try:
                if sources.canonical(urljoin(page_url, identity)) != sources.canonical(page_url):
                    continue
            except ValueError:
                continue
        elif len(articles) != 1:
            continue
        images = article.get("image") or article.get("thumbnail") or []
        for raw in images if isinstance(images, list) else [images]:
            image = refs.get(raw, {}) if isinstance(raw, str) else raw if isinstance(raw, dict) else {}
            ref = image.get("@id")
            if isinstance(ref, str):
                image = {**refs.get(ref, {}), **image}
            if "ImageObject" not in schema_types(image):
                continue
            license_url = image.get("license")
            license_name = OPEN_LICENSES.get(license_url.rstrip("/")) if isinstance(license_url, str) else None
            file = image.get("contentUrl") or image.get("url")
            captions = names(image.get("caption")) + names(image.get("description")) + names(image.get("name"))
            credits = names(image.get("creditText")) or names(image.get("creator"))
            if not license_name or not credits or not captions or not isinstance(file, str):
                continue
            url = urljoin(page_url, file)
            if sources.allowed(url, domains):
                yield {"url": url, "source": page_url, "credit": " · ".join(dict.fromkeys([captions[0], *credits]))[:250],
                       "caption": " ".join(captions)[:550], "creator": names(image.get("creator")),
                       "license": license_name, "license_url": license_url}


class VisualChecks:
    def __init__(self, config, photos, video, pages=journal_articles.public_page, saved=None, offline=False):
        self.config = config
        self.photos = photos
        self.video = video
        self.pages = pages
        self.results = {} if saved is None else dict(saved)
        self.offline = offline
        self.replay = saved is not None
        self.robots = {}

    def check(self, card):
        if card.key not in self.results:
            if self.replay:
                raise ValueError(f"Для визуальной проверки нет записи {card.key}")
            self.results[card.key] = self.inspect(card)
        return self.results[card.key]

    def inspect(self, card):
        settings = self.config.get("presentation", {})
        custom = settings.get("cards", {}).get(card.key, {})
        topic = subject(card, custom)
        is_video = card.item["kind"] == "video"
        if not self.offline and is_video:
            licensed = self.video(custom.get("native_video"))
            if licensed:
                movie, _ = licensed
                return {"subject": topic, "images": [], "video": movie, "quality": 3}

        catalogue = editorial.visual_assets(card.item, self.config, all_candidates=True)
        candidates = [asset for asset in catalogue if role_for(asset, topic)]
        checked = self.verify_images(candidates, topic)
        search_log = []
        page = None
        permitted = self.config.get("article_domains", [])
        origin = [domain for domain in card.source["domains"] if domain in permitted]
        # The original source can also supply approved image metadata through JSON-LD.
        roles_found = {asset["role"] for asset in checked.values()}
        enough = len(checked) >= (3 if topic == "artist" else 1)
        if topic == "technique":
            enough = "technique_steps" in roles_found or {"creative_process", "technique_result"} <= roles_found
        if not self.offline and not enough and origin:
            try:
                result = self.pages(card.item["url"], origin, self.robots)
            except (ImportError, OSError, ValueError):
                result = None
            if result:
                _, page = result
                domains = list(dict.fromkeys([*origin, *self.config["image_domains"], *settings.get("visual_image_domains", [])]))
                artist = custom.get("feature_artist") or (card.item.get("artist_radar") or {}).get("artist")
                fresh = [asset for asset in publisher_images(page, card.item["url"], domains)
                         if role_for(asset, topic) and (topic != "artist" or not artist
                             or artist.casefold() in " ".join([asset["caption"], *asset["creator"]]).casefold())]
                for digest, asset in self.verify_images(fresh, topic).items():
                    checked.setdefault(digest, asset)
                candidates.extend(fresh)
        if not self.offline and not is_video and self.config.get("extended_illustrations") and not enough:
            found, search_log = journal_illustrations.search(card, topic, page, self.config, self.pages, self.robots)
            for digest, asset in self.verify_images(found, topic).items():
                checked.setdefault(digest, asset)
            candidates.extend(found)
        roles = ROLES[topic]
        ranked = sorted(checked.values(), key=lambda asset: roles.index(asset["role"]))
        if topic == "technique" and not is_video and not any(asset["role"] == "technique_steps" for asset in ranked):
            first = {}
            for asset in ranked:
                first.setdefault(asset["role"], asset)
            ranked = [*first.values(), *(asset for asset in ranked if asset not in first.values())]
        images = ranked[:1 if is_video else 5]
        review = {"subject": topic, "images": images, "video": None, "candidates": len(candidates), "quality": 0,
                  "search": search_log}
        if not images and is_video and self.config.get("video_link_fallback") and video_permalink(card.item["url"]):
            return {**review, "video_link": True}
        if not images:
            review["reason"] = ("Иллюстрация не прошла проверку прав, файла или разрешения" if candidates else
                                "Нет подходящего изображения с подтверждёнными правами")
        elif topic == "technique" and not is_video:
            evidence = {asset["role"] for asset in images}
            if "technique_steps" not in evidence and not {"creative_process", "technique_result"} <= evidence:
                review["reason"] = "Для техники не подтверждены изображения процесса и результата"
        if not review.get("reason"):
            review["quality"] = min(4, len(images) - 1) + int(any(min(a["width"], a["height"]) >= 960 for a in images))
        return review

    def verify_images(self, candidates, topic):
        checked = {}
        works = set()
        for asset in candidates[:12] if not self.offline else []:
            role = role_for(asset, topic)
            work = artist_pages.sources.clean(asset.get("work", "")).casefold()
            if not role or work and work in works:
                continue
            for validated, _ in self.photos([asset]):
                short, long = sorted((validated["width"], validated["height"]))
                if short >= 480 and long >= 700:
                    checked.setdefault(validated["sha256"], {**validated, "role": role})
                    if work:
                        works.add(work)
        return checked
