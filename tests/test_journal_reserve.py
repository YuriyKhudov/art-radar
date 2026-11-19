import datetime as dt
import sqlite3
import unittest
from types import SimpleNamespace

from radar import editorial, journal_illustrations as illustrations, journal_reserve, journal_visuals
from radar.telegram_journal import verified_visual_issue


class ReserveTest(unittest.TestCase):
    def test_events_expire_at_confirmed_end_and_old_lessons_remain(self):
        today = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
        rows = [SimpleNamespace(key=direction, item={"title": title, "direction": direction,
                "published": "2026-11-10T00:00:00+00:00", **extra})
                for direction, title, extra in [("practice", "Урок масла", {}),
                                               ("news", "Выставка графики", {"event_end": "2026-11-20"})]]
        with sqlite3.connect(":memory:") as conn:
            reserve = journal_reserve.Reserve(conn)
            reserve.replenish(rows, today, 7)
            reserve.confirm(["practice"], today)
            self.assertEqual(reserve.report(today), {"verified": {"practice": 1}, "pending": {"event": 1}})
            later = today + dt.timedelta(days=2)
            self.assertEqual(reserve.report(later), {"verified": {"practice": 1}, "pending": {}})
            reserve.remove(["practice"])
            self.assertFalse(reserve.pool(later))

    def test_fallback_link_cannot_serve_as_art_or_an_entire_visual_issue(self):
        video = {"url": "https://youtube.com/watch?v=Lt4pX0ZhjsQ", "video_link": True, "images": []}
        article = {"url": "https://example.org/feature", "article": True, "images": [{"sha256": "a" * 64}]}
        issue = {"policy": journal_visuals.POLICY, "cards": [article, video]}
        self.assertTrue(verified_visual_issue(issue))
        self.assertFalse(verified_visual_issue({**issue, "cards": [video, video]}))
        self.assertFalse(verified_visual_issue({**issue, "cards": [article, {**video, "article": True}]}))
        self.assertFalse(journal_visuals.video_permalink("https://youtube.com/channel/UCaa"))

    def test_author_video_uses_fact_in_title_without_inventing_duration(self):
        source = {"artist_id": "author", "lang": "en", "domains": ["youtube.com"]}
        raw = {"kind": "video", "title": "12 charcoal portraits in 15 minutes",
               "artist_radar": {"stream": "priority", "artist": "Имя художника"},
               "url": "https://youtube.com/watch?v=Lt4pX0ZhjsQ"}
        card = editorial.official_drawing_exercise(raw, source)
        self.assertIn("12 портретов углём за 15 мин.", card["summary"])
        self.assertNotIn("duration", card)
        self.assertIsNone(editorial.official_drawing_exercise({**raw, "title": "Charcoal prints for sale"}, source))

    def test_catalogued_art_requires_creator_and_per_file_reuse_rights(self):
        work = {"url": "https://upload.wikimedia.org/example.jpg", "mime": "image/jpeg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                "extmetadata": {key: {"value": value} for key, value in
                                {"License": "pd", "Copyrighted": "False", "Artist": "Илья Ефимович Репин",
                                 "ObjectName": "Не ждали", "Categories": "PD-Art (PD-old-auto-expired)"}.items()}}
        row = {"title": "File:Example.jpg", "imageinfo": [work]}
        asset = illustrations.commons_asset(row, "artist", ["Ilya Repin"], ["Не ждали"], "Не ждали", "work")
        self.assertEqual(asset["license"], "Public Domain")
        work["extmetadata"]["Restrictions"] = {"value": "restricted"}
        self.assertIsNone(illustrations.commons_asset(row, "artist", ["Ilya Repin"], ["Не ждали"], "Не ждали", "work"))
        del work["extmetadata"]["Restrictions"]
        row["title"] = "File:Gallery facade.jpg"
        self.assertIsNone(illustrations.commons_asset(row, "artist", ["Ilya Repin"], ["Не ждали"], "Не ждали", "work"))
