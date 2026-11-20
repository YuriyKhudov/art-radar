import datetime as dt
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from radar import (artist_pages, editorial, editorial_sources as sources, journal_articles, journal_cover, journal_visuals,
                   local_editor, telegram_journal as journal, visual_issue)

NOW = dt.datetime(2026, 11, 18, 19, tzinfo=journal.MOSCOW)
CONFIG = sources.read(sources.ROOT / "sources.json")
ARCHIVE = [item for item in sources.read(sources.ROOT / "materials.json") if item["type"] == "archive"]
CREDS = {"ART_RADAR_JOURNAL_PUBLISH": "1", "ART_RADAR_JOURNAL_FORMAT": "legacy", "JOURNAL_TELEGRAM_TOKEN": "example",
         "JOURNAL_TELEGRAM_CHAT": "-123", "JOURNAL_TELEGRAM_TOPIC": "456"}


def fresh(at=NOW):
    fixtures = [
        ("bazanova-rt", "Как писать небо маслом", "В уроке художница объясняет, как смешивает тёплые краски для облаков.",
         "video", f"https://rutube.ru/video/{'a' * 32}/", 540),
        ("tanr", "В Перми открылась выставка графики", "На выставке собрали рисунки пяти современных художников и отдельную серию офортов.",
         "article", "https://theartnewspaper.ru/posts/a", None),
        ("enterclass-rt", "Пленэр: работа над весенним этюдом", "Художник работает с натуры маслом и записывает весь процесс в своей мастерской.",
         "video", f"https://rutube.ru/video/{'b' * 32}/", 880),
        ("kulik-rt", "Интервью с художницей о новой серии работ", "В разговоре художница рассказывает о живописи и о работе над новой серией.",
         "video", f"https://rutube.ru/video/{'c' * 32}/", 1800),
    ]
    return [{"source": source, "url": url, "title": title, "evidence": evidence, "kind": kind,
             "published": at.isoformat(), "provenance": "Тестовый источник", **sources.duration(seconds)}
            for source, title, evidence, kind, url, seconds in fixtures]


class EditorialTest(unittest.TestCase):
    def test_ordinary_issue_uses_fresh_material_from_distinct_publishers(self):
        raw = fresh()
        same_publisher = {**raw[0], "source": "bazanova-yt", "url": "https://youtu.be/ntSZ6XWFb1A",
                          "title": "Урок акварели о смешении красок"}
        picked, issue = journal.prepare([*ARCHIVE, *raw, same_publisher], CONFIG, [], NOW, None, 1, verify=False)
        self.assertEqual(len(picked), 4)
        self.assertEqual(len({card.publisher for card in picked}), 4)
        self.assertEqual({card.direction for card in picked}, {"studio", "practice", "news", "people"})
        self.assertIn("<b>Art Radar · 18 ноября 2026</b>", issue["html"])
        self.assertIn(editorial.SLOGAN, issue["html"])
        self.assertIn("<b>Как писать небо маслом</b>", issue["html"])
        self.assertIn(">Смотреть на RUTUBE</a> · 9:00", issue["html"])
        self.assertIn(">Читать статью</a>", issue["html"])
        self.assertNotIn("Попробуйте", issue["html"])
        self.assertLessEqual(editorial.length(issue["html"]), editorial.PHOTO_CAPTION_LIMIT)
        self.assertEqual(len(issue["cover"]["tiles"]), 4)
        self.assertNotIn("url", issue["cover"])
        self.assertFalse(journal.prepare(ARCHIVE, CONFIG, [], NOW, None, 1, verify=False)[0])

    def test_presentation_leads_with_qualified_artist_and_ignores_truncated_excerpt(self):
        from dataclasses import replace

        picked, _ = editorial.assess(fresh(), CONFIG, NOW)
        original = picked[-1]
        info = {"stream": "priority", "artist": "Пример художника", "feature": "works"}
        featured = replace(original, item={**original.item, "artist_radar": info,
                                           "summary": "«Художник рассказывает о подробностях своей…»",
                                           "evidence": "Зрители обсуждают графическую серию и технику сухой иглы. Дальше в записи"},
                           score=90)
        text, cover = editorial.render([*picked[:-1], featured], CONFIG, now=NOW)
        self.assertEqual(cover["tiles"][0]["title"], featured.item["title"])
        self.assertIn("Зрители обсуждают графическую серию и технику сухой иглы.", text)
        self.assertNotIn("подробностях своей", text)
        self.assertNotIn("Дальше в записи", text)

    def test_cover_uses_only_licensed_originals(self):
        from PIL import Image

        body = io.BytesIO()
        Image.new("RGB", (20, 10), "#cccccc").save(body, "JPEG")
        licensed = {"url": "https://images.metmuseum.org/test.jpg", "license": "CC0", "credit": "Метрополитен-музей"}
        unlicensed = {**licensed, "license": "All rights reserved"}
        cover = {"date": "18 ноября 2026", "tiles": [
            {"section": "Искусство", "title": "Работы художника", "image": licensed},
            {"section": "Видео", "title": "Творческий процесс", "image": unlicensed}]}
        with patch.object(sources, "request", return_value=(body.getvalue(), "image/jpeg")) as request:
            picture, originals = journal_cover.compose(cover)
        request.assert_called_once()
        self.assertEqual(originals, 1)
        with Image.open(io.BytesIO(picture)) as composed:
            self.assertEqual(composed.format, "JPEG")
            self.assertEqual(composed.width, 1080)

    def test_cover_reflows_for_each_image_count(self):
        profiles = [[1.5], [1.29, .51], [1.3, 1, .8], [.8, 1.3, 1], [1.2, .9, 1.5, .8]]
        for profile in profiles:
            with self.subTest(profile=profile):
                boxes = journal_cover.photo_layout(profile, 1052)
                self.assertEqual(len(boxes), len(profile))
                for x, y, width, height in boxes:
                    self.assertGreater(width, 0)
                    self.assertGreater(height, 0)
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + width, 1052)
                for index, (x, y, width, height) in enumerate(boxes):
                    for other_x, other_y, other_w, other_h in boxes[index + 1:]:
                        self.assertTrue(x + width <= other_x or other_x + other_w <= x
                                        or y + height <= other_y or other_y + other_h <= y)

    def test_cover_only_images_do_not_change_the_message(self):
        picked, _ = editorial.assess(fresh(), CONFIG, NOW)
        baseline, _ = editorial.render(picked, CONFIG, now=NOW)
        asset = {"url": "https://upload.wikimedia.org/example.jpg", "license": "CC BY-SA 4.0",
                 "source": "https://commons.wikimedia.org/?curid=123", "credit": "Автор"}
        presentation = {**CONFIG.get("presentation", {}), "cover_images": {picked[0].key: asset}}
        text, cover = editorial.render(picked, {**CONFIG, "presentation": presentation}, now=NOW)
        self.assertEqual(text, baseline)
        self.assertEqual(cover["tiles"][0]["image"], asset)

    def test_classic_fallback_has_all_materials_in_one_message(self):
        picked, _ = editorial.assess(fresh(), CONFIG, NOW)
        issue = editorial.render_visual(picked, CONFIG, None, NOW)
        issue["cards"][0]["images"] = [{"url": "https://images.metmuseum.org/example.jpg", "license": "CC0", "credit": "Музей"}]
        with patch.object(journal, "visual_photos") as load:
            [(method, data, files)] = journal.visual_packets(visual_issue.make_plan(issue, "classic"), "42", None, CONFIG)
        load.assert_not_called()
        self.assertEqual(method, "sendMessage")
        self.assertFalse(files)
        self.assertEqual(data["disable_notification"], "true")
        self.assertIn("Музей", data["text"])
        for card in issue["cards"]:
            self.assertIn(editorial.link(card["title"], card["url"]), data["text"])

    def test_single_visual_issue_has_media_and_a_button_per_card(self):
        picked, _ = editorial.assess(fresh(), CONFIG, NOW)
        issue = editorial.render_visual(picked, CONFIG, None, NOW)
        asset = {"url": "https://images.metmuseum.org/example.jpg", "license": "CC0", "credit": "Музей"}
        images = [[(asset, b"jpg"), (asset, b"jpg2")], [(asset, b"jpg")], [], [(asset, b"jpg")]]
        with patch.object(journal, "visual_photos", side_effect=images):
            [(method, data, files)] = journal.visual_packets(visual_issue.make_plan(issue, "rich"), "42", None,
                                                            {**CONFIG, "visual_selection": False})
        self.assertEqual(method, "sendRichMessage")
        self.assertEqual(len(files), 4)
        self.assertEqual(data["rich_message"].count("<tg-button type="), 4)
        self.assertEqual(data["rich_message"].count('tg://photo?id='), 4)
        self.assertEqual(data["rich_message"].count("<hr/>"), 3)
        self.assertEqual(data["rich_message"].count("<tg-collage>"), 1)
        self.assertNotIn("<tg-slideshow>", data["rich_message"])
        self.assertNotIn("Б ·", data["rich_message"])
        for card in issue["cards"]:
            self.assertIn(f'<b>{editorial.link(card["title"], card["url"])}</b>',
                          json.loads(data["rich_message"])["html"])

    def test_mosaic_keeps_each_work_and_protects_extreme_aspect_ratios(self):
        figures = [(str(i), {"aspect": ratio}) for i, ratio in enumerate((1.2, .73, 1.4, 1.1, 3.0))]
        rows = visual_issue.mosaic_rows(figures)
        self.assertEqual([[identifier for identifier, _ in row] for row in rows], [["1", "0", "2", "3"], ["4"]])

    def test_verified_platform_buttons_share_a_native_preview_row(self):
        rutube = fresh()[0]["url"]
        youtube = "https://www.youtube.com/watch?v=Lt4pX0ZhjsQ"
        card = {"title": "Как писать небо маслом", "section": "В мастерской", "summary": "", "byline": "",
                "url": rutube, "action": "Смотреть на RUTUBE", "images": [], "video_link": True,
                "actions": editorial.video_actions({"url": rutube, "verified_video_urls": [youtube]})}
        painting = {**card, "video_link": False, "url": "https://example.org/article", "article": True,
                    "action": "Читать оригинал", "actions": []}
        plan = visual_issue.make_plan({"header": "Art Radar", "cards": [card, painting]}, "rich")
        packets = journal.visual_packets(plan, "42", None, {**CONFIG, "visual_selection": False})
        self.assertEqual([method for method, _, _ in packets], ["sendMessage", "sendRichMessage"])
        options = json.loads(packets[0][1]["link_preview_options"])
        self.assertEqual(options, {"url": youtube, "prefer_large_media": True, "show_above_text": True})
        row, = json.loads(packets[0][1]["reply_markup"])["inline_keyboard"]
        self.assertEqual([button["text"] for button in row], ["Смотреть на RUTUBE", "Смотреть на YouTube"])
        self.assertEqual(visual_issue.rich_html("", [card], [[]]).count("<tg-button type="), 2)

    def test_crossposts_require_same_title_owner_duration_and_subject(self):
        ru = fresh()[0]
        yt = {**ru, "source": "bazanova-yt", "url": "https://www.youtube.com/watch?v=Lt4pX0ZhjsQ"}
        other = {**yt, "url": "https://www.youtube.com/watch?v=ntSZ6XWFb1A", "seconds": 840}
        sources.crossposts([ru, yt, other], CONFIG)
        self.assertEqual(ru["related_urls"], [yt["url"]])
        self.assertEqual(yt["related_urls"], [ru["url"]])
        self.assertNotIn("related_urls", other)
        different = {**yt, "evidence": "Этот автор отдельно обсуждает новую выставку графики и показывает офорты в галерее."}
        sources.crossposts([ru, different], CONFIG)
        self.assertNotIn("related_urls", ru)

    def test_editor_rejects_repeated_price_and_copied_headline(self):
        self.assertTrue(journal_articles.repeated_fact("Аукцион завершился продажей за $85,8 млн.",
                                                       "Итоговая цена картины составила $85.8 млн с учётом комиссии."))
        self.assertFalse(journal_articles.repeated_fact("Аукцион завершился продажей за $85,8 млн.",
                                                        "Художник написал картину в Веймаре в период работы в Баухаусе."))
        self.assertIsNone(journal_articles.generated_title({"title": "Работа Кандинского установила рекорд"},
                                                           "Работа Кандинского установила рекорд", ""))

    def test_visual_rules_require_art_that_matches_the_material(self):
        building = {"role": "venue", "credit": "Фасад музея"}
        painting = {"role": "artwork", "credit": "Портрет неизвестной, масло на холсте"}
        photograph = {"role": "art_photograph", "credit": "Фотография из серии «Память»"}
        rooms = {"role": "interior", "credit": "Пустой зал галереи"}
        display = {"role": "exhibition_view", "credit": "Вид экспозиции с работами участников"}
        for topic in ("artist", "event", "fair", "exhibition", "photography"):
            self.assertIsNone(journal_visuals.role_for(building, topic))
            self.assertIsNone(journal_visuals.role_for(rooms, topic))
        self.assertEqual(journal_visuals.role_for(building, "architecture"), "venue")
        self.assertEqual(journal_visuals.role_for(painting, "fair"), "artwork")
        self.assertEqual(journal_visuals.role_for(painting, "artist"), "artwork")
        self.assertEqual(journal_visuals.role_for(display, "event"), "exhibition_view")
        self.assertEqual(journal_visuals.role_for(photograph, "photography"), "art_photograph")
        self.assertIsNone(journal_visuals.role_for(painting, "photography"))
        self.assertIsNone(journal_visuals.role_for({"role": "artwork", "credit": "Логотип музея"}, "artist"))
        card = SimpleNamespace(item={"kind": "article", "title": "Выставка открылась в музее архитектуры"}, direction="news")
        self.assertEqual(journal_visuals.subject(card, {}), "exhibition")

    def test_art_selection_excludes_facades_before_editing_and_uses_other_candidates(self):
        raw = fresh()
        rejected = raw[1]
        valid = {"subject": "video", "reason": "", "images": [{"url": "https://images.metmuseum.org/photo.jpg",
                "license": "CC0", "credit": "Изображение", "role": "video_frame", "sha256": "b" * 64}], "video": None, "quality": 1}
        refused = {"subject": "exhibition", "images": [], "video": None, "quality": 0,
                   "reason": "Нет подходящего изображения с подтверждёнными правами"}
        visuals = Mock()
        visuals.check.side_effect = lambda card: refused if card.key == sources.canonical(rejected["url"]) else valid
        article_editor = Mock()
        picked, issue = journal.prepare(raw, CONFIG, [], NOW, None, 1, verify=False,
                                        article_checks=article_editor, visual_checks=visuals)
        article_editor.check.assert_not_called()
        self.assertEqual(len(picked), 3)
        row = next(row for row in issue["items"] if row["url"] == rejected["url"])
        self.assertEqual(row["decision"], refused["reason"])
        self.assertTrue(journal.verified_visual_issue(issue["visual"]))
        self.assertTrue(all(card["images"] == valid["images"] for card in issue["visual"]["cards"]))

    def test_publisher_media_requires_explicit_license_and_is_scoped_to_the_article(self):
        url = "https://example.org/article"
        directive = {"@type": "Article", "url": url, "license": "https://creativecommons.org/licenses/by/4.0/",
                     "image": [{"@id": "#art"}, {"@type": "ImageObject", "url": "/no-grant.jpg", "creditText": "Художник",
                               "caption": "Холст, масло"}]}
        graph = {"@graph": [directive, {"@id": "#art", "@type": "ImageObject", "contentUrl": "/art.jpg",
                                      "creator": {"name": "Художник"}, "caption": "Акварель на бумаге",
                                      "license": "https://creativecommons.org/licenses/by-sa/4.0/"},
                             {**directive, "url": "https://example.org/unrelated"}]}
        page = artist_pages.parse('<script type="application/ld+json">' + json.dumps(graph) + '</script>')
        [asset] = journal_visuals.publisher_images(page, url, ["example.org"])
        self.assertEqual(asset["url"], "https://example.org/art.jpg")
        self.assertEqual(asset["license"], "CC BY-SA 4.0")
        self.assertEqual(journal_visuals.role_for(asset, "exhibition"), "artwork")

    def test_visual_check_technique_requires_both_process_and_result(self):
        card = SimpleNamespace(item={"url": "https://example.org/lesson", "kind": "article", "title": "Урок живописи"},
                               source={"domains": ["example.org"]}, key="https://example.org/lesson", direction="practice")
        asset = {"url": "https://example.org/result.jpg", "role": "technique_result", "license": "CC0", "credit": "Картина"}
        process = {**asset, "role": "creative_process", "url": "https://example.org/process.jpg", "credit": "Работа над картиной"}
        cfg = {"presentation": {"visual_images": {card.key: [asset]}}, "image_domains": ["example.org"]}

        def downloaded(assets):
            return [({**item, "width": 1200, "height": 800, "sha256": hashlib.sha256(item["url"].encode()).hexdigest()}, b"img")
                    for item in assets]

        checks = journal_visuals.VisualChecks(cfg, downloaded, lambda _: None)
        self.assertIn("процесса и результата", checks.check(card)["reason"])
        cfg["presentation"]["visual_images"][card.key].append(process)
        checks = journal_visuals.VisualChecks(cfg, downloaded, lambda _: None)
        self.assertEqual({item["role"] for item in checks.check(card)["images"]}, {"creative_process", "technique_result"})
        unreadable = journal_visuals.VisualChecks(cfg, lambda assets: [], lambda _: None)
        self.assertIn("файла", unreadable.check(card)["reason"])

    def test_delivery_cannot_silently_drop_a_verified_attachment(self):
        card = {"url": "https://example.org/entry", "images": [{"sha256": "a" * 64}], "video": None}
        plan = [{"kind": "rich", "header": "Art Radar", "cards": [card]}]
        with patch.object(journal, "visual_photos", return_value=[]):
            with self.assertRaisesRegex(ValueError, "изменились или недоступны"):
                journal.visual_packets(plan, "42", None, CONFIG)

    def test_text_card_keeps_editorial_paragraphs_and_a_small_source_link(self):
        card = {"section": "Художник", "artist": "Валентин Серов", "title": "Приёмы портрета", "article": True,
                "summary": "Первый абзац.\n\nВторой абзац.", "byline": "Arzamas", "action": "Читать оригинал",
                "url": "https://arzamas.academy/example"}
        body = visual_issue.rich_html("Art Radar", [card], [[]])
        self.assertIn("<b>Валентин Серов</b>", body)
        self.assertIn("Первый абзац.</p><p>Второй абзац.", body)
        self.assertIn('href="https://arzamas.academy/example">Читать оригинал</a>', body)
        self.assertNotIn("<tg-button", body)

    def test_foreign_article_requires_a_full_russian_edition_before_synthesis(self):
        from types import SimpleNamespace

        foreign = "https://example.org/full"
        read = Mock(return_value=None)
        model = Mock()
        item = SimpleNamespace(key=foreign, item={"url": foreign, "title": "Художник", "ru_editorial": True},
                               source={"lang": "en", "domains": ["example.org"]})
        config = {"article_domains": ["example.ru"]}
        checks = journal_articles.ArticleChecks(config, read=read, model=model)
        self.assertIn("русская версия", checks.check(item)["reason"])
        read.assert_not_called()
        config["russian_editions"] = {foreign: "https://example.ru/translation"}
        translated = journal_articles.ArticleChecks(config, read=read, model=model)
        self.assertIn("Полный открытый", translated.check(item)["reason"])
        read.assert_called_once_with("https://example.ru/translation", ["example.ru"], {})
        model.complete.assert_not_called()

    def test_automatic_summary_does_not_change_an_artists_name(self):
        original = ["В программу вошли работы Александры Гарт, которые будут представлены на стенде галереи.",
                    "На специальном показе издательство представит графику, изготовленную методом шелкографии, и познакомит посетителей с работой мастерской."]
        draft = {"paragraphs": [
            {"text": "Александр Гарт представлен серией работ, отобранных для отдельного стенда художественной галереи.", "evidence": [1]},
            {"text": "Отдельная часть события знакомит зрителей с мастерской и с тем, как издатели используют шелкографию в выставочных проектах.", "evidence": [2]}]}
        self.assertIsNone(journal_articles.verified_summary(draft, original, exact_names=True))

    def test_common_noun_before_surname_is_not_a_changed_given_name(self):
        self.assertTrue(journal_articles.names_preserved("Картина Серова меняет характер парадного портрета.",
                                                         "В работе Валентина Серова есть важная деталь."))
        self.assertFalse(journal_articles.names_preserved("Михаил Репин строит композицию вокруг двери.",
                                                          "Художник Илья Репин выстраивает сцену вокруг двери."))

    def test_confirmed_work_title_is_not_counted_as_copied_prose(self):
        title = "Весна пришла и растопила снег на деревьях и крышах города"
        confirmed = {tuple(journal_articles.words(title))}
        source = f"Критик обратился к картине «{title}» и сопоставил её с соседней работой."
        draft = f"Цветовые пятна особенно заметны в картине «{title}», когда взгляд переводят от неба к крышам."
        self.assertTrue(journal_articles.prose_ngrams(source, set()) & journal_articles.prose_ngrams(draft, set()))
        self.assertFalse(journal_articles.prose_ngrams(source, confirmed) & journal_articles.prose_ngrams(draft, confirmed))

    def test_cloudflare_requests_only_the_selected_model_with_strict_json(self):
        body = io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": '{"supported": true, "problems": []}'}}]}).encode())
        body.headers = {}
        editor = local_editor.CloudflareEditor("test-key", "a" * 32)
        editor.http.open = Mock(return_value=body)
        schema = journal_articles.CHECK_SCHEMA
        self.assertEqual(editor.complete("Проверь", {"summary": "Текст"}, 220, schema),
                         {"supported": True, "problems": []})
        request = editor.http.open.call_args.args[0]
        data = json.loads(request.data)
        self.assertEqual(data["model"], "@cf/google/gemma-4-26b-a4b-it")
        self.assertEqual(request.full_url, f"https://api.cloudflare.com/client/v4/accounts/{'a' * 32}/ai/v1/chat/completions")
        self.assertFalse(data["chat_template_kwargs"]["enable_thinking"])
        self.assertTrue(data["response_format"]["json_schema"]["strict"])

    def test_cloudflare_stops_new_requests_after_quota_error(self):
        editor = local_editor.CloudflareEditor("test-key", "a" * 32)
        editor.http.open = Mock(side_effect=urllib.error.HTTPError("https://api.cloudflare.com", 429, "test", {},
                                                                  io.BytesIO(b'{"errors":[{"code":3036}]}')))
        for _ in range(2):
            self.assertIsNone(editor.complete("Проверь", {}, 220, journal_articles.CHECK_SCHEMA))
        self.assertEqual(editor.stop_reason, "quota-exhausted")
        editor.http.open.assert_called_once()

    def test_ready_article_is_reused_when_cloudflare_quota_is_exhausted(self):
        pool = fresh()
        candidates, _ = editorial.assess(pool, CONFIG, NOW)
        article = next(card for card in candidates if card.item["kind"] == "article")
        prepared = {"summary": "Проверенный текст из резерва.", "url": article.item["url"],
                    "verification": local_editor.CLOUDFLARE_MODEL}
        model = local_editor.CloudflareEditor("test-key", "a" * 32)
        model.stop_reason = "quota-exhausted"
        model.http.open = Mock()
        reader = Mock()
        checks = journal_articles.ArticleChecks(CONFIG, model=model, read=reader)
        with closing(sqlite3.connect(":memory:")) as connection:
            saved = journal.Journal(connection)
            saved.remember("article", article, CONFIG, prepared)
            picked, result = journal.prepare(pool, CONFIG, [], NOW, None, 1, False, article_checks=checks, journal=saved)
        self.assertTrue(picked)
        self.assertTrue(any(card.get("summary") == prepared["summary"] for card in result["visual"]["cards"]))
        reader.assert_not_called()
        model.http.open.assert_not_called()

    def test_article_retries_only_twice_and_sends_facts_instead_of_source_paragraphs(self):
        from types import SimpleNamespace

        original = [
            "Валентин Серов написал портрет Веры Мамонтовой в Абрамцеве в 1887 году. Девочка позировала художнику несколько недель.",
            "Третьяковская галерея показала портрет в зале русской живописи. Музей представил картины автора рядом с его графикой.",
        ]
        url = "https://example.org/article"
        article = SimpleNamespace(key=url, item={"url": url, "title": "Картины Валентина Серова"},
                                  source={"lang": "ru", "domains": ["example.org"]})
        model = Mock()
        model.complete.return_value = None
        editor = journal_articles.ArticleChecks({"article_domains": ["example.org"]}, model=model, read=lambda *_: original)
        result = editor.check(article)
        self.assertIn("reason", result)
        self.assertEqual(model.complete.call_count, 2)
        for call in model.complete.call_args_list:
            packet = call.args[1]
            self.assertEqual(set(packet["facts"]), {1, 2})
            self.assertEqual(packet["facts"][1][0]["action"], "написал")
            self.assertNotIn("article", packet)
            self.assertTrue(all(paragraph not in json.dumps(packet, ensure_ascii=False) for paragraph in original))

    def test_fact_extraction_ignores_reported_speech_and_artistic_hypotheses(self):
        titles = {("не", "ждали")}
        self.assertIsNone(journal_articles.single_fact("В мясных лавках продают крыс, писал Мане своей знакомой.", titles))
        self.assertIsNone(journal_articles.single_fact("Серов, вероятно, написал портрет для музейной выставки.", titles))
        self.assertEqual(journal_articles.single_fact("Лицо героя Репин переписывал четыре раза.", titles),
                         {"before": ["Лицо героя Репин"], "action": "переписывал", "after": ["четыре раза"]})

    def test_transmission_switches_to_classic_only_for_explicit_unsupported_response(self):
        rich = ("sendRichMessage", {}, [])
        classic = ("sendMessage", {}, [])
        with (patch.object(journal, "send_request", side_effect=[journal.TelegramRejected("404", 404), 42]) as send,
              redirect_stdout(io.StringIO())):
            self.assertEqual(journal.transmit_visual(rich, None, "token", classic), 42)
        self.assertEqual([call.args[0] for call in send.call_args_list], ["sendRichMessage", "sendMessage"])
        with patch.object(journal, "send_request", side_effect=RuntimeError("timeout")) as send:
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                journal.transmit_visual(rich, None, "token", classic)
            send.assert_called_once()

    def test_recency_boundary_and_marketing(self):
        raw = fresh()
        promo = {**raw[0], "title": "Цветная пятница: скидки до 70% на уроки рисования"}
        teaser = {**raw[0], "seconds": 70, "access_evidence": "Смотрите полный выпуск в профиле"}
        old = {**raw[1], "published": (NOW - dt.timedelta(days=8)).isoformat()}
        future = {**raw[2], "published": (NOW + dt.timedelta(minutes=10)).isoformat()}
        picked, report = editorial.assess([promo, teaser, old, future], CONFIG, NOW)
        self.assertFalse(picked)
        self.assertEqual([r["decision"] for r in report[:2]], ["Рекламный или партнёрский материал"] * 2)
        self.assertIn("семидневного", report[2]["decision"])
        self.assertIn("ещё не наступила", report[3]["decision"])

    def test_monthly_history_prefers_other_publishers_for_similar_cards(self):
        raw = fresh()
        rival = {**raw[0], "source": "rizhkin-rt", "url": "https://rutube.ru/video/new/",
                 "title": "Урок живописи с кистью и красками"}
        history = [{**raw[0], "title": f"Старый {i} выпуск из мастерской", "direction": "practice",
                    "url": f"https://rutube.ru/video/old{i}/", "_issued_at": NOW.isoformat()} for i in range(5)]
        candidates, _ = editorial.assess([raw[0], rival], CONFIG, NOW)
        seen = []
        editorial.select(candidates, CONFIG, verify=lambda card: seen.append(card.publisher) or True,
                         history=history, now=NOW)
        self.assertEqual(seen[0], "rizhkin")

    def test_age_deadline_and_unknown_captions(self):
        article = {**ARCHIVE[0], "type": "news", "published": "2026-10-01T12:00:00+03:00"}
        expired = {**ARCHIVE[1], "deadline": "2026-11-17"}
        video = {**next(item for item in ARCHIVE if item["lang"] == "en"), "kind": "video"}
        selected, report = editorial.assess([article, expired, video], CONFIG, NOW)
        self.assertEqual(selected, [])
        self.assertIn("Материал вне семидневного", report[0]["decision"])
        self.assertIn("Срок подачи", report[1]["decision"])
        self.assertIn("не проверено", report[2]["decision"])

    def test_link_failures_excluded_and_recorded(self):
        broken, *other_items = fresh()
        with patch.object(journal, "check", side_effect=lambda url, domains, **kw: url != broken["url"]):
            picked, report = journal.prepare([broken, *other_items], CONFIG, [], NOW, None, 1, verify=True)
        self.assertGreaterEqual(len(picked), 3)
        self.assertTrue(any(item["url"] == broken["url"] and "сетевую" in item["decision"]
                            for item in report["items"]))

    def test_normalized_links_title_fingerprint_and_story_alias(self):
        self.assertEqual(sources.canonical("https://www.example.com/a/?utm_medium=rss#part"), "https://example.com/a")
        self.assertEqual(sources.canonical("https://youtu.be/ntSZ6XWFb1A?si=track"),
                         sources.canonical("https://www.youtube.com/watch?v=ntSZ6XWFb1A"))
        item = {**ARCHIVE[0], "title": "Музей показывает рисунки Петрова из ранних альбомов", "story": "retrospective"}
        old = {**item, "title": "Музей показывает рисунки Петрова из ранних альбомов впервые",
               "url": ARCHIVE[1]["url"], "story": None}
        other_language = {**old, "title": "Petrov's notebooks", "story": "retrospective"}
        alias = {**other_language, "story": None, "related_urls": [item["url"]]}
        for variant in [old, other_language, alias]:
            with self.subTest(variant=variant):
                cards, report = editorial.assess([item], CONFIG, NOW, [variant])
                self.assertFalse(cards)
                self.assertIn("уже опубликован", report[0]["decision"])

    def test_title_only_and_advertising_are_held(self):
        article = {"source": "tanr", "url": "https://theartnewspaper.ru/posts/test", "title": "Реставрация картины",
                   "kind": "article"}
        ad = {**article, "evidence": "Партнёрский материал о живописи. " * 5}
        _, report = editorial.assess([article, ad], CONFIG, NOW)
        self.assertEqual([row["decision"] for row in report],
                         ["Недостаточно содержательного описания источника", "Рекламный или партнёрский материал"])

    def test_rejects_prior_editorial_regressions_and_admits_meaningful_short(self):
        base = fresh()[0]
        cases = [
            {**base, "title": "Итоги года без мужа 🍁 Влог + рисование"},
            {**base, "title": "Рисуем кресло в Procreate"},
            {**base, "title": "Торжественное открытие выставки «Осень 2026»"},
            sources.entry({"id": base["source"]}, url=base["url"], title=base["title"], key="promo", kind="video",
                          text="В кадре кисти и эскиз платья.\nРаспродажа курса открыта до пятницы, мест осталось мало.\n"
                               "Введите промокод при оплате и получите скидку на все уроки.",
                          date=NOW.isoformat(), origin="Тестовый источник"),
            {**base, "seconds": 51, "duration": "0:51", "title": "Неодинаковость. Фрагмент из курса «Гуашь и солнце»",
             "access_evidence": "Полностью курс художника по ссылке. Симметрия и похожесть мазков портят рисунок."},
        ]
        picked, report = editorial.assess(cases, CONFIG, NOW)
        self.assertFalse(picked)
        self.assertEqual([row["decision"] for row in report], [
            "Личный сюжет занимает главное место", "Упражнение по цифровому инструменту вне основной темы",
            "Церемония вместо разбора искусства", "Рекламный или партнёрский материал", "Рекламный или партнёрский материал"])
        short = {**base, "seconds": 29, "duration": "0:29", "title": "Мазок мастихином по сырому маслу",
                 "evidence": "В кадре художница показывает, как одним мазком положить свет на полотно мастихином."}
        unknown = {key: value for key, value in short.items() if key not in {"seconds", "duration"}}
        for item in [short, unknown]:
            cards, _ = editorial.assess([item], CONFIG, NOW)
            self.assertEqual(len(cards), 1)
        text, _ = editorial.render(editorial.assess([unknown], CONFIG, NOW)[0], CONFIG)
        self.assertNotIn("длительность", text)

    def test_verified_selection_replays_without_rechecking_network(self):
        urls = fresh()
        network = Mock(side_effect=lambda url, domains, image: url != urls[0]["url"])
        first = journal.LinkChecks(verify=network)
        initial = journal.prepare(urls, CONFIG, [], NOW, None, 1, verify=first)[1]
        self.assertGreaterEqual(network.call_count, 3)
        second = journal.LinkChecks(replay=first.snapshot(), verify=Mock(side_effect=AssertionError))
        self.assertEqual(journal.prepare(urls, CONFIG, [], NOW, None, 1, verify=second)[1], initial)

    def test_uses_artistic_chapters_instead_of_opening_pitch(self):
        item = {**fresh()[0], "evidence": "Посмотрите видео, о котором я рассказывала в ролике! "
                                        "00:00 — Как выстраивать колорит в серой и монохромной природе? "
                                        "01:00 — Внутреннее ощущение цвета и тональные отношения больших масс."}
        picked, _ = editorial.assess([item], CONFIG, NOW)
        self.assertIn("тональные отношения", picked[0].item["summary"])
        self.assertNotIn("Посмотрите видео", picked[0].item["summary"])

    def test_rutube_reads_published_time_duration_and_ignores_paid_or_other_author(self):
        record = {"id": "0123456789abcdef0123456789abcdef", "title": "Урок живописи",
                  "description": "Подпишитесь на соцсети\nВ уроке показана работа художника с тремя оттенками красного.",
                  "author": {"id": 99}, "publication_ts": "2026-11-18T15:01:00",
                  "created_ts": "2020-01-01T15:01:00", "duration": 3726}
        rows = list(sources.rutube_entries({"results": [record, {**record, "is_paid": True},
                                                       {**record, "author": {"id": 91}}]},
                                           {"id": "rt", "channel": 99}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration"], "1:02:06")
        self.assertEqual(rows[0]["published"], "2026-11-18T15:01:00+03:00")
        self.assertNotIn("Подпишитесь", rows[0]["evidence"])
        self.assertEqual(sources.duration("PT1H2M6S"), {"seconds": 3726, "duration": "1:02:06"})

    def test_feed_is_present_on_its_own_domain(self):
        raw = b"""<rss version="2.0"><channel>
        <item><title>Artwork</title><link>https://example.org/work</link>
        <guid>abc</guid><pubDate>Wed, 18 Nov 2026 15:00:00 +0000</pubDate></item>
        <item><title>Foreign link</title><link>https://untrusted.invalid/work</link></item>
        </channel></rss>"""
        rows = list(sources.feed_entries(raw, {"id": "example", "domains": ["example.org"]}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["feed_id"], "abc")
        self.assertEqual(rows[0]["published"], "2026-11-18T15:00:00+00:00")


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "journal.sqlite3"
        self.config = {**CONFIG, "theme_every": 0, "article_domains": [], "visual_selection": False}
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def store(self):
        return closing(sqlite3.connect(self.path))

    def test_schedule_uses_local_day_and_time(self):
        last = "2026-11-15T20:04:00+03:00"
        hour = dt.time(19)
        self.assertFalse(journal.due(NOW - dt.timedelta(minutes=1), last, hour, 3))
        self.assertFalse(journal.due(NOW - dt.timedelta(days=1), last, hour, 3))
        self.assertTrue(journal.due(NOW.astimezone(dt.timezone.utc), last, hour, 3))
        self.assertFalse(journal.due(NOW, last, hour, 3, "2026-11-18"))

    def test_daily_storage_survives_failed_feeds(self):
        article = {**ARCHIVE[0], "url": "https://arzamas.academy/mag/487-serov?utm_source=test"}
        success = ([article], [{"source": "arzamas", "status": "ok", "count": 1}])
        error = ([], [{"source": "arzamas", "status": "TimeoutError", "count": 0}])
        with self.store() as conn, patch.object(sources, "collect", side_effect=[success, error, success]) as collect:
            state = journal.Journal(conn)
            self.assertTrue(state.daily(self.config, NOW))
            self.assertFalse(state.daily(self.config, NOW))
            self.assertTrue(state.daily(self.config, NOW + dt.timedelta(days=1)))
            self.assertEqual(state.setting("collected_on"), "2026-11-18")
            self.assertEqual(state.items(), [article])
            self.assertTrue(state.daily(self.config, NOW + dt.timedelta(days=1)))
            self.assertEqual(len(state.items()), 1)
            self.assertEqual(collect.call_count, 3)

    def test_success_persists_state_before_send_and_blocks_restarts(self):
        def send(*_):
            with self.store() as conn:
                self.assertIsNotNone(journal.Journal(conn).clock()[2])
            self.assertEqual(persist.call_count, 2)
            return 123

        persist = Mock()
        with (patch.dict(journal.os.environ, CREDS, clear=True), patch.object(journal, "now_moscow", return_value=NOW),
              patch.object(sources, "collect", return_value=([], [])), patch.object(journal, "check", return_value=True),
              patch.object(journal, "send", side_effect=send) as transport):
            for _ in range(2):
                with self.store() as conn:
                    journal.scheduled(journal.Journal(conn), self.config, fresh(), persist)
            with self.store() as conn:
                state = journal.Journal(conn)
                self.assertEqual(state.clock(), (NOW.isoformat(), 1, None))
                self.assertGreaterEqual(len(state.history()), 3)
            transport.assert_called_once()

    def test_ambiguous_delivery_blocks_retry_until_reconciled(self):
        with (patch.dict(journal.os.environ, CREDS, clear=True), patch.object(journal, "now_moscow", return_value=NOW) as clock,
              patch.object(sources, "collect", return_value=([], [])), patch.object(journal, "check", return_value=True),
              patch.object(journal, "send", side_effect=RuntimeError("Timeout")) as transport):
            with self.store() as conn, self.assertRaises(RuntimeError):
                journal.scheduled(journal.Journal(conn), self.config, fresh(), Mock())
            clock.return_value = NOW + dt.timedelta(days=4)
            with self.store() as conn:
                state = journal.Journal(conn)
                journal.scheduled(state, self.config, fresh(), Mock())
                state.resolve(state.clock()[2], 130)
                self.assertEqual(state.clock(), (NOW.isoformat(), 1, None))
            transport.assert_called_once()

    def test_disabled_publish_does_not_need_token_or_touch_telegram(self):
        with (self.store() as conn, patch.dict(journal.os.environ, {}, clear=True),
              patch.object(sources, "collect", return_value=([], [])), patch.object(journal, "send") as transport):
            state = journal.Journal(conn)
            journal.scheduled(state, self.config, fresh(), Mock())
            self.assertEqual(state.clock(), (None, 0, None))
            transport.assert_not_called()

    def test_scheduled_uses_one_rich_message_by_default(self):
        env = {name: value for name, value in CREDS.items() if name != "ART_RADAR_JOURNAL_FORMAT"}
        with (self.store() as conn, patch.dict(journal.os.environ, env, clear=True),
              patch.object(journal, "now_moscow", return_value=NOW),
              patch.object(sources, "collect", return_value=([], [])), patch.object(journal, "check", return_value=True),
              patch.object(journal, "send_request", return_value=101) as transport, redirect_stdout(io.StringIO())):
            state = journal.Journal(conn)
            journal.scheduled(state, self.config, fresh(), Mock())
            journal.scheduled(state, self.config, fresh(), Mock())
            transport.assert_called_once()
            self.assertEqual(transport.call_args.args[0], "sendRichMessage")
            self.assertEqual(state.clock(), (NOW.isoformat(), 1, None))

    def test_group_preflight_checks_existing_credentials_without_sending(self):
        env = {"TELEGRAM_BOT_TOKEN": "bot", "TELEGRAM_CHAT_ID": "-123", "TELEGRAM_THREAD_ID": "456"}
        responses = [{"id": -123, "type": "supergroup", "is_forum": True}, {"id": 10}, {"status": "administrator"}]
        persisted = Mock()
        with (patch.dict(journal.os.environ, env, clear=True),
              patch.object(journal, "telegram_info", side_effect=responses) as api,
              patch.object(journal, "send_request") as send, redirect_stdout(io.StringIO())):
            journal.prepare_group(persisted)
            self.assertEqual([call.args[1] for call in api.call_args_list], ["getChat", "getMe", "getChatMember"])
            persisted.assert_called_once()
            send.assert_not_called()

    def test_empty_selection_skips_day_and_never_tries_to_catch_up(self):
        with (self.store() as conn, patch.dict(journal.os.environ, CREDS, clear=True),
              patch.object(journal, "now_moscow", return_value=NOW) as clock,
              patch.object(sources, "collect", return_value=([], [])), patch.object(journal, "check", return_value=True),
              patch.object(journal, "send", return_value=123) as transport):
            state = journal.Journal(conn)
            journal.scheduled(state, self.config, [], Mock())
            journal.scheduled(state, self.config, fresh(), Mock())
            transport.assert_not_called()
            clock.return_value = NOW + dt.timedelta(days=9)
            journal.scheduled(state, self.config, fresh(clock.return_value), Mock())
            transport.assert_called_once()

    def test_telegram_error_never_prints_secret(self):
        with patch.object(journal.urllib.request, "urlopen", side_effect=urllib.error.URLError("hidden-token")):
            with self.assertRaisesRegex(RuntimeError, "^Telegram не подтвердил"):
                journal.send({"html": "Испытание", "cover": None}, "hidden-token", "-1", "1")

    def test_private_delivery_reuses_bot_credentials_has_no_topic_and_does_not_repeat(self):
        test_db = self.path.with_name("private-test.sqlite3")
        payload = {"issues": [{"html": "<b>Тест</b>", "cover": {"native": True, "url": "https://example.org/art"}}],
                   "verified_links": True}
        env = {"TELEGRAM_BOT_TOKEN": "existing-token", "ART_RADAR_TEST_CHAT_ID": "42", "TELEGRAM_CHAT_ID": "-123"}

        def fake_telegram(request, timeout):
            self.assertIn("/botexisting-token/", request.full_url)
            self.assertIn(b"chat_id=42", request.data)
            self.assertNotIn(b"message_thread_id", request.data)
            if request.full_url.endswith("/getChat"):
                return io.BytesIO(b'{"ok":true,"result":{"id":42,"type":"private"}}')
            self.assertTrue(request.full_url.endswith("/sendMessage"))
            self.assertIn(b"link_preview_options", request.data)
            self.assertIn(b"parse_mode=HTML", request.data)
            with closing(sqlite3.connect(test_db)) as check:
                self.assertEqual(check.execute("SELECT telegram_id FROM test_deliveries").fetchall(), [(None,)])
            return io.BytesIO(b'{"ok":true,"result":{"message_id":541}}')

        with self.store() as conn:
            state = journal.Journal(conn)
            before = state.clock()
            with (patch.dict(journal.os.environ, env, clear=True),
                  patch.object(journal.urllib.request, "urlopen", side_effect=fake_telegram) as transport):
                journal.test_delivery(payload, test_db, Mock())
                with self.assertRaisesRegex(ValueError, "уже был отправлен"):
                    journal.test_delivery(payload, test_db, Mock())
            self.assertEqual(transport.call_count, 3)
            self.assertEqual(state.clock(), before)
        with closing(sqlite3.connect(test_db)) as conn:
            self.assertEqual(conn.execute("SELECT telegram_id FROM test_deliveries").fetchall(), [(541,)])

    def test_private_delivery_fails_closed_for_missing_or_wrong_chat(self):
        payload = {"issues": [{"html": "Тест", "cover": None}], "verified_links": True}
        with patch.dict(journal.os.environ, {"TELEGRAM_BOT_TOKEN": "existing-token"}, clear=True):
            with patch.object(journal.urllib.request, "urlopen") as transport:
                with self.assertRaisesRegex(ValueError, "ART_RADAR_TEST_CHAT_ID"):
                    journal.test_delivery(payload, self.path, Mock())
                transport.assert_not_called()
        env = {"TELEGRAM_BOT_TOKEN": "existing-token", "ART_RADAR_TEST_CHAT_ID": "42"}
        with (patch.dict(journal.os.environ, env, clear=True),
              patch.object(journal.urllib.request, "urlopen",
                           side_effect=lambda *_args, **_kw: io.BytesIO(b'{"ok":true,"result":{"type":"group","id":42}}')) as wire):
            with self.assertRaisesRegex(ValueError, "не подтвердил личный чат"):
                journal.test_delivery(payload, self.path, Mock())
            self.assertTrue(wire.call_args.args[0].full_url.endswith("/getChat"))
            self.assertFalse(self.path.exists())

    def test_private_delivery_requires_the_same_cover_bytes_as_preview(self):
        preview = self.path.parent / "cover-1.jpg"
        preview.write_bytes(b"changed")
        attachment = {"file": preview.name, "sha256": hashlib.sha256(b"original").hexdigest()}
        audit = {"verified_links": True, "issues": [{"html": "<b>Тест</b>",
                 "cover": {"tiles": [{"title": "Выпуск"}], "attachment": attachment}}]}
        with patch.object(journal, "confirmed_test_chat", return_value=("token", "42")) as chat:
            with self.assertRaisesRegex(ValueError, "отличается"):
                journal.test_delivery(audit, self.path, Mock(), preview.parent)
            chat.assert_not_called()
            preview.write_bytes(b"original")
            with patch.object(journal.urllib.request, "urlopen",
                              return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')) as wire:
                journal.test_delivery(audit, self.path, Mock(), preview.parent)
            request = wire.call_args.args[0]
            self.assertTrue(request.full_url.endswith("/sendPhoto"))
            self.assertIn(b"original", request.data)
            self.assertIn(b'name="caption"', request.data)
            self.assertNotIn(b"link_preview_options", request.data)

    def test_private_update_edits_only_the_confirmed_issue_from_the_same_chat(self):
        picked, _ = editorial.assess(fresh(), CONFIG, NOW)
        issue = editorial.render_visual(picked, CONFIG, None, NOW)
        payload = {"verified_links": True, "issues": [{"visual": issue}]}
        private_db = self.path.with_name("private.sqlite3")
        with (patch.object(journal, "confirmed_test_chat", return_value=("bot", "42")) as chat,
              patch.object(journal, "send_request", return_value=32) as sender, redirect_stdout(io.StringIO())):
            journal.test_visual_delivery(payload, private_db, Mock(), CONFIG, "rich")
            journal.test_visual_delivery(payload, private_db, Mock(), CONFIG, "rich", update=True)
            self.assertEqual([call.args[0] for call in sender.call_args_list], ["sendRichMessage", "editMessageText"])
            self.assertEqual(sender.call_args.args[1]["message_id"], 32)
            chat.return_value = ("bot", "43")
            with self.assertRaisesRegex(ValueError, "подтверждённый личный выпуск"):
                journal.test_visual_delivery(payload, private_db, Mock(), CONFIG, "rich", update=True)
            self.assertEqual(sender.call_count, 2)

    def test_visual_delivery_never_resends_a_confirmed_or_unconfirmed_card(self):
        path = self.path
        with closing(sqlite3.connect(path)) as first:
            outbox = visual_issue.Deliveries(first)
            transport = Mock(side_effect=[11, RuntimeError("uncertain")])
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                outbox.deliver("release", ["one", "two", "three"], transport, Mock(), pause=Mock())
            self.assertEqual([call.args for call in transport.call_args_list], [("one", None), ("two", 11)])
        with closing(sqlite3.connect(path)) as second:
            outbox = visual_issue.Deliveries(second)
            transport = Mock(return_value=13)
            with self.assertRaisesRegex(visual_issue.UnconfirmedCard, "№2"):
                outbox.deliver("release", ["one", "two", "three"], transport, Mock(), pause=Mock())
            transport.assert_not_called()
            outbox.reconcile("release", 12)
            self.assertEqual(outbox.deliver("release", ["one", "two", "three"], transport, Mock(), pause=Mock()),
                             [11, 12, 13])
            transport.assert_called_once_with("three", 11)

    def test_visual_explicit_rejection_allows_only_that_card_to_retry(self):
        with self.store() as conn:
            outbox = visual_issue.Deliveries(conn)
            transport = Mock(side_effect=[11, journal.TelegramRejected("400"), 12, 13])
            with self.assertRaises(journal.TelegramRejected):
                outbox.deliver("release", ["one", "two", "three"], transport, Mock(), pause=Mock())
            self.assertEqual(outbox.deliver("release", ["one", "two", "three"], transport, Mock(), pause=Mock()),
                             [11, 12, 13])
            self.assertEqual([call.args[0] for call in transport.call_args_list], ["one", "two", "two", "three"])

    def test_explicit_rejection_releases_reservation_without_resend_that_day(self):
        error = urllib.error.HTTPError("https://api.telegram.org", 429, "Too Many Requests", {},
                                       io.BytesIO(b'{"ok":false,"error_code":429}'))
        with (self.store() as conn, patch.dict(journal.os.environ, CREDS, clear=True),
              patch.object(journal, "now_moscow", return_value=NOW),
              patch.object(sources, "collect", return_value=([], [])), patch.object(journal, "check", return_value=True),
              patch.object(journal.urllib.request, "urlopen", side_effect=error) as transport):
            state = journal.Journal(conn)
            with self.assertRaises(journal.TelegramRejected):
                journal.scheduled(state, self.config, fresh(), Mock())
            self.assertEqual(state.clock(), (None, 0, None))
            journal.scheduled(state, self.config, fresh(), Mock())
            transport.assert_called_once()
