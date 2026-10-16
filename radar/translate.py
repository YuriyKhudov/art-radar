"""Перевод заголовков видео и статей на русский через бесплатный сервис MyMemory.

Переводы запоминаются в .cache/translations.json, поэтому каждый заголовок переводится один раз.
У бесплатного сервиса дневной лимит, так что за одну сборку переводится не больше BUDGET символов —
сначала самое заметное (популярные видео, свежие статьи), остальное — в следующие часы.
"""

import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from radar.notify import ROOT, log

CACHE = ROOT / ".cache" / "translations.json"
BUDGET = 4500
API = "https://api.mymemory.translated.net/get?langpair=en|ru&q="
CYRILLIC = re.compile(r"[а-яё]", re.I)
BAD = re.compile(r"MYMEMORY WARNING|PLEASE SELECT|QUERY LENGTH LIMIT|INVALID", re.I)


def load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(cache):
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def ask(text):
    req = urllib.request.Request(API + urllib.parse.quote(text[:480]), headers={"User-Agent": "ArtRadar/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    out = (d.get("responseData") or {}).get("translatedText") or ""
    if d.get("quotaFinished") or d.get("responseStatus") != 200 or BAD.search(out) or not CYRILLIC.search(out):
        return None
    return out.strip()


def translate_titles(items):
    """Добавить title_ru к элементам (в порядке важности). Русские заголовки не трогаем."""
    cache = load()
    todo, spent = [], 0
    for it in items:
        t = it["title"]
        if CYRILLIC.search(t) or t in cache or t in todo:
            continue
        if spent + len(t) > BUDGET:
            break
        todo.append(t)
        spent += len(t)
    if todo:
        def one(t):
            try:
                return t, ask(t)
            except Exception:
                return t, None
        with ThreadPoolExecutor(max_workers=4) as pool:
            done = {t: r for t, r in pool.map(one, todo) if r}
        cache.update(done)
        save(cache)
        log(f"Переведено заголовков: {len(done)} из {len(todo)}")
    for it in items:
        if it["title"] in cache:
            it["title_ru"] = cache[it["title"]]
    return items
