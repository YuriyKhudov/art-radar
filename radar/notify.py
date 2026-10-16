"""Уведомления в Telegram о новых видео на выбранных YouTube-каналах.

Каналы проверяются через официальную RSS-ленту YouTube (без ключей и без
сбора данных со страниц). Уже показанные видео запоминаются в data/radar.db.

Запуск:
  python -m radar.notify            проверить каналы и прислать новое
  python -m radar.notify --setup    подключить Telegram-бота
  python -m radar.notify --test     прислать проверочное сообщение
  python -m radar.notify --dry-run  показать, что было бы отправлено, ничего не отправляя
"""

import argparse
import datetime as dt
import html
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_FILE = ROOT / "sources_seed.json"
SETTINGS_FILE = ROOT / "settings.json"
DB_FILE = ROOT / "data" / "radar.db"
LOG_FILE = ROOT / "data" / "notify.log"

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

DEFAULT_SETTINGS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # Какие каналы проверять: "all" — все из sources_seed.json, или список, например ["@americaart"].
    "notify_channels": "all",
    # Ночью не беспокоить: накопленное придёт одним сообщением после окончания тихих часов.
    "quiet_hours": {"from": "22:00", "to": "09:00"},
}


def log(msg):
    LOG_FILE.parent.mkdir(exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if sys.stdout:
        try:
            print(line)
        except (UnicodeEncodeError, OSError):
            pass


# ---------- настройки и каталог ----------

def load_settings():
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULT_SETTINGS)
    data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {**DEFAULT_SETTINGS, **data}


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def all_channels():
    """Все YouTube-каналы из каталога: и общий список, и каналы галерей."""
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    channels = []
    for gallery in seed.get("galleries", []):
        for c in gallery.get("youtube", []):
            channels.append({"name": c["name"], "handle": c["handle"], "channel_id": c["channel_id"],
                             "category": "galleries", "gallery": gallery["name"]})
    channels += [{"name": c["name"], "handle": c["handle"], "channel_id": c["channel_id"],
                  "category": c.get("category", "")} for c in seed.get("youtube", [])]
    return channels


def channels_to_watch(settings):
    if settings["notify_channels"] == "all":
        return all_channels()
    wanted = {h.lower() for h in settings["notify_channels"]}
    found = [c for c in all_channels() if c["handle"].lower() in wanted]
    missing = wanted - {c["handle"].lower() for c in found}
    for h in sorted(missing):
        log(f"Канал {h} не найден в sources_seed.json — пропускаю")
    return found


# ---------- база ----------

def db():
    DB_FILE.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_videos (
        video_id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, url TEXT,
        published_at TEXT, first_seen_at TEXT, notified_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS channels_initialized (
        channel_id TEXT PRIMARY KEY, initialized_at TEXT)""")
    # Число просмотров (из той же ленты YouTube) — для сортировки «Популярные». В старых базах столбца ещё нет.
    if "views" not in {row[1] for row in conn.execute("PRAGMA table_info(seen_videos)")}:
        conn.execute("ALTER TABLE seen_videos ADD COLUMN views INTEGER")
    return conn


# ---------- YouTube RSS ----------

def fetch_videos(channel_id):
    req = urllib.request.Request(RSS_URL.format(channel_id), headers={"User-Agent": "ArtRadar/0.1"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        root = ET.fromstring(resp.read())
    videos = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", namespaces=NS)
        link = entry.find("atom:link", NS)
        stats = entry.find("media:group/media:community/media:statistics", NS)
        videos.append({
            "video_id": video_id,
            "title": entry.findtext("atom:title", namespaces=NS) or "",
            "url": link.get("href") if link is not None else f"https://www.youtube.com/watch?v={video_id}",
            "published_at": entry.findtext("atom:published", namespaces=NS) or "",
            "views": int(stats.get("views", 0)) if stats is not None else None,
        })
    return videos


# ---------- Telegram ----------

def telegram(settings, method, **params):
    url = f"https://api.telegram.org/bot{settings['telegram_bot_token']}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "ошибка Telegram"))
    return result["result"]


def send(settings, text):
    telegram(settings, "sendMessage", chat_id=settings["telegram_chat_id"], text=text,
             parse_mode="HTML", disable_web_page_preview="false")


def video_message(channel, video):
    return (f"🎬 <b>Новое видео на канале «{html.escape(channel['name'])}»</b>\n\n"
            f"«{html.escape(video['title'])}»\n\n"
            f"👉 <a href=\"{html.escape(video['url'])}\">Посмотреть на YouTube</a>")


def digest_message(items):
    lines = [f"🌅 <b>Пока вы отдыхали, вышло новых видео: {len(items)}</b>\n"]
    for channel_name, title, url in items:
        lines.append(f"• «{html.escape(channel_name)}»: <a href=\"{html.escape(url)}\">{html.escape(title)}</a>")
    return "\n".join(lines)


def in_quiet_hours(settings, now=None):
    now = (now or dt.datetime.now()).time()
    start = dt.time.fromisoformat(settings["quiet_hours"]["from"])
    end = dt.time.fromisoformat(settings["quiet_hours"]["to"])
    if start <= end:
        return start <= now < end
    return now >= start or now < end


# ---------- основная проверка ----------

def check(settings, dry_run=False):
    collect(settings)
    if dry_run or (settings["telegram_bot_token"] and settings["telegram_chat_id"]):
        send_pending(settings, dry_run)


def collect(settings):
    """Проверить каналы и запомнить новые видео в базе."""
    conn = db()
    channels = channels_to_watch(settings)

    def fetch(channel):
        try:
            return channel, fetch_videos(channel["channel_id"])
        except (urllib.error.URLError, ET.ParseError, TimeoutError) as e:
            log(f"Не удалось проверить «{channel['name']}»: {e}")
            return channel, None

    # Каналов много — проверяем по 8 одновременно, чтобы программа открывалась быстро.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, channels))

    week_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
    for channel, videos in results:
        if videos is None:
            continue
        first_time = conn.execute("SELECT 1 FROM channels_initialized WHERE channel_id=?",
                                  (channel["channel_id"],)).fetchone() is None
        now = dt.datetime.now().isoformat(timespec="seconds")
        new = 0
        for v in videos:
            # При первой проверке канала видео старше недели считаются уже известными;
            # видео за последнюю неделю показываются как новые.
            old = first_time and v["published_at"] < week_ago
            cur = conn.execute(
                "INSERT OR IGNORE INTO seen_videos (video_id, channel_id, title, url, published_at, first_seen_at, "
                "notified_at, views) VALUES (?,?,?,?,?,?,?,?)",
                (v["video_id"], channel["channel_id"], v["title"], v["url"], v["published_at"], now,
                 "skipped-initial" if old else None, v["views"]))
            new += cur.rowcount
            if not cur.rowcount and v["views"] is not None:  # уже известное видео — обновить просмотры
                conn.execute("UPDATE seen_videos SET views=? WHERE video_id=?", (v["views"], v["video_id"]))
        if first_time:
            conn.execute("INSERT INTO channels_initialized VALUES (?,?)", (channel["channel_id"], now))
            log(f"«{channel['name']}»: канал подключён, запомнено видео: {new}. Уведомления — о новых роликах.")
        elif new:
            log(f"«{channel['name']}»: новых видео: {new}")
        conn.commit()


def send_pending(settings, dry_run=False):
    """Прислать в Telegram видео, о которых ещё не сообщали."""
    conn = db()
    names = {c["channel_id"]: c["name"] for c in all_channels()}
    pending = conn.execute(
        "SELECT video_id, channel_id, title, url FROM seen_videos WHERE notified_at IS NULL "
        "ORDER BY published_at").fetchall()
    if not pending:
        return
    if dry_run:
        for _, channel_id, title, url in pending:
            log(f"[проба] отправил бы: «{names.get(channel_id, channel_id)}» — {title} — {url}")
        return
    if in_quiet_hours(settings):
        log(f"Тихие часы: {len(pending)} видео ждут утра")
        return

    try:
        if len(pending) > 3:
            send(settings, digest_message([(names.get(c, c), t, u) for _, c, t, u in pending]))
        else:
            for _, channel_id, title, url in pending:
                send(settings, video_message({"name": names.get(channel_id, channel_id)},
                                             {"title": title, "url": url}))
                time.sleep(1)
    except (urllib.error.URLError, RuntimeError) as e:
        log(f"Не удалось отправить в Telegram: {e}. Попробую при следующей проверке.")
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany("UPDATE seen_videos SET notified_at=? WHERE video_id=?",
                     [(now, vid) for vid, *_ in pending])
    conn.commit()
    log(f"Отправлено уведомлений о видео: {len(pending)}")


# ---------- подключение бота ----------

def setup():
    settings = load_settings()
    print("\nПодключение Telegram-бота\n")
    print("1. В Telegram найдите @BotFather, отправьте ему /newbot и придумайте имя боту.")
    print("2. BotFather пришлёт длинный ключ (токен) вида 123456:ABC-...\n")
    token = input("Вставьте сюда этот ключ и нажмите Enter: ").strip()
    if not token:
        print("Ключ не введён — настройка отменена.")
        return
    settings["telegram_bot_token"] = token
    try:
        me = telegram(settings, "getMe")
    except Exception as e:
        print(f"\nКлюч не подошёл ({e}). Проверьте, что скопировали его целиком, и запустите снова.")
        return
    print(f"\nОтлично! Бот @{me['username']} найден.")
    print(f"3. Откройте в Telegram бота @{me['username']}, нажмите «Старт» и отправьте ему любое сообщение.")
    input("   Когда отправите, нажмите здесь Enter... ")
    updates = telegram(settings, "getUpdates")
    chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
    if not chats:
        print("\nСообщение не найдено. Напишите боту ещё раз и запустите настройку снова.")
        return
    settings["telegram_chat_id"] = str(chats[-1])
    save_settings(settings)
    send(settings, "✅ Готово! Сюда будут приходить уведомления о новых видео.")
    print("\nГотово! Проверьте Telegram — там должно быть сообщение «Готово!».")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Уведомления о новых видео в Telegram")
    parser.add_argument("--setup", action="store_true", help="подключить Telegram-бота")
    parser.add_argument("--test", action="store_true", help="прислать проверочное сообщение")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять, только показать")
    args = parser.parse_args()

    if args.setup:
        setup()
    elif args.test:
        settings = load_settings()
        send(settings, "👋 Проверка: уведомления Art Radar работают.")
        print("Отправлено.")
    else:
        check(load_settings(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
