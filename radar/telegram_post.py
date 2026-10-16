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
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import radar.notify as notify

SITE = "https://yuriykhudov.github.io/art-radar/"


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
    return app.recent_videos(settings), json.loads(notify.SEED_FILE.read_text(encoding="utf-8"))["fairs"]


def build_post(videos, fairs, now=None, top=5):
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=24)).isoformat()
    fresh = [v for v in videos if v["published"] >= since and "/shorts/" not in v["url"]]
    fresh.sort(key=lambda v: -(v["views"] or 0))
    lines = ["🎨 <b>Art Radar — главное за сутки</b>", ""]
    if fresh:
        lines.append("🔥 <b>Популярные новые видео</b>")
        for i, v in enumerate(fresh[:top], 1):
            lines.append(f"{i}. <a href=\"{html.escape(v['url'])}\">{html.escape(v['title'])}</a>\n"
                         f"    <i>{html.escape(v['channel'])}{views_text(v['views'])}</i>")
    else:
        lines.append("За сутки новых видео не вышло — загляните в ленту на сайте.")
    today = now.date()
    upcoming = sorted((f for f in fairs if f.get("start") and dt.date.fromisoformat(f.get("end") or f["start"]) >= today),
                      key=lambda f: f["start"])
    if upcoming:
        f = upcoming[0]
        days = (dt.date.fromisoformat(f["start"]) - today).days
        when = "идёт сейчас" if days <= 0 else f"через {days} дн."
        lines += ["", f"🗓 <b>Ближайшая ярмарка:</b> {html.escape(f['name'])}, {html.escape(f['city'])} — {when}"]
    lines += ["", f"👉 <a href=\"{SITE}\">Все видео, новости и ярмарки — в Art Radar</a>"]
    return "\n".join(lines), len(fresh)


def telegram(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data),
                                timeout=30) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError(res.get("description"))
    return res["result"]


def last_topic(token):
    """Номер группы и ветки из самого свежего сообщения боту в ветке (для настройки)."""
    for u in reversed(telegram(token, "getUpdates")):
        m = u.get("message") or {}
        if m.get("chat", {}).get("type") in ("group", "supergroup"):
            return {"chat_id": m["chat"]["id"], "thread_id": m.get("message_thread_id") or "", "title": m["chat"].get("title", "")}
    return {}


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
    p.add_argument("--last-topic-json", action="store_true")
    args = p.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if args.last_topic_json:
        print(json.dumps(last_topic(token), ensure_ascii=False))
        return
    if args.find_ids:
        find_ids(token)
        return
    videos, fairs = collect_data()
    text, n = build_post(videos, fairs)
    if args.dry_run or not token:
        print(text)
        print(f"\n[проба: пост не отправлен; свежих видео за сутки: {n}]")
        return
    params = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
              "link_preview_options": json.dumps({"is_disabled": True})}
    if os.environ.get("TELEGRAM_THREAD_ID"):
        params["message_thread_id"] = os.environ["TELEGRAM_THREAD_ID"]
    telegram(token, "sendMessage", **params)
    print("Пост отправлен.")


if __name__ == "__main__":
    main()
