"""Presentation and resumable delivery of visual Telegram issues."""

import html
import json
import time


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
    if not 3 <= len(cards) <= 4:
        raise ValueError("В визуальном выпуске требуется от трёх до четырёх карточек.")
    header = issue["header"]
    if variant in {"rich", "classic"}:
        return [{"kind": variant, "header": header, "cards": cards}]
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
    text = f'{html.escape(card["section"])}\n<b><a href="{url}">{title}</a></b>'
    if card["summary"]:
        text += f'\n{html.escape(card["summary"])}'
    if card["byline"]:
        text += f'\n<i>{html.escape(card["byline"])}</i>'
    return text


def rich_html(header, cards, photos):
    parts = [f'<p>{header.replace(chr(10), "<br/>")}</p>']
    for index, (card, figures) in enumerate(zip(cards, photos)):
        if index:
            parts.append("<hr/>")
        images = [f'<img src="tg://photo?id={identifier}"/>' for identifier, _ in figures]
        if len(images) > 1:
            credits = "<br/>".join(credit_html(asset) for _, asset in figures)
            parts.append(f'<tg-slideshow>{"".join(images)}<figcaption>{credits}</figcaption></tg-slideshow>')
        elif images:
            parts.append(f'<figure>{images[0]}<figcaption>{credit_html(figures[0][1])}</figcaption></figure>')
        parts.append(f'<p>{card_html(card).replace(chr(10), "<br/>")}</p>')
        url = html.escape(card["url"], quote=True)
        action = html.escape(card["action"])
        parts.append(f'<tg-button-row align="left"><tg-button type="url" url="{url}">{action}</tg-button></tg-button-row>')
    return "".join(parts)


def classic_html(header, cards):
    body = []
    for card in cards:
        images = "\n".join(credit_html(asset) for asset in card["images"])
        action = html.escape(card["action"])
        url = html.escape(card["url"], quote=True)
        body.append(f'{card_html(card)}\n<a href="{url}">{action}</a>' + (f"\n<i>{images}</i>" if images else ""))
    return header + "\n\n" + "\n\n────────\n\n".join(body)


def gallery_preview(issue):
    header = issue["header"].replace("\n", "<br>")
    cards = []
    for card in issue["cards"]:
        images = "".join(f'<figure><img src="{html.escape(asset["url"], quote=True)}" alt="">'
                         f'<figcaption>{credit_html(asset)}</figcaption></figure>' for asset in card["images"])
        if not images:
            images = ""
        body = card_html(card).replace("\n", "<br>")
        action = html.escape(card["action"])
        url = html.escape(card["url"], quote=True)
        cards.append(f'<article>{images}<p>{body}</p><a class="action" href="{url}">{action}</a></article>')
    page = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Визуальный Art Radar</title>
<style>
body{background:#f6f0e5;color:#252b26;font:16px/1.56 system-ui,sans-serif;margin:30px auto;padding:0 16px;max-width:650px}
header{font-size:1.2em;margin-bottom:20px;padding-bottom:14px;border-bottom:2px solid #953e2d}
article{background:#fcf8f0;margin:0 0 20px;padding:14px;border:1px solid #dbd2c4;border-radius:5px}
figure{margin:0 0 6px}img{display:block;max-width:100%;height:auto;margin:0 auto}
figcaption,small{font-size:.75em;color:#5b5349}a{color:#803628;text-underline-offset:3px}
.action{display:inline-block;padding:5px 12px;border:1px solid currentColor;border-radius:5px}
i{color:#5b5349;font-size:.88em}p{margin:8px 0}
</style></head><body>"""
    return page + f"<header>{header}</header>" + "".join(cards) + "</body></html>\n"
