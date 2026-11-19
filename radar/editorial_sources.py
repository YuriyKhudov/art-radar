"""Collect the journal's feeds and reviewed archive without using the site's database."""

import datetime as dt
import email.utils
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "editorial"
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
CYRILLIC = re.compile(r"[а-яё]", re.I)
TRACKERS = {"fbclid", "gclid", "ysclid", "mc_cid", "mc_eid", "ref", "si"}
MOSCOW = dt.timezone(dt.timedelta(hours=3))
BOILERPLATE = re.compile(r"подпиш|\bподписывай|соцсет|\btelegram\b|ссылк[аи] на|плейлист|\bпромокод\b|"
                         r"поддерж[аи]|наш магазин|запись на курс|набор группы|купи|все ссылки|следите за нами|"
                         r"boosty|просмотров|поставьте лайк|заказать картину|курсы по рисунку|"
                         r"клуб с еженедельн|резидент галереи|формат[а-я]* сотрудничества|закрытый клуб|"
                         r"официальный адрес сайта|приходите в мою мастерскую|присоединяйтесь и рисуйте|"
                         r"если вы смотрите нас|приобрести запись|выбирайте свой курс|выбрать урок по ссылке|"
                         r"ближайшие живые встречи|для записи напишите|скажите.{0,5}хочу к|"
                         r"^\W*уроки художника|курсы для начинающих|(?:найти|смотрите) под постом|"
                         r"^\W*полный урок на сайте|^\W*интернет-магазин", re.I)
CONTACT = re.compile(r"(?:\+7|8)\s*\(?\d{3}\)?[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")
ADVERTISING = re.compile(r"промокод|скидк|распродаж|покупайте|купить курс|магазин|оплатить|"
                         r"до конца акции|запись на курс|мест[ао] огранич|приобрести|\bdiscount\b|shop now", re.I)
VK_LOCK = threading.Lock()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def canonical(url):
    """Remove tracking and unify the official forms of a video or article URL."""
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password or parts.port:
        raise ValueError("Ожидается прямая ссылка HTTPS")
    path = parts.path.rstrip("/") or "/"
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if host in {"youtu.be", "m.youtube.com", "youtube.com"}:
        video = (path.split("/")[-1] if host == "youtu.be" or path.startswith(("/shorts/", "/embed/"))
                 else dict(query).get("v"))
        if video and re.fullmatch(r"[\w-]{11}", video, re.ASCII):
            return f"https://youtube.com/watch?v={video}"
    query = sorted((k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in TRACKERS)
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(query), ""))


def allowed(url, domains):
    try:
        canonical(url)
        host = urllib.parse.urlsplit(url).hostname.removeprefix("www.")
        return host in domains
    except ValueError:
        return False


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, domains):
        self.domains = domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl, self.domains):
            raise ValueError("Переадресация за пределы источника")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(url, domains, limit):
    if not allowed(url, domains):
        raise ValueError("Домен отсутствует в настройках источника")
    opener = urllib.request.build_opener(Redirects(domains))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ArtRadarJournal/1.0"})
    try:
        with opener.open(req, timeout=10) as response:
            return response.read(limit), response.headers.get_content_type()
    except HTTPException:
        raise OSError("Неполный или некорректный HTTP-ответ") from None


def check_url(url, domains, image=False):
    content, kind = request(url, domains, 8192)
    if image:
        return bool(content) and kind.startswith("image/")
    body = content.lower()
    blocked = (b"just a moment", b"cf-chl-", b"video unavailable", b"verify you are human",
               b"this page could not be found")
    return bool(content) and not any(marker in body for marker in blocked)


def clean(value):
    value = html.unescape(value or "")
    value = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>", " ", value, flags=re.I | re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", value).split())


def description(text):
    raw = re.sub(r"</?(?:p|div|br|li)\b[^>]*>", "\n", text or "", flags=re.I)
    paragraphs = []
    for line in re.split(r"\n|(?<=[.!?])\s+(?=[А-ЯЁ«])", raw):
        line = clean(re.sub(r"(?:https?://|www\.|\bt\.me/)\S+|#\w+", "", CONTACT.sub("", line)))
        if len(line.split()) >= 5 and not BOILERPLATE.search(line):
            paragraphs.append(line)
    return " ".join(" ".join(paragraphs).split()[:65])[:480]


def timestamp(value, timezone=dt.timezone.utc):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        result = email.utils.parsedate_to_datetime(value)
    return (result if result.tzinfo else result.replace(tzinfo=timezone)).isoformat()


def duration(value):
    if isinstance(value, str) and (iso := re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)):
        hours, minutes, seconds = map(lambda n: int(n or 0), iso.groups())
        value = hours * 3600 + minutes * 60 + seconds
    try:
        total = int(value)
    except (TypeError, ValueError):
        return {}
    if total <= 0:
        return {}
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    label = f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"
    return {"seconds": total, "duration": label}


def advertising_dominates(text):
    text = re.sub(r"</?(?:p|div|br|li)\b[^>]*>", "\n", text or "", flags=re.I)
    segments = [clean(line) for line in re.split(r"\n|[.!?]\s+|[•▪]", text)]
    segments = [line for line in segments if len(line.split()) >= 3]
    ads = sum(len(line.split()) for line in segments if ADVERTISING.search(line))
    total = sum(len(line.split()) for line in segments)
    return bool(ads and ads * 2 >= total)


def entry(source, *, url, title, text, date, key, kind, origin, extra=None):
    evidence = description(text)
    return {"source": source["id"], "url": url, "feed_id": key, "title": clean(title),
            "evidence": evidence, "access_evidence": clean(CONTACT.sub("", text or ""))[:420],
            "promotion_dominant": advertising_dominates(text),
            "published": date, "kind": kind, "provenance": origin, **(extra or {})}


def feed_entries(raw, source):
    root = ET.fromstring(raw)
    if root.tag not in {"rss", ATOM + "feed"}:
        raise ValueError("Ответ не является RSS или Atom")
    for node in [*root.findall("./channel/item"), *root.findall(ATOM + "entry")][:75]:
        if node.tag == "item":
            link = (node.findtext("link") or "").strip()
            title, text = node.findtext("title"), node.findtext("description")
            date, key = node.findtext("pubDate", ""), node.findtext("guid")
            video, media = None, node.find(MEDIA + "content")
        else:
            link = next((element.get("href", "") for element in node.findall(ATOM + "link")
                         if element.get("rel", "alternate") == "alternate"), "")
            title = node.findtext(ATOM + "title")
            text = (node.findtext(ATOM + "summary") or node.findtext(ATOM + "content")
                    or node.findtext(f"{MEDIA}group/{MEDIA}description"))
            date = node.findtext(ATOM + "published", "")
            video = node.findtext(YT + "videoId")
            key = video or node.findtext(ATOM + "id")
            media = node.find(f"{MEDIA}group/{MEDIA}content")
        try:
            published = timestamp(date) if date else None
        except (TypeError, ValueError, OverflowError):
            published = None
        if title and allowed(link, source["domains"]):
            kind = "video" if video else source.get("kind", "article")
            extra = duration(media.get("duration")) if media is not None else {}
            yield entry(source, url=link, title=title, text=text, date=published, key=key, kind=kind,
                        origin=source.get("feed", "RSS / Atom"), extra=extra)


def get_json(url, domain):
    raw, mime = request(url, [domain], 4_000_001)
    if len(raw) > 4_000_000 or mime != "application/json":
        raise ValueError("Ответ источника не является допустимым JSON")
    return json.loads(raw)


def rutube_entries(data, source):
    for video in data["results"][:25]:
        key = video.get("id") or ""
        url = f"https://rutube.ru/video/{key}/"
        author = video.get("author") or {}
        if (not re.fullmatch(r"[a-f\d]{32}", key) or str(author.get("id")) != str(source["channel"])
                or not video.get("title") or not video.get("publication_ts") or video.get("is_paid")
                or video.get("is_club") or video.get("is_adult") or video.get("is_livestream") or video.get("is_hidden")):
            continue
        extras = {**duration(video.get("duration")), "access": "free"}
        if video.get("is_embeddable") is not False and video.get("is_embed_allowed") is not False:
            extras["player"] = f"https://rutube.ru/play/embed/{key}"
        yield entry(source, title=video["title"], url=url, text=video.get("description"),
                    date=timestamp(video["publication_ts"], MOSCOW), key=key, kind="video",
                    origin=f"RUTUBE · публичный JSON канала {source['channel']}", extra=extras)


def vk_entries(source, token):
    def vk(method, **query):
        params = urllib.parse.urlencode({**query, "v": "5.199", "access_token": token})
        with VK_LOCK:
            try:
                return get_json(f"https://api.vk.com/method/{method}?{params}", "api.vk.com")
            finally:
                time.sleep(0.4)

    owner_id = source.get("owner")
    if owner_id is None:
        profile = vk("utils.resolveScreenName", screen_name=source["screen_name"]).get("response")
        if not isinstance(profile, dict) or profile.get("type") not in {"user", "group"}:
            raise ValueError("VK не подтвердил профиль автора")
        owner_id = int(profile["object_id"]) * (-1 if profile["type"] == "group" else 1)
    data = vk("video.get", owner_id=owner_id, count=source.get("count", 25))
    if "response" not in data:
        number = data.get("error", {}).get("error_code")
        raise ValueError(f"VK video.get · код {number}" if isinstance(number, int) else "VK video.get отклонил запрос")
    for video in data["response"]["items"]:
        owner, key, date = video["owner_id"], video["id"], video.get("date")
        if int(owner) != owner_id or not date or not video.get("title") or video.get("is_private"):
            continue
        yield entry(source, title=video["title"], url=f"https://vkvideo.ru/video{owner}_{key}",
                    text=video.get("description"), date=dt.datetime.fromtimestamp(date, dt.timezone.utc).isoformat(),
                    key=f"{owner}_{key}", kind="video", origin="VK API · video.get",
                    extra=duration(video.get("duration")))


def youtube_details(items, token):
    lookup = {item["feed_id"]: item for item in items if item["kind"] == "video"
              and item["feed_id"] and item["provenance"].startswith("https://www.youtube.com/")}
    keys = list(lookup)
    errors = []
    for offset in range(0, len(keys), 50):
        params = urllib.parse.urlencode({"id": ",".join(keys[offset:offset + 50]),
                                        "part": "contentDetails,status,snippet", "key": token})
        try:
            response = get_json(f"https://www.googleapis.com/youtube/v3/videos?{params}", "googleapis.com")
            for video in response["items"]:
                item = lookup[video["id"]]
                item.update(duration(video["contentDetails"].get("duration")))
                audio = video.get("snippet", {}).get("defaultAudioLanguage")
                if audio:
                    item["audio_language"] = audio.split("-")[0]
                if video["status"].get("embeddable"):
                    item["player"] = "https://www.youtube-nocookie.com/embed/" + video["id"]
        except (OSError, ValueError, KeyError) as error:
            errors.append(type(error).__name__)
    return errors


def crossposts(items, config):
    """Use an author's fuller description only when both uploads identify the same work."""
    publishers = {source["id"]: source.get("publisher", source["id"]) for source in config["sources"]}
    videos = [item for item in items if item["kind"] == "video" and item.get("published")]
    for item in videos:
        if not item.get("player") or len(item["evidence"].split()) >= 7:
            continue
        date = dt.datetime.fromisoformat(item["published"])
        terms = set(re.findall(r"[а-яёa-z\d]{2,}|\d", item["title"].lower()))
        if len(terms) < 4:
            continue
        for peer in videos:
            if (item["source"] == peer["source"] or publishers[item["source"]] != publishers[peer["source"]]
                    or len(peer["evidence"].split()) < 7
                    or abs(date - dt.datetime.fromisoformat(peer["published"])) > dt.timedelta(days=1)):
                continue
            other = set(re.findall(r"[а-яёa-z\d]{2,}|\d", peer["title"].lower()))
            if terms <= other and {word for word in terms if word.isdecimal()} == {
                    word for word in other if word.isdecimal()}:
                item["evidence"] = peer["evidence"]
                item["related_urls"] = [peer["url"]]
                item["provenance"] += f" · описание из авторской версии {peer['url']}"
                break


def collect(config):
    """Return live entries and a per-source report, even if some feeds are unavailable."""
    def fetch(source):
        identity = {"source": source["id"], "name": source["name"]}
        connector = source.get("connector", "feed")
        vk_token = os.getenv("JOURNAL_VK_ACCESS_TOKEN")
        if connector == "vk" and not vk_token:
            return [], {**identity, "count": 0, "status": "Нужен ключ VK API"}
        for attempt in range(2):
            try:
                if connector == "rutube":
                    query = "?format=json" if attempt == 0 else ""
                    url = f"https://rutube.ru/api/video/person/{source['channel']}/{query}"
                    items = list(rutube_entries(get_json(url, "rutube.ru"), source))
                elif connector == "vk":
                    items = list(vk_entries(source, vk_token))
                else:
                    feed = source.get("feed_alternate", source["feed"]) if attempt else source["feed"]
                    raw, _ = request(feed, source["feed_domains"], 4_000_001)
                    if len(raw) > 4_000_000:
                        raise ValueError("Слишком большой ответ")
                    items = list(feed_entries(raw, {**source, "feed": feed}))
                return items, {**identity, "count": len(items), "status": "ok"}
            except (OSError, ET.ParseError, KeyError, ValueError) as exc:
                status = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
                if connector == "vk" and re.fullmatch(r"VK video.get · код \d+", str(exc)):
                    status = str(exc)
                    break
        return [], {**identity, "status": status, "count": 0}

    sources = [source for source in config["sources"] if source.get("enabled")
               and (source.get("feed") or source.get("connector") in {"rutube", "vk"})]
    with ThreadPoolExecutor(max_workers=8) as pool:
        collected = list(pool.map(fetch, sources))
    items = [item for batch, _ in collected for item in batch]
    report = [info for _, info in collected]
    key = os.getenv("JOURNAL_YOUTUBE_API_KEY")
    if key:
        errors = youtube_details(items, key)
        report.append({"source": "youtube-details", "name": "YouTube Data API",
                       "count": 0, "status": ", ".join(errors) or "ok"})
    crossposts(items, config)
    return items, report


def images(item, config):
    image = item.get("image")
    if image and image.get("license") in {"CC0", "Public Domain", "CC BY 4.0"}:
        if allowed(image.get("url", ""), config["image_domains"]) and image.get("credit"):
            return image
    return None
