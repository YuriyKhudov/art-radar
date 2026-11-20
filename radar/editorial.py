"""Russian-language editorial assessment and single-message issue rendering."""

import datetime as dt
import html
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from radar import artist_radar, editorial_sources as sources, journal_reserve

SITE = "https://yuriykhudov.github.io/art-radar/"
MOSCOW = dt.timezone(dt.timedelta(hours=3))
WEIGHTS = {"usefulness": 30, "originality": 25, "language": 20, "source": 15, "freshness": 10}
DIRECTIONS = {
    "studio": "🖌 В мастерской",
    "practice": "💡 Полезная находка",
    "news": "🌍 Мир искусства",
    "people": "🎬 Что посмотреть",
    "archive": "🗂 Из архива",
}
ART = re.compile(r"живопис|рис[уо]|график|картин|акварел|гуаш|акрил|портрет|пейзаж|гравюр|холст|пигмент|"
                 r"графит|художни|пастел|натюрморт|фломастер|светотен|наброс|этюд|палитр|скетч|"
                 r"скульптур|арт[- ]объект|авторск[а-я]+ кукл|колорит|тональн|маз(?:ок|к)", re.I)
PRACTICE = re.compile(r"как (?:на)?рисов|как написа|как сделать|урок|мастер-класс|техник|разбираем|пошагов|"
                      r"показыва(?:ю|ет).*как|тестиру|обзор.*(?:красок|кистей|бумаг|материал)|упражнен|"
                      r"как (?:смешива|выстраива|переда)\w*[^.?!]{0,30}(?:колорит|цвет|тон|оттен|краск)", re.I)
PEOPLE = re.compile(r"интервью|документальн|фильм о|разговор с|бесед[ау]|лекци[яю]|подкаст|монолог|история художник", re.I)
NEWS = re.compile(r"выставк|биеннале|экспозиц|ярмарк|фестивал|резиденц|открыти|анонсиру|проект|награду", re.I)
STUDIO = re.compile(r"мастерск|этюд|наброс|пленэр|процесс|рабочий|пишу|рисую|живопись|работа художник|эксперимент", re.I)
ARCHIVAL = re.compile(r"музейный архив|архивн[а-я]+ кадр|классики живописи|история искусства|из коллекции музея", re.I)
PROMO = re.compile(r"пресс-релиз на правах рекламы|партн[её]рский материал|advertorial|gift guide|"
                   r"полный (?:мастер-класс|урок|разбор).{0,50}(?:платн|boosty|по подписке|в закрыт|в разделе.*товары)|"
                   r"только трейлер|(?<![а-я])тизер(?=\W|$)", re.I)
PROMO_TITLE = re.compile(r"скидк|распродаж|ч[её]рная пятница|цветная пятница|курс[а-я]* со скидк|"
                         r"анонс (?:курса|урока|практикума)|(?:новогодний|зимний|летний) практикум|"
                         r"запись на|урок[аи]? на boosty|уроки? .*в разделе видео|смотрите .*полный обзор|"
                         r"лучш[а-я]+ менеджер|наш[аи] команд[аые]|завтра\W+премьера|"
                         r"скоро\s+(?:большая\s+)?(?:череда\s+)?аукцион", re.I)
TEASER = re.compile(r"смотрите полн|ссылка на полн|полный выпуск|видео в разделе|полный .* по ссылке|фрагмент интервью|"
                    r"скоро нов[а-я]+ выпуск|ловите на канале нов[а-я]+ интервью", re.I)
COURSE_LEAD = re.compile(r"фрагмент из курса|отрывок из курса|тизер курса", re.I)
COURSE_LINK = re.compile(r"полность?ю курс|полн\w+ курс|курс\b.{0,55}по ссылке|записаться на курс", re.I)
VISUAL_ART = re.compile(r"живопис|изобразител|акварел|гравюр|скульпт|рисунк|рисован|художни[кц]|этюд|графит|пейзаж|офорт", re.I)
PERSONAL_VLOG = re.compile(r"\bвлог\b.*(?:семь|личн|муж|покуп|быт|итоги года)|"
                           r"(?:без мужа|семейн\w+ будни|личн\w+ (?:жизнь|итоги)|итоги года).*\bвлог\b", re.I)
PARTY = re.compile(r"вечеринк|дефиле|нарядил\w*|костюмирован\w*|приятно провести время", re.I)
PITCH = re.compile(r"посмотрите видео, о котором|как попасть на .*урок|ссылка на курс|промокод|по ссылке в описании", re.I)
CHAPTER = re.compile(r"(?<!\d)(?:\d+:)?\d{1,2}:\d{2}\s*[-–—]\s*")
CEREMONY = re.compile(r"торжественн\w+ открыти|церемони[яию]\s+(?:открыти|награждени)|"
                       r"приветственн\w+ слов[ао].*на открытии", re.I)
DIGITAL_TOOL = re.compile(r"procreate|photoshop|blender|иллюстратор|на планшете|в иллюстраторе", re.I)
DIGITAL_EXERCISE = re.compile(r"кресл|стул|диван|мебел|логотип|наклейк|иконк|интерфейс|"
                              r"как начать рисовать на планшете|курс по программ", re.I)
CLIP_PROCESS = re.compile(r"маз[окк]|пишу|рисую|рисован|живопис|этюд|тушью|углём|углем|"
                          r"показ\w+.{0,20}(?:техник|приём|прием|работ)|процесс|новые картины|plein air", re.I)
PAY = re.compile(r"только по подписке|платн[а-я]+ доступ|paywall|subscribers only|требуется оплата", re.I)
MARKET = re.compile(r"аукцион|арт-рынок|рекордн[а-я]+ цен", re.I)
PRICE_RECORD = re.compile(r"рекорд[а-я]* (?:цен|стоимост|кандинск)|продан[а-я]* за "
                          r"(?:рекорд|\$|.{0,35}(?:доллар|миллион|млн))", re.I)
STOP = set("новая новые новый новости рассказал рассказывает музей художник искусство выставка картин впервые открыл открывается открылась".split())
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
SLOGAN = "Искусство, которое стоит вашего внимания"
PHOTO_CAPTION_LIMIT = 1024
DRAWING_EXERCISE = re.compile(r"^(\d{1,3})\s+(charcoal|graphite|pencil|ink)\s+(portraits|sketches|drawings)"
                              r"\s+in\s+(\d{1,3})\s+minutes?\s*(\([^\n]*\))?$", re.I)


@dataclass(frozen=True)
class Candidate:
    item: dict
    source: dict
    key: str
    words: frozenset
    metrics: dict
    score: float
    age: int | None

    @property
    def direction(self):
        return self.item["direction"]

    @property
    def publisher(self):
        return self.source.get("publisher", self.source["id"])

    @property
    def stream(self):
        return self.item.get("artist_radar", {}).get("stream", "general")

    @property
    def fresh(self):
        return self.direction != "archive" and self.age is not None and not self.item.get("from_reserve")

    @property
    def russian(self):
        return self.item["lang"] == "ru" or self.item.get("translation") in {"ru_audio", "ru_subtitles", "ru_text"}

    @property
    def non_us(self):
        origin = self.item.get("origin") or self.source.get("origin") or ("RU" if self.source["lang"] == "ru" else "")
        return origin not in {"", "US"}

    def audit(self, decision):
        return {"title": self.item["title"], "url": self.item["url"], "source": self.source["name"],
                "publisher": self.publisher, "published": self.item.get("published"), "duration": self.item.get("duration"),
                "direction": self.direction, "kind": self.item["kind"], "age": self.age,
                "provenance": self.item.get("provenance", "картотека"),
                "artist_radar": self.item.get("artist_radar"), "search_matches": self.item.get("search_matches", []),
                "reserve_bucket": self.item.get("reserve_bucket"), "from_reserve": bool(self.item.get("from_reserve")),
                "score": self.score, "ratings": self.metrics, "decision": decision,
                "reason": self.item["why"]}


class EditorialProvider(Protocol):
    def edit(self, raw: dict, source: dict) -> tuple[dict | None, str]:
        """Return a fact-based Russian edit or the reason it needs to be held."""


def shorten(text, length):
    return text if len(text) <= length else text[:length - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


class RulesEditor:
    def edit(self, raw, source):
        if raw.get("reviewed"):
            direction = "archive" if raw["type"] == "archive" else raw.get("direction", "news")
            return {**raw, "direction": direction}, ""
        raw_evidence = raw.get("evidence", "")
        evidence = sources.description(raw_evidence)
        title = re.sub(r"\s+#[\w]+", "", raw["title"]).strip()
        access_evidence = raw.get("access_evidence", raw_evidence)
        if PERSONAL_VLOG.search(title):
            return None, "Личный сюжет занимает главное место"
        if raw["kind"] == "video" and len(set(hit.casefold() for hit in PARTY.findall(evidence))) >= 3:
            return None, "В описании преобладает личный репортаж с вечеринки"
        if CEREMONY.search(title):
            return None, "Церемония вместо разбора искусства"
        if DIGITAL_TOOL.search(title) and DIGITAL_EXERCISE.search(title):
            return None, "Упражнение по цифровому инструменту вне основной темы"
        if (PROMO_TITLE.search(title) or PROMO.search(title + " " + access_evidence)
                or raw.get("promotion_dominant", sources.advertising_dominates(access_evidence))
                or COURSE_LEAD.search(title) and COURSE_LINK.search(access_evidence)
                or (raw.get("seconds") or 0) < 150 and TEASER.search(access_evidence)):
            return None, "Рекламный или партнёрский материал"
        if raw.get("seconds") and raw["seconds"] < 45 and not CLIP_PROCESS.search(evidence):
            return None, "Короткий фрагмент без подтверждённого художественного действия"
        if len(evidence.split()) < 7 or len(evidence) < (33 if raw["kind"] == "video" else 45):
            return None, "Недостаточно содержательного описания источника"
        if source["lang"] != "ru" or not sources.CYRILLIC.search(evidence) or not sources.CYRILLIC.search(title):
            return None, "Русскоязычное описание отсутствует"
        chapters = [part.strip(" .—–") for part in CHAPTER.split(evidence)[1:] if ART.search(part) and len(part.split()) >= 4]
        if chapters:
            evidence = " ".join(part if part.endswith(("?", "!")) else f"{part}." for part in chapters[:2])
        text = title + " " + evidence
        if source.get("general_media") and not VISUAL_ART.search(text):
            return None, "В общей культурной ленте нет темы изобразительного искусства"
        if raw["kind"] == "video" and len(words(title)) < 3 and not ART.search(title):
            return None, "У ролика нет конкретной темы в заголовке"
        is_author_story = source.get("direction") == "people" and "мастерск" in text.lower() and raw.get("seconds", 0) >= 480
        if not ART.search(text) and not is_author_story and not (
                source.get("creator") and (PRACTICE.search(text) or STUDIO.search(text))):
            return None, "Связь с живописью, рисунком или графикой не подтверждена"
        if PRICE_RECORD.search(title):
            return None, "В центре материала аукционная цена"
        if re.search(r"опен-колл|при[её]м заявок|отбор художников|грант для художник", title, re.I) and not re.search(
                r"до \d{1,2} (?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)",
                title, re.I):
            return None, "Срок подачи и условия не подтверждены"
        direction, signals = self.classify({**raw, "title": title}, source, text)
        if raw.get("archive_index") and source.get("archive_indexes"):
            direction, signals = "archive", "Материал в образовательном архиве издания."
        if not direction:
            return None, "Не найдена подходящая рубрика"
        words_available = 25
        sentences = [part.lstrip("👍✅🔴🎨📌👇👻🏆 —") for part in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ«👍✅🔴🎨📌👇👻🏆])", evidence)]
        specific = [part for part in sentences if len(words(part) - words(title)) >= 4
                    and not PITCH.search(part)
                    and not re.match(r"предлагаем вашему вниманию|всем привет|друзья,|подробности по ссылке", part, re.I)]
        complete = [part for part in specific if part.endswith((".", "?", "!")) and 8 <= len(part.split()) <= words_available]
        if chapters:
            excerpt = evidence
        elif complete:
            excerpt = max(complete, key=lambda part: (bool(ART.search(part)), len(words(part) - words(title))))
        else:
            excerpt = specific[0] if specific else sentences[0]
        fragment = " ".join(excerpt.split()[:words_available])
        punctuation = "…" if len(fragment) < len(excerpt) and fragment[-1:] not in ".?!…" else ""
        shortened = fragment.replace("«", "„").replace("»", "“")
        access = raw.get("access", source.get("access", "unknown"))
        if PAY.search(access_evidence):
            access = "paid"
        elif re.search(r"нужна регистрация|требуется регистрация", access_evidence, re.I):
            access = "registration"
        relevance = {"studio": 4.5, "practice": 4.7, "news": 4.1, "people": 4.4, "archive": 4.3}
        display_title = re.sub(r"\s+[_|]\s+художник\s+[^_|]+$", "", title, flags=re.I)
        if display_title.isupper():
            display_title = display_title.capitalize()
        return {**raw, "title": shorten(display_title, 126), "original_title": raw["title"], "summary": f"«{shortened}{punctuation}»",
                "why": f"Рубрика «{DIRECTIONS[direction][2:]}» по описанию источника. {signals}",
                "direction": direction, "rubric": DIRECTIONS[direction], "lang": "ru", "access": access,
                "type": "archive" if raw.get("archive_index") else "news",
                "topics": source.get("topics", []), "usefulness": relevance[direction],
                "originality": min(4.7, 3.7 + len(words(evidence) - words(title)) / 27)}, ""

    @staticmethod
    def classify(raw, source, text):
        title, kind = raw["title"], raw["kind"]
        if PEOPLE.search(title) or (source.get("direction") == "people"
                                    and (PEOPLE.search(text) or raw.get("seconds", 0) >= 480
                                         and re.search(r"гост[ьяеи]|рассказыва|беседуем|выпуск", text, re.I))):
            return "people", "Фильм, интервью, лекция или авторский рассказ."
        if NEWS.search(title) and not source.get("creator"):
            return "news", "Речь о событии, выставке или профессиональной возможности."
        if ARCHIVAL.search(title):
            return "archive", "Архивные записи, история искусства или музейное собрание."
        if PRACTICE.search(text) and (kind != "video" or (raw.get("seconds") or 0) >= 150
                                      or raw.get("seconds") is None and not TEASER.search(text)):
            return "practice", "В описании есть конкретный приём, разбор техники или материала."
        if kind == "video" and (source.get("creator") or STUDIO.search(text)):
            return "studio", "Запись работы автора, жизни мастерской или процесса создания изображения."
        if NEWS.search(text) or MARKET.search(title):
            return "news", "В описании есть связь с текущей художественной повесткой."
        if source.get("direction") == "studio" or STUDIO.search(text):
            return "studio", "Авторский процесс или современная художественная работа."
        return None, ""


def official_drawing_exercise(raw, source):
    info = raw.get("artist_radar") or {}
    if (raw["kind"] != "video" or not source.get("artist_id") or source["lang"] == "ru"
            or info.get("stream") != "priority" or not info.get("artist") or (raw.get("seconds") or 45) < 45
            or not sources.allowed(raw["url"], source["domains"])):
        return None
    match = DRAWING_EXERCISE.fullmatch(raw["title"].strip())
    if not match or "/shorts/" in raw["url"]:
        return None
    count, medium, subject, minutes, extra = match.groups()
    tools = {"charcoal": "углём", "graphite": "графитом", "pencil": "карандашом", "ink": "тушью"}
    subjects = {"portraits": ("портрет", "портрета", "портретов"),
                "sketches": ("набросок", "наброска", "набросков"), "drawings": ("рисунок", "рисунка", "рисунков")}
    amount = int(count)
    form = 2 if 11 <= amount % 100 <= 14 else 0 if amount % 10 == 1 else 1 if 2 <= amount % 10 <= 4 else 2
    if not amount or not int(minutes):
        return None
    artist = info["artist"]
    label = f"{count} {subjects[subject.lower()][form]} {tools[medium.lower()]} за {minutes} мин."
    summary = f"{artist} ограничивает время на рисунок. В заголовке ролика — {label.lower()}"
    if count == minutes and extra and "1 minute" in extra.lower():
        summary += " На один рисунок отведена минута."
    return {**raw, "title": f"{artist} · {label}", "original_title": raw["title"], "summary": summary,
            "why": "Задание и материал подтверждены заголовком авторского ролика.", "direction": "practice",
            "rubric": DIRECTIONS["practice"], "lang": source["lang"], "access": raw.get("access", "unknown"),
            "type": "news", "topics": ["techniques"], "usefulness": 4.5, "originality": 4.6,
            "verified_visual": True}


def words(title):
    return frozenset(word[:7] for word in re.findall(r"[а-яёa-z0-9]{4,}", title.lower().replace("ё", "е"))
                     if word not in STOP)


def similar(a, b):
    return min(len(a), len(b)) >= 4 and len(a & b) / len(a | b) >= .72


def duplicates(candidate, item):
    a, b = candidate.item, item
    links_a = {candidate.key, *(sources.canonical(url) for url in a.get("related_urls", []))}
    links_b = {sources.canonical(b["url"]), *(sources.canonical(url) for url in b.get("related_urls", []))}
    old_words = words(b.get("original_title", b["title"]))
    platforms = {sources.video_platform(row["url"]) for row in (a, b)} if a.get("kind") == b.get("kind") == "video" else set()
    different_platforms = len(platforms) == 2 and None not in platforms
    return (bool(links_a & links_b)
            or bool(a.get("story") and a["story"] == b.get("story"))
            or bool(a.get("feed_id") and a["source"] == b["source"] and a["feed_id"] == b.get("feed_id"))
            or not different_platforms and (len(candidate.words) >= 2 and candidate.words == old_words
                                            or similar(candidate.words, old_words)))


def preview_only(item):
    media = item.get("journal_visual") or {}
    return bool(media.get("video_link") and not (media.get("images") or media.get("video")))


DIGITAL_FOCUS = re.compile(r"\b(?:gta|gamedev|game\s+design|concept\s+art|3d|procreate|figma|blender|"
                           r"рису[а-яё]+\s+на\s+планшете|игров[а-яё]+\s+индустри[а-яё]+|"
                           r"компьютерн[а-яё]+\s+игр[а-яё]*|видеоигр[а-яё]*|геймдизайн[а-яё]*|"
                           r"цифров[а-яё]+\s+дизайн[а-яё]*|дизайн[а-яё]+\s+интерфейс[а-яё]*)\b", re.I)


def assess(items, config, now, history=(), provider=None):
    provider = provider or RulesEditor()
    catalog = {source["id"]: source for source in config["sources"] if source.get("enabled")}
    candidates, report = [], []
    today = now.astimezone(MOSCOW).date()
    registry = config.get("artist_radar")
    for raw in items:
        source = catalog.get(raw["source"])
        if registry and source:
            raw = artist_radar.annotate(raw, config, source)
        item, reason = provider.edit(raw, source) if source else (None, "Источник выключен")
        if registry and source and not item and reason == "Русскоязычное описание отсутствует":
            item = artist_radar.russian_facts(raw, registry)
            if item:
                reason = ""
        if registry and source and not item and reason == "Не найдена подходящая рубрика" and raw.get("search_matches"):
            item = artist_radar.russian_facts(raw, registry)
            if item:
                reason = ""
        if config.get("video_link_fallback") and source and not item and reason in {
                "Недостаточно содержательного описания источника", "Русскоязычное описание отсутствует"}:
            item = official_drawing_exercise(raw, source)
            if item:
                reason = ""
        if item:
            published = dt.datetime.fromisoformat(item["published"]) if item.get("published") else None
            day_age = (today - published.astimezone(MOSCOW).date()).days if published else None
            outside = day_age is None or day_age > config["news_days"]
            expires = item.get("reserve_expires")
            if config.get("accumulate_reserve") and not expires and outside:
                window = journal_reserve.expiry(item, today, config["news_days"])
                if window and window[0] not in {"news", "event"} and (day_age is not None or item.get("archive_index")):
                    item = {**item, "reserve_bucket": window[0], "reserve_expires": window[1].isoformat()}
                    expires = item["reserve_expires"]
            in_reserve = bool(expires and dt.date.fromisoformat(expires) >= today
                              and (item["direction"] != "news" or item.get("reserve_bucket") == "event"))
            if published and published > now:
                reason = "Дата публикации ещё не наступила"
            elif item.get("event_end") and dt.date.fromisoformat(item["event_end"][:10]) < today:
                reason = "Выставка или событие завершились"
            elif item["type"] == "news" and outside and not in_reserve:
                reason = "Материал вне семидневного окна или без даты"
            elif item.get("deadline") and dt.date.fromisoformat(item["deadline"]) < today:
                reason = "Срок подачи истёк"
            elif not all(sources.CYRILLIC.search(item.get(field, "")) for field in ("title", "summary", "why")):
                reason = "Требуется русская редактура"
            elif item["kind"] == "video" and item["lang"] != "ru" and not item.get("verified_visual") and (
                    not item.get("translation_checked")
                    or item.get("translation") not in {"ru_audio", "ru_subtitles", "none"}
                    or item.get("translation") == "none" and not item.get("visual")):
                reason = "Русскоязычное сопровождение не проверено или ролик трудно понять без перевода"
            elif (item["kind"] == "video" and config.get("require_video_duration") and not item.get("duration")
                  and not (source.get("artist_id") or source.get("creator"))):
                reason = "Ролик без хронометража не подтверждён авторским каналом"
            elif item["access"] == "paid":
                reason = "Платный доступ"
            if outside and in_reserve:
                item["from_reserve"] = True
        if reason:
            report.append({"url": raw["url"], "title": raw["title"], "source": raw["source"], "decision": reason,
                           "published": raw.get("published"), "artist_radar": raw.get("artist_radar"),
                           "search_matches": raw.get("search_matches", [])})
            continue
        russian = item["lang"] == "ru" or item.get("translation") in {"ru_audio", "ru_subtitles", "ru_text"}
        language = (5 if item["access"] == "free" else 4.3) if russian else 2.5
        if item["access"] == "registration":
            language -= .7
        freshness = (5 if day_age is not None and day_age <= 3 else
                     3.5 if day_age is not None and day_age <= config["news_days"] else 2.5)
        metrics = {"usefulness": item["usefulness"], "originality": item["originality"],
                   "language": language, "source": source["priority"], "freshness": freshness}
        score = round(sum(metrics[name] * weight / 5 for name, weight in WEIGHTS.items()), 2)
        candidate = Candidate(item, source, sources.canonical(item["url"]), words(raw["title"]), metrics, score, day_age)
        if any(duplicates(candidate, old) for old in history):
            report.append(candidate.audit("Материал или тот же сюжет уже опубликован"))
        elif score < config["minimum_score"]:
            report.append(candidate.audit("Оценка ниже редакционного порога"))
        else:
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: (-item.score, item.key)), report


def select(candidates, config, theme=None, verify=None, history=(), now=None, reserve=False, visual=False):
    picked, rejected = [], {}
    rubrics, providers, month_rubrics, month_providers = Counter(), Counter(), Counter(), Counter()
    month_streams, streams = Counter(), Counter()
    publishers = {source["id"]: source.get("publisher", source["id"]) for source in config["sources"]}
    if now:
        since = now - dt.timedelta(days=30)
        for item in history:
            if item.get("_issued_at") and dt.datetime.fromisoformat(item["_issued_at"]) >= since:
                month_rubrics[item.get("direction") or ("archive" if item.get("type") == "archive" else "news")] += 1
                month_providers[publishers.get(item.get("source"), item.get("source"))] += 1
                month_streams[item.get("artist_radar", {}).get("stream", "general")] += 1
    mix = config["monthly_mix"]
    artist_mix = config.get("artist_radar", {}).get("mix")
    for position in range(config["max_items"]):
        eligible = []
        for candidate in candidates:
            if candidate in picked or candidate.key in rejected:
                continue
            rubric = candidate.direction
            if theme and theme not in candidate.item.get("topics", []):
                continue
            if any(duplicates(candidate, prior.item) for prior in picked):
                rejected[candidate.key] = "Другой материал о том же сюжете выбран в выпуск"
                continue
            if providers[candidate.publisher] >= (2 if theme else 1):
                continue
            if not theme and rubrics[rubric] >= 2:
                continue
            if visual and picked and preview_only(picked[-1].item) and preview_only(candidate.item):
                continue
            archive_floor = config["extra_score"] if reserve and candidate.item.get("archive_index") else config["archive_score"]
            if not theme and rubric == "archive" and (rubrics["archive"] or candidate.score < archive_floor
                                                       or (position < config["min_items"] - 1 and not reserve)):
                continue
            if position >= config["min_items"] and candidate.score < config["extra_score"]:
                continue
            eligible.append(candidate)
        if not eligible:
            break
        russian_needed = round((position + 1) * config["russian_share"]) > sum(p.russian for p in picked)
        needs_video = not theme and not any(item.item["kind"] == "video" for item in picked)
        month_total = sum(month_rubrics.values()) + len(picked) + 1

        def priority(card):
            age = card.age if card.age is not None else 999
            deficit = mix[card.direction] * month_total - month_rubrics[card.direction] - rubrics[card.direction]
            balance = min(8, max(-8, 3 * deficit))
            video = card.item["kind"] == "video"
            boost = ((7 if needs_video and video else 0) + (4 if video and card.item.get("duration") else 0)
                     + (4 if card.item.get("seconds", 0) >= 480 else 0))
            freshness = 6 if card.fresh and age <= 3 else 2 if card.fresh else -9
            featured = not theme and not picked and card.fresh and age <= 3
            artist_info = card.item.get("artist_radar") or {}
            artist_boost = artist_radar.BONUS.get(artist_info.get("tier"), 3 if card.stream == "discovery" else 0)
            style_boost = min(2, len(artist_info.get("style_ids", [])))
            shortage = (artist_mix[card.stream] * month_total - month_streams[card.stream] - streams[card.stream]
                        if artist_mix else 0)
            stream_balance = min(7, max(-7, 3 * shortage))
            if (shortage > 0 and len(picked) >= 2 and card.stream != "general" and not streams[card.stream]
                    and card.fresh and card.score >= config["extra_score"]):
                stream_balance += 12
            visual = (card.item.get("journal_visual") or {}).get("quality", 0)
            needs_picture = position > 0 and not any((p.item.get("journal_visual") or {}).get("images")
                                                     or (p.item.get("journal_visual") or {}).get("video") for p in picked)
            media = card.item.get("journal_visual") or {}
            completes_visual_issue = needs_picture and bool(media.get("images") or media.get("video"))
            rating = (card.score + visual + balance + boost + freshness - 8 * rubrics[card.direction]
                      - min(24, 7 * month_providers[card.publisher])
                      + (3 if card.russian == russian_needed else 0) + artist_boost + style_boost + stream_balance)
            digital = bool(DIGITAL_FOCUS.search(f"{card.item['title']} {card.item.get('evidence', '')}"))
            return completes_visual_issue, not digital, featured, rating

        eligible.sort(key=priority, reverse=True)
        for candidate in eligible:
            outcome = verify(candidate) if verify else True
            if isinstance(outcome, str) or not outcome:
                rejected[candidate.key] = outcome or "Прямая ссылка не прошла сетевую проверку"
                continue
            if any(duplicates(candidate, prior.item) for prior in picked):
                rejected[candidate.key] = "Другой материал о том же сюжете выбран в выпуск"
                continue
            picked.append(candidate)
            rubrics[candidate.direction] += 1
            providers[candidate.publisher] += 1
            streams[candidate.stream] += 1
            break
        else:
            break
    short_issue = reserve and len(picked) == 2 and all(p.score >= config["extra_score"] for p in picked)
    has_media = any((p.item.get("journal_visual") or {}).get("images")
                    or (p.item.get("journal_visual") or {}).get("video") for p in picked)
    if ((len(picked) < config["min_items"] and not short_issue) or not any(p.russian for p in picked)
            or not any(p.non_us for p in picked) or visual and not has_media
            or not theme and (len(rubrics) < 2 or not reserve and not any(p.fresh for p in picked))):
        picked = []
    report = [candidate.audit("Выбран в выпуск" if candidate in picked else rejected.get(candidate.key) or
                              ("Другая тема" if theme and theme not in candidate.item.get("topics", []) else
                               "Не вошёл по балансу рубрик, языка и источников либо выпуск не набрал минимум"))
              for candidate in candidates]
    for candidate, row in zip(candidates, report):
        if candidate in picked:
            relevance = []
            if candidate.direction == "archive":
                relevance.append("Архивная находка с редакционной отметкой")
            elif candidate.item.get("from_reserve"):
                relevance.append("Материал из накопительного резерва")
            elif candidate.age is not None:
                relevance.append("Публикация за последние три дня" if candidate.age <= 3 else
                                 "Публикация в семидневном окне")
            if candidate.stream == "priority":
                relevance.append("Автор из приоритетного реестра")
            if candidate.stream == "discovery":
                relevance.append("Новый автор с подтверждённой художественной связью")
            if candidate.item["kind"] == "video" and not candidate.item.get("duration"):
                relevance.append("Авторский канал и содержательное описание подтверждены")
            row["reason"] += " " + ". ".join(relevance) + "."
    return picked, report


def link(title, url):
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>'


def length(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


def complete_sentences(text):
    text = " ".join(text.split())
    if text.startswith("«") and text.endswith("»"):
        text = text[1:-1]
    parts = re.findall(r'.+?[.!?][»”"]?(?=\s+[А-ЯЁA-Z«„"]|$)', text)
    return [part.strip() for part in parts if not re.search(r'\.{3}|…', part)
            and len(part.split()) >= 5 and not PITCH.search(part)]


def summary_options(item, presentation):
    preferred = complete_sentences(presentation.get("summary", item["summary"]))
    evidence = complete_sentences(item.get("evidence", "")) if item["lang"] == "ru" else []
    evidence = sorted((part for part in evidence if sources.CYRILLIC.search(part)
                       and not re.match(r"предлагаем вашему вниманию|всем привет|друзья,", part, re.I)),
                      key=lambda part: not bool(ART.search(part)))
    return list(dict.fromkeys([" ".join(preferred[:2]), *preferred, *evidence]))


def display_card(candidate, config, url_check):
    item = candidate.item
    info = item.get("artist_radar") or {}
    layout = config.get("presentation", {})
    custom = layout.get("cards", {}).get(candidate.key, {})
    artist = info.get("artist", "")
    russian = layout.get("names", {}).get(artist)
    title = custom.get("title") or (item.get("journal_article") or {}).get("title") or item["title"]
    if russian:
        title = (title.replace(artist, russian) if not custom.get("title") else title)
    credit = custom.get("credit") or candidate.source["name"].split(" · ", 1)[0]
    source_line = f"<i>{html.escape(credit)}</i>" if credit.casefold() not in title.casefold() else ""
    if russian:
        source_line += (" · " if source_line else "") + f"<i>{html.escape(artist)}</i>"
    heading = DIRECTIONS[candidate.direction]
    if info.get("stream") == "discovery":
        heading = "🔎 Художник в поле зрения"
    elif info.get("stream") == "priority" and candidate.direction == "practice":
        heading = artist_radar.HEADINGS["technique"]
    if item.get("from_reserve") and candidate.direction != "archive":
        heading += " · Из резерва"
    label = "Читать статью"
    duration = ""
    if item["kind"] == "video":
        label = video_label(sources.video_platform(item.get("video_url") or item["url"]))
        duration = f" · {html.escape(item['duration'])}" if item.get("duration") else ""
    elif info.get("feature") == "works":
        label = "Посмотреть работы"
    image = sources.images({**item, **custom}, config)
    if image and (candidate.publisher in config.get("cover_exclusions", []) or
                  url_check and not url_check(image["url"], config["image_domains"], image=True)):
        image = None
    actions = video_actions(item) if item["kind"] == "video" else []
    footer = (" · ".join(link(action["label"], action["url"]) for action in actions)
              if actions else link(label, item["url"])) + duration
    if source_line:
        footer += " · " + source_line
    byline = " · ".join(filter(None, (item.get("duration") if item["kind"] == "video" else None,
                                      credit if credit.casefold() not in title.casefold() else None,
                                      artist if russian else None)))
    return {"heading": heading, "title": title, "footer": footer, "action": label, "actions": actions, "byline": byline,
            "summaries": summary_options(item, custom), "image": image}


def video_label(platform):
    if platform == "VK Видео":
        return "Смотреть в VK Видео"
    return f"Смотреть на {platform}" if platform else "Открыть оригинал"


def video_actions(item):
    urls = {}
    for url in [item.get("video_url") or item["url"], *item.get("verified_video_urls", [])]:
        if platform := sources.video_platform(url):
            urls.setdefault(platform, url)
    return [{"label": video_label(platform), "url": urls[platform]}
            for platform in ("RUTUBE", "YouTube", "VK Видео") if platform in urls]


def issue_layout(picked, config, theme, url_check, now):
    date = (now or dt.datetime.now(MOSCOW)).astimezone(MOSCOW).date()
    date_text = f"{date.day} {MONTHS[date.month - 1]} {date.year}"
    header = [f"<b>Art Radar · {date_text}</b>", f"<i>{SLOGAN}</i>"]
    if theme:
        header.append(f"<b>{html.escape(config['themes'][theme]['title'])}</b>")

    ordered = list(picked)
    lead = next((card for card in picked if card.stream == "priority" and card.fresh and
                 card.age <= config["news_days"] and card.score >= config["extra_score"]), None)
    if lead:
        proposed = [lead, *(card for card in ordered if card != lead)]
        if not any(preview_only(a.item) and preview_only(b.item) for a, b in zip(proposed, proposed[1:])):
            ordered = proposed
    cards = [display_card(candidate, config, url_check) for candidate in ordered]
    return header, ordered, cards, date_text


def visual_assets(item, config, all_candidates=False):
    presentation = config.get("presentation", {})
    key = sources.canonical(item["url"])
    custom = presentation.get("cards", {}).get(key, {})
    baseline = [presentation.get("cover_images", {}).get(key),
                sources.images({**item, **custom}, config), sources.images(item, config)]
    approved = presentation.get("visual_images", {}).get(key, [])
    pictures = {image["url"]: image for image in [*baseline, *approved] if image}
    if all_candidates:
        return list(pictures.values())
    role_order = {"artwork": 0, "video_frame": 1, "video_preview": 1, "portrait": 3}
    if item["kind"] == "video":
        role_order.update(artwork=1, video_frame=0, video_preview=0)
    ranked = sorted(pictures.values(), key=lambda image: role_order.get(image.get("role"), 2))
    art = [image for image in ranked if image.get("role") != "portrait"]
    return (art or ranked)[:1 if item["kind"] == "video" else 5]


def render_visual(picked, config, theme, now):
    header, ordered, cards, _ = issue_layout(picked, config, theme, None, now)
    output = []
    for candidate, card in zip(ordered, cards):
        item = candidate.item
        article = item.get("journal_article")
        custom = config.get("presentation", {}).get("cards", {}).get(candidate.key, {})
        author = (item.get("artist_radar") or {}).get("artist")
        feature = custom.get("feature_artist") or (author if card["heading"] == "🔎 Художник в поле зрения" else None)
        if feature:
            feature = config.get("presentation", {}).get("names", {}).get(feature, feature)
        summary = article["summary"] if article else next(
            (value for value in card["summaries"] if value and length(html.escape(value)) <= 200), "")
        section = "🎨 Художник в поле зрения" if feature and article else custom.get("section", card["heading"])
        checked = item.get("journal_visual")
        output.append({"section": section, "artist": feature if article else None, "title": card["title"],
                       "summary": summary, "article": bool(article), "byline": card["byline"],
                       "action": "Читать оригинал" if article else card["action"],
                       "actions": card["actions"],
                       "url": article["url"] if article else item.get("video_url") or item["url"],
                       "images": checked["images"] if checked else visual_assets(item, config),
                       "video_link": bool(checked and checked.get("video_link")),
                       "video": checked["video"] if checked else custom.get("native_video") if item["kind"] == "video" else None})
    return {"header": "\n".join(header), "cards": output}


def render(picked, config, theme=None, url_check=None, now=None):
    header, ordered, cards, date_text = issue_layout(picked, config, theme, url_check, now)
    credits = []
    for image in {card["image"]["url"]: card["image"] for card in cards if card["image"]}.values():
        license_name = image["license"]
        license_label = link(license_name, "https://creativecommons.org/licenses/by/4.0/") if license_name == "CC BY 4.0" else license_name
        credits.append(f"<i>{link(image['credit'], image['url'])} · {license_label}</i>")
    footer = [*credits, link("Сайт Art Radar", SITE)]

    def assemble(summaries):
        blocks = ["\n".join(header)]
        for card, summary in zip(cards, summaries):
            lines = [html.escape(card["heading"]), f"<b>{html.escape(card['title'])}</b>"]
            if summary:
                lines.append(html.escape(summary))
            lines.append(card["footer"])
            blocks.append("\n".join(lines))
        return "\n\n".join([*blocks, "\n".join(footer)])

    empty = assemble([""] * len(cards))
    remaining = PHOTO_CAPTION_LIMIT - length(empty)
    summaries = []
    for index, card in enumerate(cards):
        limit = min(170, remaining // (len(cards) - index) - 1)
        summary = next((option for option in card["summaries"] if option and length(html.escape(option)) <= limit), "")
        remaining -= length(html.escape(summary)) + bool(summary)
        summaries.append(summary)
    text = assemble(summaries)
    if length(text) > 4096:
        raise ValueError("Выпуск превышает лимит Telegram")
    cover_images = config.get("presentation", {}).get("cover_images", {})
    tiles = []
    for card, candidate in zip(cards, ordered):
        checked = candidate.item.get("journal_visual")
        image = next(iter(checked["images"]), None) if checked else cover_images.get(candidate.key, card["image"])
        tiles.append({"section": card["heading"].split(" ", 1)[-1], "title": card["title"], "image": image})
    cover = {"date": date_text, "tiles": tiles}
    return text, cover
