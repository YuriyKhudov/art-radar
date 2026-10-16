"""Сборка сайта Art Radar для GitHub Pages.

  python -m radar.build --out _site      собрать сайт: свежие видео и статьи + готовая статистика продаж
  python -m radar.build --export-sales   обновить site_data/sales_stats.json из локальной базы продаж

Сайт статический: каждый час GitHub заново собирает его на своих серверах (см. .github/workflows/site.yml).
Что посетитель уже посмотрел, запоминает его браузер.
"""

import argparse
import datetime as dt
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import radar.notify as notify

ROOT = notify.ROOT
SALES_STATS = ROOT / "site_data" / "sales_stats.json"


def export_sales():
    from radar import sales
    SALES_STATS.parent.mkdir(exist_ok=True)
    data = {"sales": sales.stats_records(), "datasets": [sales.LEE2024]}
    SALES_STATS.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{SALES_STATS}: {len(data['sales'])} продаж")


ASSETS = ROOT / "site_assets"

MANIFEST = {
    "name": "Art Radar", "short_name": "Art Radar", "lang": "ru",
    "description": "Американский арт-рынок для художника: видео, новости, ярмарки и форматы картин",
    "start_url": "./", "scope": "./", "display": "standalone",
    "background_color": "#f7f3ec", "theme_color": "#a34e36",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Службу браузера держим простой: всегда берём свежую страницу из сети, а сохранённую — только без интернета.
SERVICE_WORKER = """const CACHE = "art-radar-v1";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET" || new URL(r.url).origin !== location.origin) return;
  e.respondWith(fetch(r).then(resp => {
    const copy = resp.clone();
    caches.open(CACHE).then(c => c.put(r, copy));
    return resp;
  }).catch(() => caches.match(r)));
});
"""


def write_app_files(out):
    """Иконки, описание приложения и служба браузера — чтобы сайт ставился на телефон как приложение."""
    for f in ASSETS.glob("*.png"):
        shutil.copyfile(f, out / f.name)
    (out / "manifest.webmanifest").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fair_ics(f):
    """Событие ярмарки для календаря (iPhone, Outlook, Google): весь день, напоминание за неделю."""
    start = dt.date.fromisoformat(f["start"])
    end = dt.date.fromisoformat(f.get("end") or f["start"]) + dt.timedelta(days=1)
    esc = lambda t: t.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Art Radar//RU", "CALSCALE:GREGORIAN", "BEGIN:VEVENT",
        f"UID:{slug(f['name'])}-{f['start']}@art-radar",
        f"DTSTAMP:{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}",
        f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}",
        f"SUMMARY:{esc(f['name'])}", f"LOCATION:{esc(f['venue'] + ', ' + f['city'])}", f"URL:{f['url']}",
        f"DESCRIPTION:{esc('Ярмарка современного искусства. Сайт: ' + f['url'] + ' — из Art Radar')}",
        "BEGIN:VALARM", "TRIGGER:-P7D", "ACTION:DISPLAY", f"DESCRIPTION:{esc('Через неделю: ' + f['name'])}", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def write_calendars(out, fairs):
    cal = out / "cal"
    cal.mkdir(exist_ok=True)
    for f in fairs:
        if f.get("start"):
            (cal / f"{slug(f['name'])}.ics").write_text(fair_ics(f), encoding="utf-8", newline="")


def build(out):
    # Каждая сборка начинается с чистой временной базы: берём из RSS последние видео и статьи.
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    notify.DB_FILE = Path(tmp.name) / "site.db"
    notify.LOG_FILE = Path(tmp.name) / "build.log"
    from radar import app  # импорт после подмены базы
    settings = {**notify.DEFAULT_SETTINGS, "notify_channels": "all"}
    app.refresh(settings)
    data = app.page_data(settings)
    for item in data["videos"] + data["articles"]:
        item["initial"] = False  # «новое» для каждого посетителя решает его браузер
    # Перевод заголовков: сначала самые популярные видео, потом свежие статьи и возможности.
    from radar.translate import translate_titles
    translate_titles(sorted(data["videos"], key=lambda v: -(v.get("views") or 0)) + data["articles"])
    stats = json.loads(SALES_STATS.read_text(encoding="utf-8")) if SALES_STATS.exists() else {"sales": [], "datasets": []}
    data.update(stats)
    data["sales_own"] = []
    data["static"] = True
    # Код счётчика посетителей GoatCounter (из sources_seed.json → "analytics"); пусто — счётчик выключен.
    data["goatcounter"] = json.loads(notify.SEED_FILE.read_text(encoding="utf-8")).get("analytics", {}).get("goatcounter", "")
    data["built_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    (out / "index.html").write_text(app.TEMPLATE.replace("/*DATA*/{}", payload), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    write_app_files(out)
    write_calendars(out, data["fairs"])
    print(f"Собрано: {len(data['videos'])} видео, {len(data['articles'])} статей, {len(data['sales'])} продаж → {out}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="_site")
    p.add_argument("--export-sales", action="store_true")
    args = p.parse_args()
    if args.export_sales:
        export_sales()
    else:
        build(args.out)


if __name__ == "__main__":
    main()
