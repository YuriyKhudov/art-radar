"""Presentation and resumable delivery of visual Telegram issues."""

import html
import json
import re
import time

from radar import editorial_sources as sources


class TelegramRejected(RuntimeError):
    """Telegram explicitly declined a request, so no message was created."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class UnconfirmedCard(RuntimeError):
    """A transport attempt cannot be repeated until it has been reconciled."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Deliveries:
    def __init__(self, connection):
        self.conn = connection
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS visual_batches (id TEXT PRIMARY KEY, plan TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS visual_slots (
                batch TEXT NOT NULL, slot INTEGER NOT NULL, telegram_id INTEGER,
                PRIMARY KEY(batch, slot), UNIQUE(batch, telegram_id)
            );
        """)

    def plan(self, key):
        row = self.conn.execute("SELECT plan FROM visual_batches WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def record(self, key, plan):
        self.conn.execute("INSERT INTO visual_batches VALUES (?, ?)", (key, encode(plan)))

    def reconcile(self, key, message_id):
        with self.conn:
            pending = self.conn.execute(
                "SELECT slot FROM visual_slots WHERE batch=? AND telegram_id IS NULL", (key,)).fetchall()
            if len(pending) != 1:
                raise ValueError("Для этого выпуска нет единственной карточки, ожидающей сверки.")
            slot = pending[0][0]
            if message_id:
                self.conn.execute("UPDATE visual_slots SET telegram_id=? WHERE batch=? AND slot=?",
                                  (message_id, key, slot))
            else:
                self.conn.execute("DELETE FROM visual_slots WHERE batch=? AND slot=?", (key, slot))

    def deliver(self, key, plan, transmit, persist, pause=time.sleep):
        receipts = []
        for index, entry in enumerate(plan, 1):
            with self.conn:
                claim = self.conn.execute("INSERT OR IGNORE INTO visual_slots VALUES (?, ?, NULL)", (key, index))
                previous = self.conn.execute("SELECT telegram_id FROM visual_slots WHERE batch=? AND slot=?",
                                             (key, index)).fetchone()[0]
            if not claim.rowcount:
                if previous is None:
                    raise UnconfirmedCard(f"Карточка №{index} ожидает сверки с Telegram.")
                receipts.append(previous)
                continue
            persist()
            try:
                message_id = transmit(entry, receipts[0] if receipts else None)
            except TelegramRejected:
                self.reconcile(key, None)
                persist()
                raise
            self.reconcile(key, message_id)
            persist()
            receipts.append(message_id)
            if index < len(plan):
                pause(1.1)
        return receipts


DEFAULT_FORMAT = "rich"


def make_plan(issue, variant):
    cards = issue["cards"]
    if not 2 <= len(cards) <= 4:
        raise ValueError("В визуальном выпуске требуется от двух до четырёх карточек.")
    header = issue["header"]
    if variant == "classic":
        return [{"kind": variant, "header": header, "cards": cards}]
    if variant == "rich":
        plan, group = [], []
        for card in cards:
            if card.get("video_link") and not card["images"] and not card.get("video"):
                if group:
                    plan.append({"kind": "rich", "header": header if not plan else "", "cards": group})
                    group = []
                plan.append({"kind": "card", "header": header if not plan else "", "card": card, "native": True})
            else:
                group.append(card)
        if group:
            plan.append({"kind": "rich", "header": header if not plan else "", "cards": group})
        return plan
    if variant != "cards":
        raise ValueError("Неизвестный визуальный формат.")
    return [{"kind": "card", "header": header if index == 0 else "", "card": card}
            for index, card in enumerate(cards)]


def credit_html(asset):
    name = html.escape(asset["credit"])
    source = html.escape(asset.get("source", asset["url"]), quote=True)
    license_name = asset["license"]
    license_urls = {"CC BY 4.0": "https://creativecommons.org/licenses/by/4.0/",
                    "CC BY-SA 4.0": "https://creativecommons.org/licenses/by-sa/4.0/"}
    if license_name in license_urls:
        license_html = f'<a href="{license_urls[license_name]}">{license_name}</a>'
    else:
        license_html = html.escape(license_name)
    return f'<a href="{source}">{name}</a> · {license_html}'


def card_html(card):
    title = html.escape(card["title"])
    url = html.escape(card["url"], quote=True)
    title = title if card.get("article") else f'<a href="{url}">{title}</a>'
    text = html.escape(card["section"])
    if card.get("artist"):
        text += f'\n<b>{html.escape(card["artist"])}</b>'
    text += f'\n<b>{title}</b>'
    if card["summary"]:
        text += f'\n{html.escape(card["summary"])}'
    if card["byline"]:
        text += f'\n<i>{html.escape(card["byline"])}</i>'
    return text


def card_actions(card):
    return card.get("actions") or [{"label": card["action"], "url": card["url"]}]


def preview_url(card):
    return next((action["url"] for action in card_actions(card) if sources.video_platform(action["url"]) == "YouTube"),
                card["url"])


def mosaic_rows(figures):
    """Leave extreme and markedly different proportions in separate full-width blocks."""
    rows = []
    for figure in figures:
        asset = figure[1]
        group = not asset.get("video") and .59 <= asset.get("aspect", 1.0) <= 2.05
        if group and rows and rows[-1][0] and len(rows[-1][1]) < 5:
            rows[-1][1].append(figure)
        else:
            rows.append((group, [figure]))
    for _, row in rows:
        if len(row) in {3, 4} and sum(asset.get("aspect", 1) < .85 for _, asset in row) == 1:
            row.sort(key=lambda figure: figure[1].get("aspect", 1) >= .85)
    return [row for _, row in rows]


def rich_html(header, cards, photos):
    parts = [f'<p>{header.replace(chr(10), "<br/>")}</p>'] if header else []
    credits = []
    for index, (card, figures) in enumerate(zip(cards, photos)):
        if index:
            parts.append("<hr/>")
        for row in mosaic_rows(figures):
            images = "".join(f'<video src="tg://video?id={identifier}"></video>' if asset.get("video")
                             else f'<img src="tg://photo?id={identifier}"/>' for identifier, asset in row)
            parts.append(f'<tg-collage>{images}</tg-collage>' if len(row) > 1 else images)
        for paragraph in card_html(card).split("\n\n"):
            parts.append(f'<p>{paragraph.replace(chr(10), "<br/>")}</p>')
        url = html.escape(card["url"], quote=True)
        action = html.escape(card["action"])
        if card.get("article"):
            parts.append(f'<p><a href="{url}">{action}</a></p>')
        else:
            buttons = "".join(f'<tg-button type="url" url="{html.escape(link["url"], quote=True)}">'
                              f'{html.escape(link["label"])}</tg-button>' for link in card_actions(card))
            parts.append(f'<tg-button-row align="left">{buttons}</tg-button-row>')
        credits.extend(credit_html(asset) for _, asset in figures)
    if credits:
        links = "<br/>".join(dict.fromkeys(credits))
        parts.append(f'<details><summary>Иллюстрации и лицензии</summary><p>{links}</p></details>')
    return "".join(parts)


def classic_html(header, cards):
    body = []
    for card in cards:
        credits = [*card["images"], *([card["video"]] if card.get("video") else [])]
        images = "\n".join(credit_html(asset) for asset in credits)
        actions = " · ".join(f'<a href="{html.escape(link["url"], quote=True)}">{html.escape(link["label"])}</a>'
                             for link in card_actions(card))
        if card.get("article"):
            sentences = re.split(r"(?<=[.!?])\s+", card["summary"])
            paragraphs = []
            for sentence in sentences:
                if sum(len(value) + 1 for value in paragraphs) + len(sentence) > 540 and paragraphs:
                    break
                paragraphs.append(sentence)
            card = {**card, "summary": " ".join(paragraphs)}
        body.append(f'{card_html(card)}\n{actions}' + (f"\n<i>{images}</i>" if images else ""))
    return (header + "\n\n" if header else "") + "\n\n────────\n\n".join(body)


def gallery_preview(issue):
    header = issue["header"].replace("\n", "<br>")
    cards, credits = [], []
    for card in issue["cards"]:
        groups = []
        movie = card.get("video")
        if movie:
            groups.append(f'<video controls preload="metadata" src="{html.escape(movie["url"], quote=True)}"></video>')
            credits.append(credit_html(movie))
        else:
            for group in mosaic_rows(list(enumerate(card["images"]))):
                figures = []
                for _, asset in group:
                    url = html.escape(asset["url"], quote=True)
                    description = html.escape(asset["credit"], quote=True)
                    figures.append(f'<a href="{url}" target="_blank" rel="noopener"><img src="{url}" alt="{description}"></a>')
                    credits.append(credit_html(asset))
                primary = " hero" if len(group) in {3, 4} and group[0][1].get("aspect", 1) < .85 else ""
                groups.append(f'<div class="mosaic count-{len(group)}{primary}">{"".join(figures)}</div>')
        body = "".join(f'<p>{text.replace(chr(10), "<br>")}</p>' for text in card_html(card).split("\n\n"))
        style = "source" if card.get("article") else "action"
        actions = "".join(f'<a class="{style}" href="{html.escape(link["url"], quote=True)}">'
                          f'{html.escape(link["label"])}</a>' for link in card_actions(card))
        cards.append(f'<article>{"".join(groups)}{body}<div class="actions">{actions}</div></article>')
    page = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Визуальный Art Radar</title>
<style>
body{background:#f6f0e5;color:#252b26;font:16px/1.56 system-ui,sans-serif;margin:30px auto;padding:0 16px;max-width:650px}
header{font-size:1.2em;margin-bottom:20px;padding-bottom:14px;border-bottom:2px solid #953e2d}
article{margin:0 0 26px;padding:0 0 24px;border-bottom:1px solid #d4c6b7;overflow-wrap:anywhere}
img,video{display:block;width:100%;height:auto;margin:auto}a{color:#803628;text-underline-offset:3px}
.mosaic{display:grid;align-items:center;gap:4px;margin:0 0 12px;grid-template-columns:1fr}
.count-2,.count-4,.count-5{grid-template-columns:repeat(2,minmax(0,1fr))}.count-3{grid-template-columns:repeat(3,minmax(0,1fr))}
.count-5{grid-template-columns:repeat(6,minmax(0,1fr))}.count-5 a{grid-column:span 2}.count-5 a:nth-child(n+4){grid-column:span 3}
.hero{grid-template-columns:1.65fr 1fr}.hero a:first-child{grid-row:span 2}.count-4.hero a:first-child{grid-row:span 3}
.actions{display:flex;flex-wrap:wrap;gap:8px}.action{display:inline-block;padding:5px 12px;border:1px solid currentColor;border-radius:5px}
i,.source,details{color:#5b5349;font-size:.88em}p{margin:10px 0}summary{cursor:pointer}
</style></head><body>"""
    references = "<details><summary>Иллюстрации и лицензии</summary><p>" + "<br>".join(credits) + "</p></details>" if credits else ""
    return page + f"<header>{header}</header>" + "".join(cards) + references + "</body></html>\n"
