"""Ежедневная подборка Art Radar в ветку Telegram-группы.

Запускается на GitHub каждое утро (.github/workflows/telegram.yml). Нужны секреты репозитория:
  TELEGRAM_BOT_TOKEN   ключ бота от @BotFather
  TELEGRAM_CHAT_ID     номер группы (например, -1001234567890)
  TELEGRAM_THREAD_ID   номер ветки (темы) внутри группы

  python -m radar.telegram_post --dry-run   показать текст поста, ничего не отправляя
  python -m radar.telegram_post --find-ids  после сообщения боту в ветке показать номера группы и ветки
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import radar.notify as notify

SITE = "https://yuriykhudov.github.io/art-radar/"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def views_text(n):
    if not n:
        return ""
    if n >= 1_000_000:
        return f" · 👁 {n / 1e6:.1f} млн".replace(".0 ", " ").replace(".", ",")
    if n >= 1000:
        return f" · 👁 {round(n / 1000)} тыс."
    return f" · 👁 {n}"


def collect_data():
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    notify.DB_FILE = Path(tmp.name) / "tg.db"
    notify.LOG_FILE = Path(tmp.name) / "tg.log"
    from radar import app
    settings = {**notify.DEFAULT_SETTINGS, "notify_channels": "all"}
    notify.collect(settings)
    from radar.news import collect_news, recent_articles
    collect_news()
    opps = [a for a in recent_articles() if a["category"] == "opportunities"]
    return app.recent_videos(settings), json.loads(notify.SEED_FILE.read_text(encoding="utf-8"))["fairs"], opps


NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
CAPTION_LIMIT = 1024  # столько символов Telegram разрешает в подписи к альбому
# Louisiana Channel и другие снимают не только художников — интервью с писателями и архитекторами в подборку не берём.
NOT_ART = re.compile(r"\s*(writer|author|poet|novelist|architect|musician|composer|filmmaker|director|singer)\b", re.I)


def visible_len(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def build_post(videos, fairs, opps=(), now=None, weekly=False):
    """Подпись к посту и картинки-превью видео для альбома. По понедельникам — «Лучшее за неделю»."""
    now = now or dt.datetime.now(dt.timezone.utc)
    top = 6 if weekly else 5
    since = (now - dt.timedelta(days=7 if weekly else 1)).isoformat()
    from radar.translate import load
    ru = load()  # переводы заголовков, накопленные сайтом
    name = lambda v: ru.get(v["title"], v["title"])
    fresh = [v for v in videos if v["published"] >= since and "/shorts/" not in v["url"] and not NOT_ART.match(v["title"])]
    fresh.sort(key=lambda v: -(v["views"] or 0))
    picked = fresh[:top]
    today = now.date()
    upcoming = sorted((f for f in fairs if f.get("start") and dt.date.fromisoformat(f.get("end") or f["start"]) >= today),
                      key=lambda f: f["start"])

    soon = sorted((a for a in opps if a.get("deadline") and a["deadline"] >= today.isoformat()),
                  key=lambda a: a["deadline"])[:3]

    def compose(title_len):
        lines = [f"🎨 <b>Art Radar · {today.day} {MONTHS[today.month - 1]}</b>",
                 "🏆 <b>Лучшее за неделю</b>" if weekly else "🔥 <b>Самое смотрибельное за сутки</b>", ""]
        if picked:
            for i, v in enumerate(picked):
                t = name(v) if len(name(v)) <= title_len else name(v)[:title_len - 1].rstrip() + "…"
                lines.append(f"{NUM[i]} <a href=\"{html.escape(v['url'])}\">{html.escape(t)}</a>")
                lines.append(f"      <i>{html.escape(v['channel'])}{views_text(v['views'])}</i>")
        else:
            lines.append("За сутки новых видео не вышло — загляните в ленту на сайте.")
        if weekly and soon:
            lines += ["", "🎯 <b>Возможности для художников</b>"]
            for a in soon:
                t = name(a) if len(name(a)) <= title_len else name(a)[:title_len - 1].rstrip() + "…"
                d = dt.date.fromisoformat(a["deadline"])
                lines.append(f"• <a href=\"{html.escape(a['id'])}\">{html.escape(t)}</a> — до {d.day} {MONTHS[d.month - 1]}")
        if upcoming:
            f = upcoming[0]
            days = (dt.date.fromisoformat(f["start"]) - today).days
            when = "идёт прямо сейчас!" if days <= 0 else f"через {days} дн."
            lines += ["", f"🗓 <b>{html.escape(f['name'])}</b> ({html.escape(f['city'])}) — {when}"]
        lines += ["", f"👉 <a href=\"{SITE}\">Все видео, новости и ярмарки — в Art Radar</a>"]
        return "\n".join(lines)

    caption = compose(90)
    for title_len in (70, 55, 40, 30):  # если не помещается — укорачиваем названия видео
        if visible_len(caption) <= CAPTION_LIMIT:
            break
        caption = compose(title_len)
    photos = [f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg" for v in picked]
    return caption, photos, len(fresh)


def telegram(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data),
                                    timeout=30) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:  # Telegram объясняет причину в теле ответа
        res = json.loads(e.read() or b"{}")
        res.setdefault("description", f"HTTP {e.code}")
    if not res.get("ok"):
        raise RuntimeError(res.get("description"))
    return res["result"]


def last_topic(token):
    """Номер группы и ветки из самого свежего сообщения боту в ветке (для настройки)."""
    updates = telegram(token, "getUpdates", allowed_updates=json.dumps(["message", "my_chat_member", "chat_member"]))
    me = telegram(token, "getMe")
    seen = []
    for u in reversed(updates):
        m = u.get("message") or {}
        chat = m.get("chat") or (u.get("my_chat_member") or {}).get("chat") or {}
        kind = next((k for k in u if k != "update_id"), "?")
        seen.append(f"{kind} из «{chat.get('title') or chat.get('type', '?')}»")
        if m and chat.get("type") in ("group", "supergroup"):
            return {"chat_id": chat["id"], "thread_id": m.get("message_thread_id") or "", "title": chat.get("title", ""),
                    "bot": me.get("username")}
    return {"bot": me.get("username"), "seen": seen[:10]}


def find_ids(token):
    """Показать номер группы и ветки из последних сообщений, адресованных боту."""
    seen = set()
    for u in telegram(token, "getUpdates"):
        m = u.get("message") or u.get("channel_post") or {}
        chat = m.get("chat", {})
        key = (chat.get("id"), m.get("message_thread_id"))
        if chat and key not in seen:
            seen.add(key)
            topic = (m.get("reply_to_message") or {}).get("forum_topic_created", {}).get("name", "")
            print(f"Группа: {chat.get('title')}  TELEGRAM_CHAT_ID={chat.get('id')}  "
                  f"TELEGRAM_THREAD_ID={m.get('message_thread_id')}  {topic}")
    if not seen:
        print("Сообщений боту пока нет. Напишите в нужной ветке: /start@имя_бота — и запустите снова.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--find-ids", action="store_true")
    p.add_argument("--weekly", action="store_true", help="собрать недельную подборку (по понедельникам — сама)")
    p.add_argument("--last-topic-json", action="store_true")
    args = p.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if args.last_topic_json:
        print(json.dumps(last_topic(token), ensure_ascii=False))
        return
    if args.find_ids:
        find_ids(token)
        return
    videos, fairs, opps = collect_data()
    moscow = dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))
    weekly = args.weekly or moscow.weekday() == 0  # понедельник
    caption, photos, n = build_post(videos, fairs, opps, weekly=weekly)
    if args.dry_run or not token:
        print(caption)
        print("\nКартинки альбома:", *photos, sep="\n  ")
        print(f"\n[проба: пост не отправлен; свежих видео за сутки: {n}; символов в подписи: {visible_len(caption)}]")
        return
    common = {"chat_id": os.environ["TELEGRAM_CHAT_ID"]}
    if os.environ.get("TELEGRAM_THREAD_ID"):
        common["message_thread_id"] = os.environ["TELEGRAM_THREAD_ID"]
    if len(photos) >= 2:
        media = [{"type": "photo", "media": url} for url in photos]
        media[0].update(caption=caption, parse_mode="HTML")
        telegram(token, "sendMediaGroup", media=json.dumps(media, ensure_ascii=False), **common)
    elif photos:
        telegram(token, "sendPhoto", photo=photos[0], caption=caption, parse_mode="HTML", **common)
    else:
        telegram(token, "sendMessage", text=caption, parse_mode="HTML",
                 link_preview_options=json.dumps({"is_disabled": True}), **common)
    print("Пост отправлен.")


if __name__ == "__main__":
    main()
