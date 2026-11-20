"""Collect, preview and (when enabled) publish the separate Art Radar journal."""

import argparse
import datetime as dt
import hashlib
import hmac
import html
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from http.client import HTTPException
from pathlib import Path

from radar import (artist_pages, artist_radar, editorial, editorial_sources as sources, journal_articles,
                   journal_cover, journal_reserve, journal_visuals, visual_issue)

MOSCOW = dt.timezone(dt.timedelta(hours=3), "MSK")
DEFAULT_DB = sources.ROOT.parent / "data" / "journal.sqlite3"
STATE_BRANCH = "art-radar-journal-state"
TEST_STATE_BRANCH = "art-radar-journal-test-state"
DEFAULT_TEST_DB = DEFAULT_DB.with_name("journal-test.sqlite3")


def now_moscow():
    return dt.datetime.now(MOSCOW)


def due(now, last, time, interval, attempted=None):
    now = now.astimezone(MOSCOW)
    return (now.time() >= time and now.date().isoformat() != attempted
            and (not last or (now.date() - dt.datetime.fromisoformat(last).astimezone(MOSCOW).date()).days >= interval))


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def collected_inputs(config, now, artist_snapshot=None, previous=()):
    live, report = sources.collect(config)
    if config.get("artist_radar"):
        catalog = {source["id"]: source for source in config["sources"]}
        covered = {catalog[item["source"]].get("artist_id") for item in live if item.get("published")
                   and item["kind"] == "video" and len(item["evidence"].split()) >= 8
                   and now - dt.timedelta(days=7) <= dt.datetime.fromisoformat(item["published"]) <= now}
        with ThreadPoolExecutor(max_workers=2) as pool:
            pages = pool.submit(artist_pages.monitor, config)
            search = None if artist_snapshot else pool.submit(artist_radar.search, config, now, covered - {None})
            found, queries = (search.result() if search else (artist_snapshot["items"], artist_snapshot["queries"]))
            monitored, monitors = pages.result()
        live = combine(combine(live, found), monitored)
        live = [artist_radar.annotate(item, config, catalog[item["source"]]) for item in live]
        sources.crossposts(live, config)
        status = "ok" if any(q["status"] == "ok" for q in queries) else "Поиск недоступен"
        report.append({"source": "artist-radar", "name": "Artist Radar", "status": status,
                       "count": len(found), "queries": queries})
        report.extend(monitors)
        report.append(artist_radar.translate_articles(live, config, now, previous))
    return live, report


class Journal:
    def __init__(self, conn):
        self.conn = conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS feed_items (
                url TEXT PRIMARY KEY, item TEXT NOT NULL, collected TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prepared_images (
                sha256 TEXT PRIMARY KEY, jpeg BLOB NOT NULL, checksum TEXT NOT NULL,
                prepared TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artist_discoveries (
                id TEXT PRIMARY KEY, profile TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artist_pages (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS issues (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'released')),
                attempted TEXT NOT NULL, sent TEXT, telegram_id INTEGER, items TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON issues(status) WHERE status = 'pending';
        """)
        self.deliveries = visual_issue.Deliveries(conn)
        self.editorial_reserve = journal_reserve.Reserve(conn)

    def setting(self, name):
        row = self.conn.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def put(self, name, value):
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, value))

    def prepared(self, stage, card, config):
        name = preparation_key(stage, card, config)
        saved = json.loads(self.setting(name) or "null")
        if saved and (stage != "visual" or now_moscow().date() - dt.date.fromisoformat(saved["date"]) <= dt.timedelta(days=14)):
            return saved["result"]
        return None

    def remember(self, stage, card, config, result):
        with self.conn:
            self.put(preparation_key(stage, card, config), encode({"date": now_moscow().date().isoformat(), "result": result}))

    def remember_images(self, files):
        if not files:
            return
        now = now_moscow().isoformat()
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO prepared_images VALUES (?, ?, ?, ?)",
                                  [(digest, body, hashlib.sha256(body).hexdigest(), now)
                                   for digest, body in files.items() if len(body) <= 2_000_000])
            self.conn.execute("DELETE FROM prepared_images WHERE sha256 IN (SELECT sha256 FROM prepared_images "
                              "ORDER BY prepared DESC, sha256 LIMIT -1 OFFSET 24)")

    def prepared_image(self, asset):
        digest = asset.get("sha256")
        row = self.conn.execute("SELECT jpeg, checksum, prepared FROM prepared_images WHERE sha256=?", (digest,)).fetchone()
        if row and now_moscow() - dt.datetime.fromisoformat(row[2]) <= dt.timedelta(days=14):
            image, checksum, _ = row
            if hashlib.sha256(image).hexdigest() == checksum:
                return asset, image
        return None

    def daily(self, config, now):
        today = now.date().isoformat()
        if self.setting("collected_on") == today:
            return False
        if config.get("artist_radar"):
            live, report = collected_inputs(config, now, previous=self.items())
        else:
            live, report = collected_inputs(config, now)
        registry = config.get("artist_radar")
        oldest = (now.date() - dt.timedelta(days=config["news_days"] + 2)).isoformat()
        reserve_candidates = []
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO feed_items VALUES (?, ?, ?)",
                                  [(sources.canonical(item["url"]), encode(item), today) for item in live])
            self.conn.execute("DELETE FROM feed_items WHERE collected < ?", (oldest,))
            if config.get("accumulate_reserve"):
                reserve_candidates, _ = editorial.assess(live, config, now, self.history())
                self.editorial_reserve.replenish(reserve_candidates, now, config["news_days"])
            if registry:
                profiles = artist_radar.discoveries(live, registry, limit=1000)
                self.conn.executemany(
                    """INSERT INTO artist_discoveries VALUES (?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET profile=excluded.profile, last_seen=excluded.last_seen""",
                    [(p["id"], encode(p), today, today) for p in profiles],
                )
                for source in report:
                    for observed in source.get("observations", []):
                        saved = self.conn.execute("SELECT digest FROM artist_pages WHERE id=?", (observed["id"],)).fetchone()
                        observed["status"] = "first_seen" if saved is None else (
                            "unchanged" if saved[0] == observed["digest"] else "changed")
                        self.conn.execute("""INSERT INTO artist_pages VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET digest=excluded.digest, last_seen=excluded.last_seen,
                            content=excluded.content""", (observed["id"], observed["digest"], today, today, encode(observed)))
            self.put("source_report", encode(report))
            # Failed sources retain the previous successful harvest; a full failure can retry next tick.
            if not report or any(row["status"] == "ok" for row in report):
                self.put("collected_on", today)
        if reserve_candidates and config.get("video_link_fallback"):
            qualify_video_reserve(self.editorial_reserve, reserve_candidates, now)
        for row in report:
            print(f"{row['source']} · {row['status']} · {row['count']}")
        return True

    def items(self):
        return [json.loads(row[0]) for row in self.conn.execute("SELECT item FROM feed_items")]

    def history(self):
        return [{**row["item"], "_issued_at": sent}
                for batch, sent in self.conn.execute("SELECT items,sent FROM issues WHERE status='sent'")
                for row in json.loads(batch)]

    def clock(self):
        sent, count = self.conn.execute("SELECT MAX(sent), COUNT(*) FROM issues WHERE status='sent'").fetchone()
        pending = self.conn.execute("SELECT id FROM issues WHERE status='pending'").fetchone()
        return sent, count, pending[0] if pending else None

    def reserve(self, picked, now, time, interval, expected_count, plan=None):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            last, count, pending = self.clock()
            if pending or count != expected_count or not due(now, last, time, interval, self.setting("attempted_on")):
                return None
            batch = [{"item": c.item, "url": c.key, "score": c.score, "ratings": c.metrics} for c in picked]
            self.put("attempted_on", now.date().isoformat())
            if not batch:
                return None
            self.put("reservation", uuid.uuid4().hex)
            issue = self.conn.execute("INSERT INTO issues (status, attempted, items) VALUES ('pending', ?, ?)",
                                      (now.isoformat(), encode(batch))).lastrowid
            if plan:
                self.deliveries.record(f"issue:{issue}", plan)
            return issue

    def resolve(self, issue, message_id, now=None):
        with self.conn:
            if message_id:
                updated = self.conn.execute(
                    "UPDATE issues SET status='sent', sent=COALESCE(?, attempted), telegram_id=? "
                    "WHERE id=? AND status='pending'", (now.isoformat() if now else None, message_id, issue))
            else:
                updated = self.conn.execute("UPDATE issues SET status='released' WHERE id=? AND status='pending'", (issue,))
            if not updated.rowcount:
                raise ValueError("Выпуск не находится в состоянии ожидания подтверждения")
            if message_id:
                batch = self.conn.execute("SELECT items FROM issues WHERE id=?", (issue,)).fetchone()[0]
                self.editorial_reserve.remove(row["url"] for row in json.loads(batch))


def checkpoint(db, expected_branch=STATE_BRANCH):
    """Push the durable reservation before any attempt to send to Telegram."""
    def git(*args):
        return subprocess.run(["git", "-C", str(db.parent), *args], check=True)

    branch = subprocess.check_output(["git", "-C", str(db.parent), "branch", "--show-current"], text=True).strip()
    if branch != expected_branch:
        raise ValueError(f"Для хранения истории требуется отдельная ветка {expected_branch}")
    git("add", "--force", "--", db.name)
    changes = subprocess.run(["git", "-C", str(db.parent), "diff", "--cached", "--quiet"])
    if changes.returncode == 1:
        git("-c", "user.name=art-radar-journal", "-c", "user.email=art-radar-journal@users.noreply.github.com",
            "commit", "-m", "Save Art Radar journal state", "--", db.name)
    else:
        changes.check_returncode()
    # Still push if the last invocation committed locally but its push failed.
    git("push", "origin", f"HEAD:refs/heads/{expected_branch}")


def check(url, domains, image=False):
    for _ in range(2):
        try:
            return sources.check_url(url, domains, image)
        except urllib.error.HTTPError as error:
            if error.code not in {425, 429, 502, 503, 504}:
                return False
        except urllib.error.URLError as error:
            if not isinstance(error.reason, TimeoutError):
                return False
        except TimeoutError:
            continue
        except (OSError, ValueError):
            return False
    return False


class LinkChecks:
    def __init__(self, replay=None, verify=check, allow_new=False):
        self.replay = replay is not None and not allow_new
        self.verify = verify
        self.result = {(url, tuple(domains), image): valid for url, domains, image, valid in replay or []}

    def __call__(self, url, domains, image=False):
        key = url, tuple(domains), image
        if key not in self.result:
            if self.replay:
                raise ValueError(f"В записи проверки отсутствует ссылка {url}")
            self.result[key] = self.verify(url, domains, image)
        return self.result[key]

    def snapshot(self):
        return [[*key, result] for key, result in self.result.items()]


def combine(live, archive):
    merged = {}
    for item in [*live, *archive]:
        key = sources.canonical(item["url"])
        old = merged.get(key)
        if old and old.get("artist_radar") and not item.get("artist_radar") and not item.get("reviewed"):
            continue
        if old and (old.get("search_matches") or item.get("search_matches")):
            hits = {encode(hit): hit for hit in old.get("search_matches", []) + item.get("search_matches", [])}
            item = {**item, "search_matches": list(hits.values())}
        if old and old.get("ru_editorial") and not item.get("ru_editorial") and old.get("fact_excerpt") == item.get("fact_excerpt"):
            item = {**item, "ru_editorial": old["ru_editorial"]}
        merged[key] = item
    return list(merged.values())


def load_harvest(paths, config):
    runs = sorted((sources.read(path) for path in paths), key=lambda run: run["collected_at"])
    current, identities = [], {}
    for run in runs:
        for row in run["sources"]:
            previous = identities.get(row["source"])
            if not previous or previous["status"] != "ok" or row["status"] == "ok":
                identities[row["source"]] = row
        for item in run["items"]:
            current.append({
                **item, "evidence": sources.CONTACT.sub("", item["evidence"]),
                "access_evidence": sources.CONTACT.sub("", item.get("access_evidence", "")),
            })
    items = combine(current, [])
    sources.crossposts(items, config)
    counts = Counter(item["source"] for item in items)
    indexes = Counter(item.get("archive_index") for item in items)
    report = []
    for key, row in identities.items():
        if "-archive:" in key:
            count = indexes[row.get("endpoint")]
        elif key in {"artist-radar", "artist-translation", "youtube-details"}:
            count = row["count"]
        else:
            count = counts[key]
        report.append({**row, "count": count})
    return {"collected_at": runs[-1]["collected_at"], "sources": report,
            "runs": [entry for run in runs for entry in run.get("runs", [
                {"collected_at": run["collected_at"], "sources": run["sources"]}])],
            "items": items}


def preparation_key(stage, card, config):
    keys = ("original_title", "title", "evidence", "published", "language", "image")
    material = {key: card.item.get(key) for key in keys}
    if stage == "article":
        settings = [journal_articles.EDITORIAL_VERSION, config.get("russian_editions", {}).get(card.key),
                    config.get("article_edits", {}).get(card.key)]
    else:
        presentation = config.get("presentation", {})
        settings = [journal_visuals.POLICY, config.get("extended_illustrations"), config.get("video_link_fallback"),
                    *(presentation.get(key, {}).get(card.key) for key in ("cards", "cover_images", "visual_images"))]
    digest = hashlib.sha256(encode([card.key, card.source["id"], material, settings]).encode()).hexdigest()
    return f"prepared:{stage}:{digest}"


def prepare(pool, config, history, now, theme, sequence, verify, article_checks=None, visual_checks=None, reserve=None,
            replay_reserve=False, journal=None):
    history = [*history, *({"url": url, "title": "", "source": ""} for url in config.get("previously_published", []))]
    if reserve is not None:
        pool = combine(pool, reserve.pool(now))
    cards, excluded = editorial.assess(pool, config, now, history)
    if visual_checks:
        candidates = [card for card in cards if not theme or theme in card.item.get("topics", [])]
        checked_cards = []
        if journal:
            for card in candidates:
                if card.key not in visual_checks.results and (saved := journal.prepared("visual", card, config)):
                    visual_checks.results[card.key] = saved
        unchecked = {card.key for card in candidates if card.key not in visual_checks.results} if journal else set()
        # Network access is bounded per source; results are reused between issues in the same run.
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(visual_checks.check, candidates))
        for card, review in zip(candidates, results):
            if review.get("reason"):
                audit = card.audit(review["reason"])
                audit["visual_review"] = visual_audit(review)
                excluded.append(audit)
            else:
                checked_cards.append(replace(card, item={**card.item, "journal_visual": review}))
                if journal and card.key in unchecked and (review["images"] or review["video"]):
                    journal.remember("visual", card, config, review)
        if journal:
            journal.remember_images(visual_checks.files)
            visual_checks.files.clear()
        cards = checked_cards
    url_check = check if verify is True else verify or None
    verified = {}

    def edit_article(card):
        if journal and card.key not in article_checks.results:
            if saved := journal.prepared("article", card, config):
                article_checks.results[card.key] = saved
                return saved
            result = article_checks.check(card)
            if result.get("summary"):
                journal.remember("article", card, config, result)
            return result
        return article_checks.check(card)

    def validate(card):
        if card.key not in verified:
            article = edit_article(card) if article_checks and card.item["kind"] != "video" else {}
            verified[card.key] = article.get("reason") or (
                True if article or not url_check else url_check(card.item["url"], card.source["domains"]))
            if verified[card.key] is True and card.item["kind"] == "video" and url_check:
                known = card.item.get("related_urls", [])
                linked = sources.confirmed_video_refs(card.item, card.source) if not getattr(url_check, "replay", False) else []
                alternatives = [url for url in dict.fromkeys([*known, *linked]) if sources.video_platform(url)
                                and url_check(url, [urllib.parse.urlsplit(sources.canonical(url)).hostname])]
                if alternatives:
                    card.item["verified_video_urls"] = alternatives
                    card.item["related_urls"] = alternatives
                    if any(editorial.duplicates(card, prior) for prior in history):
                        verified[card.key] = "Уже опубликована подтверждённая версия того же ролика"
        return verified[card.key]

    picked, audit = editorial.select(cards, config, theme, validate if verify or article_checks else None, history, now,
                                     reserve=reserve is not None or replay_reserve, visual=visual_checks is not None)
    if reserve is not None:
        with reserve.connection:
            reserve.confirm([key for key, passed in verified.items() if passed is True], now)
    if visual_checks:
        investigated = {card.item["url"]: card.item["journal_visual"] for card in cards}
        for row in audit:
            row["visual_review"] = visual_audit(investigated[row["url"]])
    if not picked:
        return picked, {"theme": theme, "items": excluded + audit}
    selected_rows = {row["url"]: row for row in audit if row["decision"] == "Выбран в выпуск"}
    for card in picked:
        if card.item["kind"] == "video":
            selected_rows[card.item["url"]]["video_platforms"] = editorial.video_actions(card.item)
    if article_checks:
        for index, card in enumerate(picked):
            if card.item["kind"] != "video":
                metadata = edit_article(card)
                picked[index] = replace(card, item={**card.item, "journal_article": metadata})
                row = selected_rows[card.item["url"]]
                row["article_edit"] = {key: value for key, value in metadata.items() if key != "summary"}
    text, image = editorial.render(picked, config, theme, url_check=url_check, now=now)
    visual = editorial.render_visual(picked, config, theme, now)
    if visual_checks:
        visual["policy"] = journal_visuals.POLICY
    return picked, {"theme": theme, "html": text, "cover": image, "items": excluded + audit,
                    "visual": visual}


def visual_audit(review):
    return {"subject": review["subject"], "images": [asset["role"] for asset in review["images"]],
            "video": bool(review["video"]), "quality": review["quality"],
            "video_link": bool(review.get("video_link")), "candidates": review.get("candidates", 1),
            "search": review.get("search", []),
            "external_assets": [{key: asset[key] for key in ("registry", "source", "license", "credit")}
                                for asset in review["images"] if asset.get("registry")]}


def qualify_video_reserve(reserve, cards, now, verifier=None):
    verified = reserve.confirmed(now)
    choices = sorted((card for card in cards if card.key not in verified and card.item["kind"] == "video"
                      and journal_visuals.video_permalink(card.key)), key=lambda card: (card.stream == "priority", card.score),
                     reverse=True)
    publishers = {}
    for card in choices:
        publishers.setdefault(card.publisher, card)
    choices = list(publishers.values())[:8]
    check_link = verifier or check
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda card: check_link(card.item["url"], card.source["domains"]), choices))
    with reserve.connection:
        reserve.confirm((card.key for card, passed in zip(choices, results) if passed), now)


def verified_visual_issue(visual):
    cards = visual.get("cards", [])
    if visual.get("policy") != journal_visuals.POLICY or not 2 <= len(cards) <= 4:
        return False
    if not any(card.get("images") or card.get("video") for card in cards):
        return False
    if any(a.get("video_link") and b.get("video_link") for a, b in zip(cards, cards[1:])):
        return False
    for card in cards:
        assets = [card["video"]] if card.get("video") else card.get("images", [])
        if not assets and card.get("video_link") and not card.get("article") and journal_visuals.video_permalink(card["url"]):
            continue
        if not assets or any(not re.fullmatch(r"[a-f0-9]{64}", asset.get("sha256", "")) for asset in assets):
            return False
    return True


def batch_verifier(pool, config):
    """Check the example catalogue once, even when five issues share the same pool."""
    catalog = {source["id"]: tuple(source["domains"]) for source in config["sources"] if source.get("enabled")}
    urls = set()
    for item in pool:
        domains = catalog.get(item["source"])
        if not domains:
            continue
        urls.add((item["url"], domains, False))
        if item.get("alternative"):
            urls.add((item["alternative"], tuple(item.get("alternative_domains", [])), False))
        if item.get("player"):
            urls.add((item["player"], tuple(config["player_domains"]), False))
        custom = config.get("presentation", {}).get("cards", {}).get(sources.canonical(item["url"]), {})
        cover = sources.images({**item, **custom}, config)
        if cover:
            urls.add((cover["url"], tuple(config["image_domains"]), True))
    requests = sorted(urls)
    with ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = dict(zip(requests, executor.map(lambda args: check(*args), requests)))

    def checked(url, domains, image=False):
        return outcomes.get((url, tuple(domains), image), False)

    return checked


def next_theme(config, sequence):
    themes, every = list(config["themes"]), config["theme_every"]
    if every and themes and sequence % every == 0:
        return themes[(sequence // every - 1) % len(themes)]
    return None


def save_preview(output, issues, report, day, verified, snapshot=None, replay=None, registry=None, reserve_report=None):
    output.mkdir(parents=True, exist_ok=True)
    snapshot_hash = None
    if snapshot is not None:
        body = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        (output / "harvest.json").write_bytes(body)
        snapshot_hash = hashlib.sha256(body).hexdigest()
    if replay is not None:
        (output / "selection.json").write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
    for index, issue in enumerate(issues, 1):
        visual = issue.get("visual")
        if not visual:
            continue
        suffix = "" if len(issues) == 1 else f"-{index}"
        (output / f"visual{suffix}.html").write_text(visual_issue.gallery_preview(visual), encoding="utf-8")
        classic = visual_issue.classic_html(visual["header"], visual["cards"])
        (output / f"classic{suffix}.html.txt").write_text(classic + "\n", encoding="utf-8")
    selected = [row for issue in issues for row in issue["items"] if row["decision"] == "Выбран в выпуск"]
    diversity = {"directions": dict(Counter(row["direction"] for row in selected)),
                 "publishers": dict(Counter(row["publisher"] for row in selected)),
                 "artist_mix": dict(Counter((row.get("artist_radar") or {}).get("stream", "general") for row in selected)),
                 "videos": sum(row["kind"] == "video" for row in selected),
                 "durations": sum(bool(row["duration"]) for row in selected if row["kind"] == "video"),
                 "fresh_3d": sum(row["age"] is not None and row["age"] <= 3 for row in selected),
                 "archives": sum(row["direction"] == "archive" for row in selected)}
    discoveries = artist_radar.discoveries(
        [item for item in snapshot["items"] if item.get("fact_excerpt")], registry, per_source=4
    ) if snapshot and registry else []
    articles = []
    for index, issue in enumerate(issues, 1):
        if "html" not in issue:
            articles.append('<article><div class="post"><p>Подборка не набрала подходящих материалов.</p></div></article>')
            continue
        cover = issue["cover"]
        image = ""
        if cover and cover.get("tiles"):
            photo, originals = journal_cover.compose(cover)
            name = f"cover-{index}.jpg"
            (output / name).write_bytes(photo)
            cover["attachment"] = {"file": name, "sha256": hashlib.sha256(photo).hexdigest(),
                                   "original_images": originals}
            image = f'<img class="issue-cover" src="{name}" alt="Обложка выпуска Art Radar">'
        elif cover and cover.get("embed"):
            embed = html.escape(cover["embed"], quote=True)
            title = html.escape(cover["title"], quote=True)
            image = (f'<div class="player"><iframe src="{embed}" title="{title}" loading="lazy" '
                     f'allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div>')
        elif cover and cover.get("native"):
            image = f'<div class="native">{editorial.link(cover["title"], cover["url"])}<br>{html.escape(cover["credit"])}</div>'
        elif cover and cover.get("collage"):
            image = '<div class="collage">' + "".join(
                f'<img src="{html.escape(asset["url"], quote=True)}" alt="{html.escape(asset["credit"], quote=True)}">'
                for asset in cover["collage"]) + "</div>"
        elif cover:
            image = f'<img src="{html.escape(cover["url"], quote=True)}" alt="{html.escape(cover["credit"], quote=True)}">'
        text = issue["html"].replace("\n\n", "</p><p>").replace("\n", "<br>")
        date = html.escape(issue.get("as_of") or day)
        visual_link = f' · <a style="color:inherit" href="visual-{index}.html">Визуальный выпуск</a>' if len(issues) > 1 and issue.get("visual") else ""
        articles.append(f'<article id="issue-{index}"><div class="issue-date">Выпуск {index} · {date}{visual_link}</div>'
                        f'{image}<div class="post"><p>{text}</p></div></article>')
        (output / f"issue-{index}.html.txt").write_text(issue["html"] + "\n", encoding="utf-8")
    page = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Выпуски Art Radar</title>
<style>
:root{color-scheme:light;background:#eeebe5;color:#211e19;font:16px/1.52 system-ui,sans-serif}
body{margin:0 auto;padding:28px 16px 64px;max-width:690px}
header{margin:0 0 28px;border-bottom:2px solid #211e19;padding-bottom:18px}
h1{font:normal clamp(36px,8vw,64px)/1 Georgia,serif;margin:0 0 12px}
article{border:1px solid #d7d0c4;border-radius:10px;overflow:hidden;margin:0 0 28px;background:#fbf9f4}
.issue-date{padding:9px clamp(16px,5vw,38px);background:#252522;color:#fff;font-size:.9em}
.player{aspect-ratio:16 / 9;background:#242320}.player iframe{display:block;width:100%;height:100%;border:0}
.native{padding:24px clamp(16px,5vw,38px);background:#e7dfd1}.native a{font:1.25em Georgia,serif}
.collage{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;background:#d9d2c3}
img{display:block;width:100%;max-height:420px;object-fit:contain;background:#d9d2c3}
.issue-cover{max-height:none;background:#f6f0e5}
.post{padding:8px clamp(16px,5vw,38px) 20px}.post p{margin:0;padding:14px 0;border-bottom:1px solid #e4dbcf}
.post p:last-child{border-bottom:0}a{color:#8a341d;text-underline-offset:3px}
i{color:#5b5245;font-size:.9em}
</style></head><body><header><h1>Art Radar</h1>
<a href="audit.html">Оценки и результаты сбора</a></header>"""
    (output / "index.html").write_text(page + "\n".join(articles) + "</body></html>\n", encoding="utf-8")
    (output / "audit.json").write_text(json.dumps({"date": day, "verified_links": verified, "reserve": reserve_report,
                                                 "snapshot_sha256": snapshot_hash, "diversity": diversity,
                                                 "sources": report, "issues": issues, "discoveries": discoveries},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    save_audit(output, issues, report, verified, diversity, snapshot_hash, registry, discoveries, reserve_report)
    print(output / "index.html")


def save_audit(output, issues, report, verified, diversity, snapshot_hash, registry=None, discoveries=(), reserve_report=None):
    codes = {"usefulness": "Польза", "originality": "Оригинальность", "language": "Язык",
             "source": "Источник", "freshness": "Актуальность"}
    source_rows = [f"<li>{html.escape(row.get('name', row['source']))} · {html.escape(row['status'])} · {row['count']}</li>"
                   for row in report]
    sections = []
    for index, issue in enumerate(issues, 1):
        grouped = {}
        for row in issue["items"]:
            ratings = " · ".join(f"{codes[key]} {value:g}/5" for key, value in row.get("ratings", {}).items())
            subtitle = " · ".join(filter(None, [row["source"], row.get("published", "")[:10] if row.get("published") else "",
                                                row.get("duration"), f"{row['score']:g}/100" if "score" in row else ""]))
            title = editorial.link(row["title"], row["url"])
            reason = html.escape(row.get("reason", ""))
            via = html.escape(row.get("provenance") or "")
            review = row.get("visual_review", {})
            assets = "".join(f'<li>{editorial.link(asset["credit"], asset["source"])} · {html.escape(asset["license"])}'
                             f' · {html.escape(asset["registry"])}</li>' for asset in review.get("external_assets", []))
            queries = [f'<li>{html.escape(entry["provider"])} · {html.escape(entry["query"])} · '
                       f'{entry.get("matched", 0)} подходящих записей</li>' for entry in review.get("search", [])]
            visual_details = (f'<details><summary>Поиск иллюстраций</summary><ul>{assets}{"".join(queries)}</ul></details>'
                              if assets or queries else "")
            item = f"<li>{title}<br>{html.escape(subtitle)}<br>{html.escape(ratings)}<br>{reason}<br>{via}{visual_details}</li>"
            grouped.setdefault(row["decision"], []).append(item)
        blocks = []
        for decision, items in sorted(grouped.items(), key=lambda pair: pair[0] != "Выбран в выпуск"):
            opened = " open" if decision == "Выбран в выпуск" else ""
            blocks.append(f"<details{opened}><summary>{html.escape(decision)} · {len(items)}</summary>"
                          f"<ul>{''.join(items)}</ul></details>")
        sections.append(f"<section><h2>Выпуск {index}</h2>{''.join(blocks)}</section>")
    mode = "Сетевые проверки включены." if verified else "Локальная проверка оформления."
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
<title>Отчёт Art Radar</title><style>body{{max-width:850px;margin:24px auto;padding:0 16px 40px;
font:16px/1.55 system-ui,sans-serif;background:#fbf9f4;color:#211e19}}a{{color:#8a341d}}li{{margin-bottom:8px}}
details{{margin:9px 0;border:1px solid #d7d0c4;padding:6px 12px;border-radius:6px}}summary{{cursor:pointer}}</style>
</head><body><h1>Отбор материалов Art Radar</h1><p><a href="index.html">Предпросмотр</a> ·
<a href="audit.json">Данные JSON</a></p><p>{mode}</p>"""
    if source_rows:
        page += f"<h2>Собрано из лент · {sum(row['count'] for row in report)}</h2><ul>{''.join(source_rows)}</ul>"
    if reserve_report:
        categories = {"news": "Свежие новости", "event": "Выставки и события", "practice": "Уроки и техники",
                      "discovery": "Художественные открытия", "archive": "Архивные статьи и фильмы"}
        before = reserve_report["before"]
        page += "<h2>Накопительный резерв</h2><ul>" + "".join(
            f'<li>{name} · подтверждено {before["verified"].get(key, 0)} · ожидают проверки {before["pending"].get(key, 0)}</li>'
            for key, name in categories.items()) + "</ul>"
        for index, run in enumerate(reserve_report["issues"], 1):
            page += f'<p>Выпуск {index} · взято из резерва {len(run["selected"])}</p>'
    mix = " · ".join(f"{html.escape(editorial.DIRECTIONS[key])} {count}" for key, count in diversity["directions"].items())
    page += (f"<h2>Тематическое разнообразие</h2><p>{mix}</p>"
             f"<p>Источников {len(diversity['publishers'])} · видео {diversity['videos']} · "
             f"указана длительность у {diversity['durations']} · до трёх дней {diversity['fresh_3d']} · "
             f"архивных {diversity['archives']}</p>")
    streams = diversity.get("artist_mix", {})
    if "artist-radar" in {row["source"] for row in report}:
        page += ("<h2>Artist Radar</h2><p>"
                 f"Приоритетные художники {streams.get('priority', 0)} · "
                 f"художественные открытия {streams.get('discovery', 0)} · общие материалы {streams.get('general', 0)}</p>")
    if registry and issues:
        page += favorite_audit(registry, report, issues[-1]["items"])
        if discoveries:
            publishers = {source["id"]: source["name"] for source in registry["providers"]}
            profiles = [f'<li>{editorial.link(row["name"], row["url"])} · {html.escape(publishers[row["source"]])}'
                        f' · {html.escape(row["published"][:10])}<br>{html.escape(row["ru_editorial"] or row["fact_excerpt"])}</li>'
                        for row in discoveries if row["source"] in publishers]
            page += (f"<h2>Художники в поле зрения</h2><details><summary>{len(profiles)} найденных авторов с фактами "
                     f"из публикации</summary><ul>{''.join(profiles)}</ul></details>")
    if snapshot_hash:
        page += (f'<p>Выпуски собраны из <a href="harvest.json">снимка автоматической выгрузки</a>. '
                 f'SHA-256 <code style="overflow-wrap:anywhere">{snapshot_hash}</code>.</p>')
    if (output / "selection.json").exists():
        page += '<p><a href="selection.json">Входная история, время отбора и результаты проверки ссылок</a></p>'
    (output / "audit.html").write_text(page + "".join(sections) + "</body></html>\n", encoding="utf-8")


def favorite_audit(registry, report, cards):
    status = {"first_seen": "Первая проверка", "unchanged": "Без изменений", "changed": "Изменились метаданные"}
    sections = []
    monitored_artists = {record["id"]: record["artist_id"] for record in registry.get("page_monitors", [])}
    named_queries = [query for row in report for query in row.get("queries", []) if query.get("mode") != "discovery"]
    for artist in registry["artists"]:
        if artist["tier"] != "main" or not artist["enabled"]:
            continue
        key = artist["id"]
        matched = [row for row in cards if (row.get("artist_radar") or {}).get("artist_id") == key]
        queries = [query for query in named_queries if query.get("artist") == key]
        monitored = [source for source in report if monitored_artists.get(source["source"]) == key]
        sources_html = []
        for source in monitored:
            endpoint = editorial.link(source["name"], source["endpoint"])
            detail = []
            for seen in source.get("observations", []):
                dates = " · ".join(html.escape(date) for date in seen.get("event_dates", []))
                publication = html.escape(seen["published"][:10]) if seen["published"] else "дата публикации отсутствует"
                fingerprint = html.escape(seen["digest"][:12])
                checking = status.get(seen.get("status"), "Снимок метаданных")
                detail.append(f'<li>{editorial.link(seen["title"], seen["url"])} · {publication} · {checking} · '
                              f'<code>{fingerprint}</code>' + (f" · даты событий {dates}" if dates else "") + "</li>")
            sources_html.append(f'<li>{endpoint} · {html.escape(source["status"])}' +
                                (f"<ul>{''.join(detail)}</ul>" if detail else "") + "</li>")
        entries = [f'<li>{editorial.link(row["title"], row["url"])} · '
                   f'{html.escape((row.get("published") or "без даты")[:10])} · {html.escape(row["decision"])}</li>'
                   for row in sorted(matched, key=lambda row: row.get("published") or "", reverse=True)]
        sections.append(f'<details{" open" if any(r["decision"] == "Выбран в выпуск" for r in matched) else ""}>'
                        f'<summary>{html.escape(artist["ru"])} · найдено {len(matched)}</summary>'
                        f'<p>Поиск по имени · ответили {sum(q["status"] == "ok" for q in queries)} из {len(queries)}</p>'
                        + (f"<ul>{''.join(entries)}</ul>" if entries else "<p>Материалы с подтверждённым авторством отсутствуют.</p>")
                        + (f"<ul>{''.join(sources_html)}</ul>" if sources_html else "") + "</details>")
    return "<h2>Приоритетные художники</h2>" + "".join(sections)


TelegramRejected = visual_issue.TelegramRejected


def collage_bytes(assets):
    from PIL import Image, ImageOps

    domains = sources.read(sources.ROOT / "sources.json")["image_domains"]
    background = (242, 237, 227)
    canvas = Image.new("RGB", (1080, 540 * ((len(assets) + 1) // 2)), background)
    for index, asset in enumerate(assets):
        if not sources.images({"image": asset}, {"image_domains": domains}):
            raise ValueError("Изображение для коллажа не разрешено")
        raw, _ = sources.request(asset["url"], domains, 5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("Изображение для коллажа слишком большое")
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 24_000_000:
                raise ValueError("Изображение для коллажа слишком большое")
            tile = ImageOps.contain(ImageOps.exif_transpose(image).convert("RGBA"), (508, 508))
        offset = (index % 2 * 540 + (540 - tile.width) // 2, index // 2 * 540 + (540 - tile.height) // 2)
        canvas.paste(tile, offset, tile)
    output = io.BytesIO()
    canvas.save(output, "JPEG", quality=88)
    return output.getvalue()


def send(issue, token, chat, thread, photo=None):
    text, cover = issue["html"], issue["cover"]
    data = {"chat_id": chat, "parse_mode": "HTML"}
    if thread is not None:
        data["message_thread_id"] = thread
    fits_caption = editorial.length(text) <= editorial.PHOTO_CAPTION_LIMIT
    if cover and cover.get("tiles") and fits_caption and photo is None:
        photo, _ = journal_cover.compose(cover)
    if cover and cover.get("collage") and fits_caption and photo is None:
        try:
            photo = collage_bytes(cover["collage"])
        except (ImportError, OSError, ValueError):
            pass
    if photo and fits_caption:
        method = "sendPhoto"
        data["caption"] = text
    elif cover and cover.get("url") and not (cover.get("embed") or cover.get("native") or cover.get("collage")) and fits_caption:
        method = "sendPhoto"
        data.update(photo=cover["url"], caption=text)
    else:
        method = "sendMessage"
        photo = None
        preview = {"url": cover["url"], "prefer_large_media": True, "show_above_text": True} if cover and cover.get("url") else {
            "is_disabled": True}
        data.update(text=text, link_preview_options=encode(preview))
    files = [("photo", photo, "art-radar.jpg")] if photo else []
    return send_request(method, data, token, files)


def send_request(method, data, token, files=()):
    if files:
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                 for name, value in data.items()]
        for name, photo, filename in files:
            header = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            mime = "video/mp4" if filename.endswith(".mp4") else "image/jpeg"
            parts.extend([(header + f'Content-Type: {mime}\r\n\r\n').encode(), photo, b'\r\n'])
        parts.append(f'--{boundary}--\r\n'.encode())
        body, headers = b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    else:
        body, headers = urllib.parse.urlencode(data).encode(), {}
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get("ok") and int(result["result"]["message_id"]) > 0:
            message = result["result"]
            if data.get("disable_notification") == "true" or method == "editMessageText":
                blocks = (message.get("rich_message") or {}).get("blocks", [])
                mosaics = [block for block in blocks if block.get("type") == "collage"]
                photos = sum(block.get("type") == "photo" for block in blocks)
                photos += sum(block.get("type") == "photo" for mosaic in mosaics for block in mosaic.get("blocks", []))
                print(f"Telegram · {method} · сообщение {message['message_id']} · "
                      f"фото {photos or int(bool(message.get('photo')))} · "
                      f"мозаики {len(mosaics)} · видео {sum(b.get('type') == 'video' for b in blocks)} · "
                      f"разделители {sum(b.get('type') == 'divider' for b in blocks)} · "
                      f"кнопки {sum(b.get('type') == 'buttons' for b in blocks)} · "
                      f"настройки превью {bool(message.get('link_preview_options'))}")
            return int(message["message_id"])
    except urllib.error.HTTPError as error:
        if error.code in {400, 401, 403, 404, 429}:
            try:
                rejected = json.loads(error.read(32_768))
            except (OSError, ValueError):
                rejected = {}
            if isinstance(rejected, dict) and rejected.get("ok") is False and rejected.get("error_code") == error.code:
                raise TelegramRejected(f"Telegram явно отклонил публикацию (HTTP {error.code}).", error.code) from None
    except (OSError, ValueError, KeyError, TypeError, HTTPException):
        pass
    raise RuntimeError("Telegram не подтвердил отправку. Сначала проверьте тему и разрешите ожидающий выпуск.")


def visual_photos(card, domains):
    from PIL import Image

    photos = []
    for asset in card["images"]:
        original = journal_cover.artwork(asset, domains)
        if original is None:
            continue
        width, height = original.size
        original.thumbnail((2400, 2400))
        canvas = Image.new("RGB", original.size, "#f6f0e5")
        canvas.paste(original, (0, 0), original)
        target = io.BytesIO()
        canvas.save(target, "JPEG", quality=91, optimize=True)
        photos.append(({**asset, "aspect": width / height, "width": width, "height": height,
                        "sha256": original.info["art_radar_sha256"]}, target.getvalue()))
    return photos


def visual_domains(config):
    return list(dict.fromkeys([*config["image_domains"], *config.get("presentation", {}).get("visual_image_domains", []),
                               *config.get("article_domains", [])]))


def licensed_video(asset, config):
    if not asset or asset["license"] not in {"CC BY 4.0", "CC BY-SA 4.0", "CC0"} or not asset["credit"]:
        return None
    domains = config.get("presentation", {}).get("native_video_domains", [])
    if not sources.allowed(asset["source"], domains):
        return None
    try:
        data, mime = sources.request(asset["url"], domains, 19_000_001)
    except (OSError, ValueError):
        return None
    if mime != "video/mp4" or data[4:8] != b"ftyp" or len(data) > 19_000_000:
        return None
    digest = hashlib.sha256(data).hexdigest()
    if asset.get("sha256") and asset["sha256"] != digest:
        return None
    return {**asset, "video": True, "sha256": digest}, data


def check_visuals(config, saved=None, offline=False, keep_files=False):
    if config.get("visual_selection"):
        domains = visual_domains(config)
        return journal_visuals.VisualChecks(config, lambda assets: visual_photos({"images": assets}, domains),
                                           lambda asset: licensed_video(asset, config), saved=saved, offline=offline,
                                           keep_files=keep_files)
    return None


def visual_packets(plan, chat, thread, config, image_cache=None, prepared_image=None):
    domains = visual_domains(config)
    image_cache = {} if image_cache is None else image_cache

    def load(card):
        key = card["url"]
        if key not in image_cache:
            movie = licensed_video(card.get("video"), config)
            if movie:
                image_cache[key] = [movie]
            elif prepared_image:
                photos = []
                for asset in card["images"]:
                    cached = prepared_image(asset)
                    photos.extend([cached] if cached else visual_photos({"images": [asset]}, domains))
                image_cache[key] = photos
            else:
                image_cache[key] = visual_photos(card, domains)
        if config.get("visual_selection") and len(image_cache[key]) != (1 if card.get("video") else len(card["images"])):
            raise ValueError("Утверждённые иллюстрации изменились или недоступны. Выпуск не отправлен.")
        return image_cache[key]

    common = {"chat_id": chat, "disable_notification": "true"}
    if thread is not None:
        common["message_thread_id"] = thread
    packets = []
    for entry in plan:
        if entry["kind"] == "classic":
            text = visual_issue.classic_html(entry["header"], entry["cards"])
            if editorial.length(text) > 4096:
                raise ValueError("Совместимый выпуск превышает лимит Telegram.")
            links = {"is_disabled": True}
            video = next((card for card in entry["cards"] if card.get("video_link")), None)
            if video:
                links = {"url": video["url"], "prefer_large_media": True, "show_above_text": False}
            packets.append(("sendMessage", {**common, "parse_mode": "HTML", "text": text,
                                           "link_preview_options": encode(links)}, []))
            continue
        card = entry.get("card")
        if card and (entry.get("native") or not card["images"] or card["images"][0].get("role") == "portrait"):
            photos = []
        elif card:
            photos = load(card)
        if card and len(photos) < 2 and not any(asset.get("video") for asset, _ in photos):
            text = "\n\n".join(filter(None, (entry["header"], visual_issue.card_html(card))))
            markup = {"inline_keyboard": [[{"text": action["label"], "url": action["url"]}
                                            for action in visual_issue.card_actions(card)]]}
            data = {**common, "parse_mode": "HTML", "reply_markup": encode(markup)}
            if photos:
                asset, body = photos[0]
                data["caption"] = f"{text}\n<i>{visual_issue.credit_html(asset)}</i>"
                if editorial.length(data["caption"]) > editorial.PHOTO_CAPTION_LIMIT:
                    raise ValueError("Подпись к визуальной карточке превышает лимит Telegram.")
                packets.append(("sendPhoto", data, [("photo", body, "art-radar.jpg")]))
            else:
                preview = {"url": visual_issue.preview_url(card), "prefer_large_media": True, "show_above_text": True}
                data.update(text=text, link_preview_options=encode(preview))
                packets.append(("sendMessage", data, []))
            continue
        cards = [card] if card else entry["cards"]
        all_photos = [photos] if card else [load(item) for item in cards]
        media, files, figures = [], [], []
        for batch in all_photos:
            gallery = []
            for asset, body in batch:
                identifier = f"art{len(files) + 1}"
                attachment = {"type": "video" if asset.get("video") else "photo", "media": f"attach://{identifier}"}
                extension = "mp4" if asset.get("video") else "jpg"
                if asset.get("video"):
                    attachment.update(supports_streaming=True, duration=asset["seconds"])
                files.append((identifier, body, f"{identifier}.{extension}"))
                media.append({"id": identifier, "media": attachment})
                gallery.append((identifier, asset))
            figures.append(gallery)
        rich = {"html": visual_issue.rich_html(entry["header"], cards, figures), "media": media}
        packets.append(("sendRichMessage", {**common, "rich_message": encode(rich)}, files))
    return packets


def transmit_visual(packet, root, token, fallback=None):
    method, data, files = packet
    if root is not None:
        reply = {"message_id": root, "allow_sending_without_reply": True}
        data = {**data, "reply_parameters": encode(reply)}
    try:
        return send_request(method, data, token, files)
    except TelegramRejected as error:
        if not fallback or method != "sendRichMessage" or error.code != 404:
            raise
        print("Метод Rich Messages недоступен. Отправляется совместимый выпуск.")
        return transmit_visual(fallback, root, token)


def classic_fallbacks(plan, packets, chat, thread, config):
    return {id(packet): visual_packets([{**entry, "kind": "classic"}], chat, thread, config)[0]
            for entry, packet in zip(plan, packets) if entry["kind"] == "rich"}


def group_credentials():
    settings = tuple(os.getenv(name, "").strip() for name in
                     ("JOURNAL_TELEGRAM_TOKEN", "JOURNAL_TELEGRAM_CHAT", "JOURNAL_TELEGRAM_TOPIC"))
    if not any(settings):
        settings = tuple(os.getenv(name, "").strip() for name in
                         ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_THREAD_ID"))
    token, chat, thread = settings
    if not token or not re.fullmatch(r"-[1-9][0-9]*", chat) or not re.fullmatch(r"[1-9][0-9]*", thread):
        raise ValueError("Для журнала нужны токен, числовой ID рабочей группы и номер темы Telegram.")
    return settings


def telegram_info(token, method, **params):
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                     data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
        if data.get("ok") and isinstance(data.get("result"), dict):
            return data["result"]
    except (OSError, ValueError, TypeError, HTTPException):
        pass
    raise ValueError(f"Telegram не подтвердил настройки рабочей группы ({method}).")


def prepare_group(persist):
    token, chat, _ = group_credentials()
    group = telegram_info(token, "getChat", chat_id=chat)
    if str(group.get("id")) != chat or group.get("type") != "supergroup" or not group.get("is_forum"):
        raise ValueError("Telegram не подтвердил рабочую супергруппу с темами.")
    bot = telegram_info(token, "getMe")
    member = telegram_info(token, "getChatMember", chat_id=chat, user_id=bot["id"])
    status = member.get("status")
    permissions = member if status == "restricted" else group.get("permissions", {})
    if status not in {"administrator", "creator"} and not (
            (status == "member" or status == "restricted" and member.get("is_member"))
            and permissions.get("can_send_messages") and permissions.get("can_send_photos")):
        raise ValueError("Бот не подтвердил права отправки текста и фотографий в рабочей группе.")
    persist()
    print("Рабочая супергруппа и права бота подтверждены. Хранилище истории готово. Публикация не запускалась.")


def confirmed_test_chat():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    raw_id = os.getenv("ART_RADAR_TEST_CHAT_ID", "").strip()
    if not token or not raw_id.isascii() or not raw_id.isdecimal() or int(raw_id) <= 0:
        raise ValueError("Тест требует настроенного токена Art Radar и подтверждённого личного ART_RADAR_TEST_CHAT_ID.")
    chat = str(int(raw_id))
    if chat in {os.getenv("TELEGRAM_CHAT_ID"), os.getenv("JOURNAL_TELEGRAM_CHAT")}:
        raise ValueError("Рабочий Telegram-чат запрещён для тестовой отправки.")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/getChat",
                                     data=urllib.parse.urlencode({"chat_id": chat}).encode())
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
        if data.get("ok") and data["result"]["type"] == "private" and str(data["result"]["id"]) == chat:
            return token, chat
    except (OSError, ValueError, KeyError, TypeError, HTTPException):
        pass
    raise ValueError("Telegram не подтвердил личный чат. Откройте личный диалог с Art Radar и нажмите «Запустить».")


def test_delivery(audit, db, persist, artifacts=None):
    issues = audit.get("issues", [])
    if len(issues) != 1 or not audit.get("verified_links") or not issues[0].get("html"):
        raise ValueError("Для теста нужен один готовый выпуск из предпросмотра с проверенными ссылками.")
    issue = {"html": issues[0]["html"], "cover": issues[0]["cover"]}
    cover = issue["cover"]
    photo = None
    if cover and cover.get("tiles"):
        attachment = cover.get("attachment", {})
        path = artifacts / "cover-1.jpg" if artifacts else None
        if (not path or attachment.get("file") != path.name or not path.is_file()
                or path.stat().st_size > 10_000_000 or editorial.length(issue["html"]) > editorial.PHOTO_CAPTION_LIMIT):
            raise ValueError("Для личного теста нужна проверенная обложка и подпись до 1024 символов.")
        photo = path.read_bytes()
        if hashlib.sha256(photo).hexdigest() != attachment.get("sha256"):
            raise ValueError("Обложка отличается от проверенного предпросмотра.")
    token, chat = confirmed_test_chat()
    identity = json.dumps(issue, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha256((chat + "\n" + identity).encode()).hexdigest()
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db, timeout=15)) as connection:
        if connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='issues'").fetchone():
            raise ValueError("Для теста требуется отдельная база доставки.")
        connection.execute("CREATE TABLE IF NOT EXISTS test_deliveries (id TEXT PRIMARY KEY, telegram_id INTEGER)")
        with connection:
            saved = connection.execute("INSERT OR IGNORE INTO test_deliveries VALUES (?, NULL)", (key,))
            if not saved.rowcount:
                raise ValueError("Этот выпуск уже был отправлен либо ожидает подтверждения в тестовом чате.")
        persist()
        try:
            message_id = send(issue, token, chat, None, photo=photo)
        except TelegramRejected:
            with connection:
                connection.execute("DELETE FROM test_deliveries WHERE id=?", (key,))
            persist()
            raise
        except RuntimeError:
            raise RuntimeError("Telegram не подтвердил тестовую отправку. Проверьте личный чат; повтор этой версии заблокирован.") from None
        with connection:
            connection.execute("UPDATE test_deliveries SET telegram_id=? WHERE id=?", (message_id, key))
        persist()
    print(f"Тестовый выпуск доставлен в личный чат. Номер сообщения {message_id}.")


def test_visual_delivery(audit, db, persist, config, variant, update=False):
    issues = audit.get("issues", [])
    if len(issues) != 1 or not audit.get("verified_links") or not issues[0].get("visual"):
        raise ValueError("Нужен визуальный выпуск из проверенного предпросмотра.")
    issue = issues[0]["visual"]
    token, chat = confirmed_test_chat()
    plan = visual_issue.make_plan(issue, variant)
    packets = visual_packets(plan, chat, None, config)
    if variant == "rich":
        expected = sum(1 if card.get("video") else len(card["images"]) for card in issue["cards"])
        actual = sum(len(packet[2]) for packet in packets)
        if actual != expected:
            raise ValueError(f"В тестовом Rich Messages не хватает вложений: {actual} из {expected}.")
    fallbacks = classic_fallbacks(plan, packets, chat, None, config)
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db, timeout=15)) as connection:
        if connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='issues'").fetchone():
            raise ValueError("Для теста требуется отдельная база доставки.")
        outbox = visual_issue.Deliveries(connection)
        identity = json.dumps(plan, sort_keys=True, ensure_ascii=False)
        key = hmac.new(token.encode(), f"{chat}\n{variant}\n{identity}".encode(), hashlib.sha256).hexdigest()
        if update:
            receipt = connection.execute("SELECT telegram_id FROM visual_slots WHERE batch=? AND slot=1", (key,)).fetchone()
            if len(plan) != 1 or plan[0]["kind"] != "rich" or not receipt or not receipt[0] or outbox.plan(key) != plan:
                raise ValueError("Для обновления нужен ранее подтверждённый личный выпуск с тем же планом.")
            _, data, files = packets[0]
            edited = send_request("editMessageText", {"chat_id": chat, "message_id": receipt[0],
                                  "rich_message": data["rich_message"]}, token, files)
            if edited != receipt[0]:
                raise ValueError("Telegram вернул другой номер сообщения для обновления.")
            print(f"Личный тест · обновлено сообщение {edited}.")
            return
        with connection:
            if outbox.plan(key) is None:
                outbox.record(key, plan)
        receipts = outbox.deliver(key, packets,
                                  lambda packet, root: transmit_visual(packet, root, token, fallbacks.get(id(packet))), persist)
        print(f"Личный тест · Telegram подтвердил сообщения {', '.join(map(str, receipts))}.")


def check_platforms(config):
    credentials = {"YouTube": os.getenv("JOURNAL_YOUTUBE_API_KEY"), "VK": os.getenv("JOURNAL_VK_ACCESS_TOKEN")}
    for name, key in credentials.items():
        if not key:
            print(f"{name} · ключ не задан")
            continue
        try:
            if name == "YouTube":
                channel = next(p["channel"] for a in config["artist_radar"]["artists"] for p in a["profiles"]
                               if p["state"] == "atom")
                params = urllib.parse.urlencode({"part": "id", "id": channel, "key": key})
                result = sources.get_json(f"https://www.googleapis.com/youtube/v3/channels?{params}", "googleapis.com")
                if not any(item["id"] == channel for item in result["items"]):
                    raise ValueError("Канал не подтверждён")
                status = "channels.list · публичный канал подтверждён"
            else:
                source = next(s for s in config["sources"] if s.get("connector") == "vk" and s.get("owner"))
                result = list(sources.vk_entries({**source, "count": 1}, key))
                status = f"video.get · ответ получен, открытых роликов {len(result)}"
        except urllib.error.HTTPError as error:
            status = f"HTTP {error.code}"
        except ValueError as error:
            match = re.fullmatch(r"VK video.get · код \d+", str(error))
            status = match[0] if match else "Платформа не подтвердила метаданные"
        except (OSError, KeyError, TypeError):
            status = "Ответ недоступен"
        print(f"{name} · {status}")


def publish_visual(journal, issue, plan, packets, credentials, config, persist):
    token, chat, thread = credentials
    fallbacks = classic_fallbacks(plan, packets, chat, thread, config)
    try:
        receipts = journal.deliveries.deliver(f"issue:{issue}", packets,
                                             lambda packet, root: transmit_visual(packet, root, token, fallbacks.get(id(packet))),
                                             persist)
    except visual_issue.UnconfirmedCard as error:
        print(f"Выпуск №{issue}. {error}")
        return
    journal.resolve(issue, receipts[0], now_moscow())
    persist()
    print(f"Выпуск №{issue} подтверждён. Карточек {len(receipts)}.")


def scheduled(journal, config, archive, persist):
    now = now_moscow()
    if journal.daily(config, now):
        persist()
    if os.getenv("ART_RADAR_JOURNAL_PUBLISH") != "1":
        return
    last, count, pending = journal.clock()
    plan = journal.deliveries.plan(f"issue:{pending}") if pending else None
    if pending and not plan:
        print(f"Выпуск №{pending} ожидает подтверждения. Автоотправка приостановлена.")
        return
    time = dt.time.fromisoformat(os.getenv("ART_RADAR_JOURNAL_TIME") or config["publish_time"])
    interval = config["interval_days"]
    if not pending and not due(now, last, time, interval, journal.setting("attempted_on")):
        return
    credentials = group_credentials()
    _, chat, thread = credentials
    route = hashlib.sha256(f"{chat}\n{thread}".encode()).hexdigest()
    if plan:
        if plan.get("destination") != route:
            raise ValueError("Чат или тема ожидающего выпуска изменились. Сверьте настройки доставки.")
        if config.get("visual_selection") and plan.get("visual_policy") not in {1, 2, journal_visuals.POLICY}:
            raise ValueError("Ожидающий выпуск сформирован до проверки иллюстраций. Требуется сверка доставки.")
        entries = plan["entries"]
        packets = visual_packets(entries, chat, thread, config, prepared_image=journal.prepared_image)
        publish_visual(journal, pending, entries, packets, credentials, config, persist)
        return
    sequence = count + 1
    theme = next_theme(config, sequence)
    pool, history = combine(journal.items(), archive), journal.history()
    article_checks = journal_articles.ArticleChecks(config) if config.get("article_domains") else None
    visuals = check_visuals(config, keep_files=True)
    reserve = journal.editorial_reserve if config.get("accumulate_reserve") else None
    if reserve is not None:
        eligible, _ = editorial.assess(pool, config, now, history)
        with journal.conn:
            reserve.replenish(eligible, now, config["news_days"])
    picked, result = prepare(pool, config, history, now, theme, sequence, verify=True,
                             article_checks=article_checks, visual_checks=visuals, reserve=reserve, journal=journal)
    if not picked and theme:
        picked, result = prepare(pool, config, history, now, None, sequence, verify=True,
                                 article_checks=article_checks, visual_checks=visuals, reserve=reserve, journal=journal)
    variant = os.getenv("ART_RADAR_JOURNAL_FORMAT") or visual_issue.DEFAULT_FORMAT
    plan = visual_issue.make_plan(result["visual"], variant) if picked and variant != "legacy" else None
    packets = visual_packets(plan, chat, thread, config, prepared_image=journal.prepared_image) if plan else []
    reserved_plan = {"entries": plan, "destination": route, "visual_policy": result["visual"].get("policy")} if plan else None
    issue = journal.reserve(picked, now, time, interval, count, plan=reserved_plan)
    persist()
    if issue:
        if packets:
            publish_visual(journal, issue, plan, packets, credentials, config, persist)
            return
        try:
            message_id = send(result, *credentials)
        except TelegramRejected:
            journal.resolve(issue, None)
            persist()
            raise
        journal.resolve(issue, message_id, now_moscow())
        persist()
        print(f"Выпуск №{issue} подтверждён.")
    elif not picked:
        print("Подходящих материалов недостаточно. Следующая проверка — завтра.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--git-state", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Собрать предварительный просмотр")
    preview.add_argument("--out", type=Path, default=Path("journal-preview"))
    preview.add_argument("--theme")
    preview.add_argument("--examples", action="store_true")
    preview.add_argument("--automatic-issues", type=int, choices=(1, 3), default=0,
                         help="Три предпросмотра из автоматического сбора и изолированного резерва")
    preview.add_argument("--snapshot", type=Path, action="append",
                         help="Повторить отбор из одной или нескольких автоматических выгрузок")
    preview.add_argument("--refresh", action="store_true")
    preview.add_argument("--artist-search", type=Path, help="Использовать сохранённый реальный поиск Artist Radar")
    preview.add_argument("--selection", type=Path, help="Повторить отбор с зафиксированными историей и проверками ссылок")
    preview.add_argument("--offline", action="store_true", help="Локальная проверка оформления без сетевой валидации")
    preview.add_argument("--comparison", action="store_true", help="Один тестовый выпуск с разрешёнными иллюстрациями")
    preview.add_argument("--exclude-audit", type=Path, help="Исключить материалы уже показанного тестового выпуска")
    preview.add_argument("--illustrated", action="store_true", help="Проба вёрстки по свежим материалам с разрешёнными изображениями")
    preview.add_argument("--only-url", action="append", help="Ограничить предпросмотр конкретными проверяемыми публикациями")
    commands.add_parser("scheduled", help="Ежедневный сбор и проверка срока выпуска")
    commands.add_parser("prepare-group", help="Проверить права бота и подготовить хранилище группы без отправки")
    commands.add_parser("status", help="Последняя отправка и выпуск в ожидании")
    commands.add_parser("check-platforms", help="Проверить ключи публичных API без поиска и публикации")
    test_send = commands.add_parser("test-send", help="Отправить один выпуск в подтверждённый личный Telegram-чат")
    test_send.add_argument("--audit", type=Path, required=True)
    test_send.add_argument("--test-db", type=Path, default=DEFAULT_TEST_DB)
    test_send.add_argument("--variant", choices=("legacy", "rich", "classic"), default=visual_issue.DEFAULT_FORMAT)
    test_send.add_argument("--update", action="store_true", help="Обновить уже подтверждённый личный Rich Messages")
    reconcile = commands.add_parser("reconcile", help="Сверка с Telegram после прерванной отправки")
    reconcile.add_argument("issue", type=int)
    outcome = reconcile.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--sent", type=int, metavar="MESSAGE_ID")
    outcome.add_argument("--absent", action="store_true")
    args = parser.parse_args()
    if args.command == "test-send":
        if args.update and args.variant != "rich":
            parser.error("--update применяется к ранее отправленному Rich Messages")
        db = args.test_db.resolve()
        if db == args.db.resolve() or db == DEFAULT_DB.resolve():
            parser.error("История тестов хранится в отдельной базе")
        audit = sources.read(args.audit)
        config = sources.read(sources.ROOT / "sources.json")
        issues = audit.get("issues", [])
        if config.get("visual_selection") and (len(issues) != 1 or not verified_visual_issue(issues[0].get("visual") or {})):
            parser.error("Для личной отправки нужен выпуск с подтверждёнными иллюстрациями по действующим правилам")
        persist_test = lambda: checkpoint(db, TEST_STATE_BRANCH) if args.git_state else None
        if args.variant == "legacy":
            test_delivery(audit, db, persist_test, args.audit.parent)
        else:
            test_visual_delivery(audit, db, persist_test, config, args.variant, args.update)
        return
    if args.command == "preview" and args.selection and (not args.snapshot or args.offline or args.artist_search):
        parser.error("--selection требует --snapshot и использует сохранённые сетевые проверки")
    if args.command == "preview" and args.comparison and (not args.snapshot or args.offline or args.examples):
        parser.error("--comparison требует --snapshot и проверенных ссылок")
    if args.command == "preview" and args.exclude_audit and args.selection:
        parser.error("--exclude-audit применяется при новом отборе, без --selection")
    if args.command == "preview" and args.illustrated and (args.selection or args.comparison or args.examples):
        parser.error("--illustrated применяется при новом отборе одного выпуска")
    if args.command == "preview" and args.only_url and (args.selection or args.examples or args.comparison or args.illustrated):
        parser.error("--only-url применяется при новом отборе одного выпуска")
    if args.command == "preview" and (args.offline and args.refresh or args.snapshot and args.refresh):
        parser.error("--refresh требует сети и не совмещается с --snapshot")
    if args.command == "preview" and args.examples and (args.theme or args.offline and not args.snapshot):
        parser.error("Пять обычных выпусков требуют автоматической выгрузки. Для офлайн-повтора укажите --snapshot.")
    if args.command == "preview" and args.automatic_issues and (args.offline or args.theme or args.only_url or args.selection
            or args.comparison or args.illustrated or args.examples or not (args.snapshot or args.refresh)):
        parser.error("Автоматические выпуски требуют полной свежей или сохранённой автоматической выгрузки.")
    if args.command == "reconcile" and args.sent is not None and args.sent <= 0:
        parser.error("--sent требует положительного номера сообщения")
    config = artist_radar.configure(sources.read(sources.ROOT / "sources.json"))
    if args.command == "preview" and args.automatic_issues:
        presentation = config.get("presentation", {})
        config = {**config, "article_edits": {}, "presentation": {**presentation, "cards": {},
                  "visual_images": {}, "cover_images": {}}}
    if args.command == "check-platforms":
        check_platforms(config)
        return
    archive = sources.read(sources.ROOT / "materials.json")
    if args.command == "preview" and args.theme and args.theme not in config["themes"]:
        parser.error("Неизвестная тема. Доступные темы: " + ", ".join(config["themes"]))
    automatic = args.command == "preview" and args.automatic_issues
    db = ((args.out / "editorial.sqlite3") if automatic else args.db).resolve()
    use_memory = args.command == "preview" and not automatic and (args.examples or not db.is_file())
    if not use_memory:
        db.parent.mkdir(parents=True, exist_ok=True)

    def persist():
        if args.git_state:
            checkpoint(db)

    with closing(sqlite3.connect(":memory:" if use_memory else db, timeout=15)) as connection:
        journal = Journal(connection)
        if args.command == "scheduled":
            scheduled(journal, config, archive, persist)
        elif args.command == "prepare-group":
            prepare_group(persist)
        elif args.command == "status":
            last, count, pending = journal.clock()
            print(encode({"last_success": last, "published": count, "pending": pending,
                          "collected_on": journal.setting("collected_on")}))
        elif args.command == "reconcile":
            key = f"issue:{args.issue}"
            if journal.deliveries.plan(key) is not None:
                if journal.clock()[2] != args.issue:
                    raise ValueError("Этот визуальный выпуск уже закрыт.")
                journal.deliveries.reconcile(key, args.sent)
            else:
                journal.resolve(args.issue, args.sent)
            persist()
        else:
            snapshot = None
            now = now_moscow()
            replay = sources.read(args.selection) if args.selection else None
            artist_snapshot = sources.read(args.artist_search) if args.artist_search else None
            if args.snapshot:
                snapshot = load_harvest(args.snapshot, config)
                if artist_snapshot:
                    snapshot["items"] = combine(snapshot["items"], artist_snapshot["items"])
                    snapshot["collected_at"] = max(snapshot["collected_at"], artist_snapshot["collected_at"])
                    snapshot["sources"] = [r for r in snapshot["sources"] if r["source"] != "artist-radar"]
                    snapshot["sources"].append({"source": "artist-radar", "name": "Artist Radar", "status": "ok",
                                                "count": len(artist_snapshot["items"]), "queries": artist_snapshot["queries"]})
            elif args.refresh or args.examples:
                live, source_report = collected_inputs(config, now, artist_snapshot, previous=journal.items())
                snapshot = {"collected_at": now_moscow().isoformat(), "sources": source_report, "items": live}
            live = snapshot["items"] if snapshot else []
            source_report = snapshot["sources"] if snapshot else []
            if snapshot:
                now = dt.datetime.fromisoformat(snapshot["collected_at"]).astimezone(MOSCOW)
            if not (args.examples or args.snapshot):
                live = combine(journal.items(), live)
                source_report = source_report or json.loads(journal.setting("source_report") or "[]")
            pool = combine(live, [] if args.examples or args.snapshot or args.automatic_issues else archive)
            if args.only_url:
                keys = {sources.canonical(url) for url in args.only_url}
                pool = [item for item in combine(live, archive) if sources.canonical(item["url"]) in keys]
            if args.illustrated:
                pool = live
            history = journal.history()
            if args.exclude_audit:
                old = sources.read(args.exclude_audit)
                history.extend(row for issue in old["issues"] for row in issue["items"]
                               if row["decision"] == "Выбран в выпуск")
            _, count, _ = journal.clock()
            if replay:
                pool = combine(pool, replay["cards_outside_harvest"])
                if "pool_urls" in replay or "illustrated_urls" in replay:
                    keys = set(replay.get("pool_urls") or replay.get("illustrated_urls", []))
                    pool = [item for item in pool if sources.canonical(item["url"]) in keys]
                history, count = replay["history"], replay["count"]
                selections = [(dt.datetime.fromisoformat(date), theme) for date, theme in replay["schedule"]]
            else:
                selections = [(now - dt.timedelta(days=i * config["interval_days"]), None)
                              for i in reversed(range(5))] if args.examples else [(now, args.theme or next_theme(config, count + 1))]
                if args.automatic_issues:
                    selections = [(now, None)] * args.automatic_issues
            if args.comparison:
                style = config["presentation"]
                keys = set(style["visual_comparison"])
                mapping = style["visual_comparison_sources"]
                pool = [{**item, "source": mapping.get(item["source"], item["source"])} for item in combine(pool, archive)
                        if sources.canonical(item["url"]) in keys]
            pool = sorted(pool, key=lambda card: sources.canonical(card["url"]))
            captured_urls = {sources.canonical(item["url"]): item for item in snapshot["items"]} if snapshot else {}
            inputs = {"schedule": [[date.isoformat(), theme] for date, theme in selections], "count": count,
                      "history": list(history), "theme_explicit": bool(args.theme),
                      "cards_outside_harvest": [item for item in pool if captured_urls.get(sources.canonical(item["url"])) != item]}
            if args.illustrated or args.only_url:
                inputs["pool_urls"] = [sources.canonical(item["url"]) for item in pool]
            elif replay and ("pool_urls" in replay or "illustrated_urls" in replay):
                inputs["pool_urls"] = [sources.canonical(item["url"]) for item in pool]
            verify = not args.offline
            if replay:
                verify = LinkChecks(replay=replay["checks"], allow_new=args.comparison or "visuals" not in replay)
                inputs["theme_explicit"] = replay["theme_explicit"]
            elif args.examples and verify:
                worth_checking = {}
                for as_of, _ in selections:
                    candidates, _ = editorial.assess(pool, config, as_of)
                    worth_checking.update((item.key, item.item) for item in candidates)
                verify = LinkChecks(verify=batch_verifier(worth_checking.values(), config))
            elif verify:
                verify = LinkChecks()
            article_checks = None
            if config.get("article_domains"):
                if args.offline:
                    article_checks = journal_articles.ArticleChecks(config, read=lambda *_: None)
                else:
                    article_checks = journal_articles.ArticleChecks(config, saved=replay.get("articles") if replay else None,
                                                                   allow_new=bool(replay and "visuals" not in replay))
            preparation_cache = journal if not (replay or args.offline or args.comparison) else None
            visuals = check_visuals(config, saved=replay.get("visuals") if replay else None, offline=args.offline,
                                    keep_files=preparation_cache is not None)
            issues = []
            reserve = None
            reserve_report = None
            if not (args.examples or replay or args.offline or args.comparison or args.illustrated or args.only_url):
                reserve = journal.editorial_reserve if config.get("accumulate_reserve") else None
            if reserve is not None:
                eligible, _ = editorial.assess(pool, config, now, history)
                with journal.conn:
                    reserve.replenish(eligible, now, config["news_days"])
                if args.automatic_issues:
                    qualify_video_reserve(reserve, eligible, now, verify)
                reserve_report = {"before": reserve.report(now), "issues": []}
                pool = combine(pool, reserve.pool(now))
                inputs["uses_reserve"] = True
                inputs["cards_outside_harvest"] = [item for item in pool
                                                  if item != captured_urls.get(sources.canonical(item["url"]))]
            elif replay and replay.get("uses_reserve"):
                inputs["uses_reserve"] = True
            for sequence, (as_of, theme) in enumerate(selections, count + 1):
                picked, issue = prepare(pool, config, history, as_of, theme, sequence, verify=verify,
                                        article_checks=article_checks, visual_checks=visuals, reserve=reserve,
                                        replay_reserve=bool(replay and replay.get("uses_reserve")), journal=preparation_cache)
                if not picked and theme and not inputs["theme_explicit"]:
                    picked, issue = prepare(pool, config, history, as_of, None, sequence, verify=verify,
                                            article_checks=article_checks, visual_checks=visuals, reserve=reserve,
                                            replay_reserve=bool(replay and replay.get("uses_reserve")), journal=preparation_cache)
                issue["as_of"] = as_of.date().isoformat()
                if args.automatic_issues:
                    issue["cover"] = None
                issues.append(issue)
                history.extend({**candidate.item, "_issued_at": as_of.isoformat()} for candidate in picked)
                if reserve is not None:
                    reserve_report["issues"].append({"selected": [p.key for p in picked if p.item.get("from_reserve")
                                                                 or p.item.get("archive_index")],
                                                     "screened": reserve.report(as_of)})
                    with journal.conn:
                        reserve.remove(p.key for p in picked)
                if args.automatic_issues:
                    print(f"Автоматический выпуск {sequence}: {len(picked)} материалов", flush=True)
            if verify:
                inputs["checks"] = verify.snapshot()
                if article_checks:
                    inputs["articles"] = article_checks.results
                if visuals:
                    inputs["visuals"] = visuals.results
            save_preview(args.out, issues, source_report, now.date().isoformat(), not args.offline, snapshot,
                         inputs if verify and snapshot else None, config.get("artist_radar"), reserve_report)
            if args.automatic_issues and any(not verified_visual_issue(issue.get("visual", {})) for issue in issues):
                raise ValueError("Автоматическая проверка не собрала запрошенные визуальные выпуски.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
