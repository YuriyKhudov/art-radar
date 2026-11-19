import datetime as dt
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from unittest.mock import patch

from radar import artist_pages, artist_radar as artist, editorial, editorial_sources as sources, telegram_journal as journal

NOW = dt.datetime(2026, 11, 19, 12, tzinfo=sources.MOSCOW)
REGISTRY = sources.read(artist.REGISTRY)
CONFIG = artist.configure(sources.read(sources.ROOT / "sources.json"), REGISTRY)
PUBLISHERS = {s["id"]: s for s in CONFIG["sources"]}
COLOSSAL = PUBLISHERS["artist-colossal"]


def post(title, excerpt, *, host="thisiscolossal.com", date="2026-11-18T11:30:00"):
    return {"id": 42, "title": {"rendered": title}, "excerpt": {"rendered": excerpt},
            "content": {"rendered": f"<p>{excerpt}</p>"}, "date_gmt": date,
            "link": f"https://{host}/test-artist-article/"}


def example_discovery():
    return artist.editorial_item(post("Eva Novik’s Oil Paintings", "Artist Eva Novik paints portraits in oil on canvas and "
                                      "builds the surface with a palette knife."),
                                 COLOSSAL, "discovery", "oil painting", None, REGISTRY)


class RegistryTests(unittest.TestCase):
    def test_registry_has_all_groups_and_configures_author_channels(self):
        self.assertEqual(len(REGISTRY["artists"]), 34)
        self.assertEqual(len({a["id"] for a in REGISTRY["artists"]}), 34)
        classes = {s["id"] for s in REGISTRY["styles"]}
        for record in REGISTRY["artists"]:
            self.assertLessEqual(set(record["styles"]), classes)
        owned = [s for s in CONFIG["sources"] if s.get("artist_id") == "korolenkov" and not s.get("page_monitor")]
        self.assertEqual({s.get("connector", "atom") for s in owned}, {"atom", "rutube", "vk"})
        self.assertEqual(len({s["publisher"] for s in owned}), 1)
        self.assertTrue(any(s.get("page_monitor") and s.get("artist_id") == "korolenkov" for s in CONFIG["sources"]))

    def test_alias_case_and_explicit_same_name_protection(self):
        match = artist.named_artist("Ірина Єрмолова — нова серія", "", REGISTRY["artists"])
        self.assertEqual(match["id"], "yermolova")
        match = artist.named_artist("Виталий Макаров. Масляная живопись", "", REGISTRY["artists"])
        self.assertIsNone(match)
        match = artist.named_artist("Виталий Макаров. Масляная живопись", "Художник из Ессентуков.", REGISTRY["artists"])
        self.assertEqual(match["id"], "makarov")
        self.assertIsNone(artist.named_artist("Анастасия Ким", "", REGISTRY["artists"]))
        self.assertIsNone(artist.named_artist("Улица Константина Коровина", "Художник Анна Минаева", REGISTRY["artists"]))

    def test_query_plan_uses_both_name_forms_and_independent_theme_queries(self):
        plan = list(artist.query_plan(REGISTRY))
        queries = {(mode, query, target) for _, mode, query, target in plan}
        self.assertIn(("artist", "Бато Дугаржапов", "dugarzhapov"), queries)
        self.assertIn(("artist", "Бато Дугаржапова", "dugarzhapov"), queries)
        self.assertIn(("artist", "Bato Dugarzhapov", "dugarzhapov"), queries)
        self.assertIn(("discovery", "oil painting", None), queries)
        self.assertFalse(any(q[2] == "anastasia-kim" for q in queries))

    def test_third_party_query_records_result_and_skips_incidental_names(self):
        entry = post("An Exhibition of Jeremy Mann’s Oil Paintings",
                     "Artist Jeremy Mann introduces oil paintings on canvas. The exhibition is an account of his studio work.")
        stray = post("One Hundred Exhibitions Across London", "Artist Paula Ross displays a series of paintings. "
                     "The venue also points to past shows, including Jeremy Mann.")
        source = {**COLOSSAL, "discovery": []}
        base = {**CONFIG, "artist_radar": {**REGISTRY, "providers": [source], "artists": [
            a for a in REGISTRY["artists"] if a["id"] == "jeremy-mann"]}}
        with patch.object(artist, "wordpress", return_value=[entry, stray]) as fetch, patch.dict(artist.os.environ, {}, clear=True):
            items, audit = artist.search(base, NOW)
        fetch.assert_called_once_with(source, "Jeremy Mann")
        self.assertEqual(audit[0]["total"], 2)
        self.assertEqual(audit[0]["matched_urls"], [entry["link"]])
        self.assertEqual(items[0]["artist_radar"]["artist_id"], "jeremy-mann")

    def test_discovery_provenance_and_persistence_does_not_change_registry(self):
        candidate = example_discovery()
        self.assertEqual(candidate["artist_radar"]["artist"], "Eva Novik")
        self.assertEqual(artist.discoveries([candidate], REGISTRY)[0]["candidate"], True)
        with (
            closing(sqlite3.connect(":memory:")) as conn,
            redirect_stdout(StringIO()),
            patch.object(sources, "collect", return_value=([], [])),
            patch.object(artist, "search", return_value=([candidate], [{"status": "ok", "count": 1}])),
            patch.object(artist_pages, "monitor", return_value=([], [])),
            patch.object(artist.translate, "ask", return_value=None),
        ):
            state = journal.Journal(conn)
            state.daily(CONFIG, NOW)
            state.daily(CONFIG, NOW + dt.timedelta(days=1))
            found = conn.execute("SELECT id, profile, first_seen, last_seen FROM artist_discoveries").fetchall()
        self.assertEqual(len(found), 1)
        self.assertEqual(json.loads(found[0][1])["queries"][0]["query"], "oil painting")
        self.assertEqual(found[0][2:], ("2026-11-19", "2026-11-20"))
        self.assertFalse(any(r["original"] == "Eva Novik" for r in REGISTRY["artists"]))

    def test_freshness_and_cross_source_dedup_preserve_original_title(self):
        item = example_discovery()
        self.assertIsNone(artist.russian_facts(item, REGISTRY))
        with patch.object(artist.translate, "ask", return_value="Ева Новик пишет портреты маслом на холсте и наносит краску мастихином."):
            artist.translate_articles([item], CONFIG, NOW)
        candidates, notes = editorial.assess([item], CONFIG, NOW)
        self.assertFalse(notes)
        self.assertIn("Работы", candidates[0].item["title"])
        self.assertEqual(candidates[0].item["original_title"], item["title"])
        self.assertIn("мастихином", candidates[0].item["summary"])
        older = {**item, "published": "2026-08-18T11:30:00+00:00"}
        self.assertFalse(editorial.assess([older], CONFIG, NOW)[0])
        ordinary = {**item, "source": "colossal"}
        ordinary.pop("artist_radar")
        self.assertEqual(journal.combine([item], [ordinary]), [item])
        self.assertFalse(editorial.assess([item], CONFIG, NOW, history=[candidates[0].item])[0])

    def test_foreign_video_requires_evidence_and_does_not_guess_subtitles(self):
        raw = {**example_discovery(), "kind": "video", "duration": "3:20", "seconds": 200,
               "evidence": "A conversation about the artist, the pictures and her early years."}
        self.assertIsNone(artist.russian_facts(raw, REGISTRY))
        raw["evidence"] = "A painting time-lapse of the artist applying oil paint to the canvas."
        self.assertIsNone(artist.russian_facts(raw, REGISTRY))
        raw["ru_editorial"] = "В ролике показано, как художница наносит масляную краску на холст."
        facts = artist.russian_facts(raw, REGISTRY)
        self.assertTrue(facts["verified_visual"])
        self.assertEqual(facts["lang"], "en")
        self.assertEqual(facts["translation"], "unknown")
        raw["evidence"] = "A conversation about how the artist works. Russian subtitles are available."
        self.assertEqual(artist.russian_facts(raw, REGISTRY)["translation"], "ru_subtitles")

    def test_catalogue_uses_publication_times_and_ignores_navigation_changes(self):
        source = PUBLISHERS["mann-events"]
        raw = ('<meta property="og:title" content="Jeremy Mann">'
               '<h1>Paintings</h1><script type="application/ld+json">'
               '{"@type":"ExhibitionEvent","startDate":"2026-11-19","dateModified":"2026-11-18"}</script>')
        item, baseline = artist_pages.page_item(artist_pages.parse(raw), source, source["url"])
        self.assertIsNone(item)
        self.assertEqual(baseline["event_dates"], ["2026-11-19"])
        _, unchanged = artist_pages.page_item(artist_pages.parse(raw + '<a href="/contact">Contact</a>'), source, source["url"])
        self.assertEqual(unchanged["digest"], baseline["digest"])
        changed = raw.replace("Paintings", "Paintings and drawings")
        _, observation = artist_pages.page_item(artist_pages.parse(changed), source, source["url"])
        self.assertNotEqual(observation["digest"], baseline["digest"])
        news = ('<script type="application/ld+json">{"@type":"NewsArticle","headline":"Studio News",'
                '"datePublished":"2026-11-18T09:00:00Z","dateModified":"2026-11-19",'
                '"description":"New oil paintings in the studio were made by Jeremy Mann."}</script>')
        item, _ = artist_pages.page_item(artist_pages.parse(news), source, source["url"])
        self.assertEqual(item["published"], "2026-11-18T09:00:00+00:00")

    def test_daily_persists_an_undated_catalogue_as_observation_only(self):
        source = PUBLISHERS["mann-events"]
        probe = artist_pages.observation(source, source["url"], "Works", "Oil paintings", None)

        def run(observed):
            return [], [{"source": source["id"], "status": "ok", "count": 0, "observations": [observed.copy()]}]

        with (closing(sqlite3.connect(":memory:")) as conn, redirect_stdout(StringIO()),
              patch.object(journal, "collected_inputs", side_effect=[run(probe), run(probe), run({**probe, "digest": "new"})])):
            state = journal.Journal(conn)
            for i, status in enumerate(("first_seen", "unchanged", "changed")):
                state.daily(CONFIG, NOW + dt.timedelta(days=i))
                current = json.loads(state.setting("source_report"))[0]["observations"][0]
                self.assertEqual(current["status"], status)
                self.assertIsNone(current["published"])
            self.assertEqual(state.items(), [])

    def test_youtube_search_has_a_budget_and_skips_sufficient_author_feeds(self):
        config = {**CONFIG, "artist_radar": {**REGISTRY, "providers": []}}
        with (patch.dict(artist.os.environ, {"JOURNAL_YOUTUBE_API_KEY": "dummy"}, clear=True),
              patch.object(artist, "youtube_query", return_value=("имя", 0, [])) as query,
              patch.object(sources, "youtube_details")):
            artist.search(config, NOW, covered={"korolenkov", "mad-charcoal"})
        self.assertEqual(query.call_count, 4)
        self.assertFalse({"korolenkov", "mad-charcoal"} & {call.args[1]["id"] for call in query.call_args_list})

    def test_discovery_rejects_names_that_are_techniques_and_caps_publishers(self):
        self.assertIsNone(artist.discovery_name("Plein Air News", "This is the final weekend of Sedona Plein Air, "
                                                "where Plein Air is a local group of painting events."))
        base = example_discovery()
        rows = [{**base, "artist_radar": {**base["artist_radar"], "artist_id": str(index)}} for index in range(7)]
        rows[-1]["source"] = "artist-outdoorpainter"
        discovered = artist.discoveries(rows, REGISTRY, per_source=4)
        self.assertEqual(len(discovered), 5)
        self.assertEqual(sum(r["source"] == "artist-colossal" for r in discovered), 4)


if __name__ == "__main__":
    unittest.main()
