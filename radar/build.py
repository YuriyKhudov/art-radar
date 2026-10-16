"""Сборка сайта Art Radar для GitHub Pages.

  python -m radar.build --out _site      собрать сайт: свежие видео и статьи + готовая статистика продаж
  python -m radar.build --export-sales   обновить site_data/sales_stats.json из локальной базы продаж

Сайт статический: каждый час GitHub заново собирает его на своих серверах (см. .github/workflows/site.yml).
Что посетитель уже посмотрел, запоминает его браузер.
"""

import argparse
import datetime as dt
import json
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
    stats = json.loads(SALES_STATS.read_text(encoding="utf-8")) if SALES_STATS.exists() else {"sales": [], "datasets": []}
    data.update(stats)
    data["sales_own"] = []
    data["static"] = True
    data["built_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    (out / "index.html").write_text(app.TEMPLATE.replace("/*DATA*/{}", payload), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
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
