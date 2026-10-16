"""Подтверждённые продажи картин: разбор текста лота, нормализация размеров и техник, хранение.

Правило: в базу попадает только продажа с подтверждением и ссылкой-доказательством.
Непроданные, снятые с торгов лоты и «исчезнувшие» работы не сохраняются.
"""

import csv
import datetime as dt
import hashlib
import io
import re
from contextlib import closing

from radar.notify import db

CM_PER_IN = 2.54

# Стандартные американские подрамники, дюймы (короткая × длинная сторона).
STD_IN = [(8, 10), (9, 12), (11, 14), (12, 12), (12, 16), (16, 20), (18, 24), (20, 20), (20, 24), (24, 24),
          (24, 30), (24, 36), (30, 30), (30, 40), (36, 36), (36, 48), (40, 60), (48, 48), (48, 60), (60, 72)]
RATIOS = [(1.0, "1:1"), (1.25, "5:4"), (4 / 3, "4:3"), (1.4, "7:5"), (1.5, "3:2"), (1.6, "8:5"), (2.0, "2:1"), (3.0, "3:1")]

MEDIA = [  # (что ищем в тексте, нормализованное название)
    ("oil stick", "oil stick"), ("oilstick", "oil stick"), ("oil", "oil"), ("acrylic", "acrylic"),
    ("gouache", "gouache"), ("watercolou?r", "watercolor"), ("pastel", "pastel"), ("tempera", "tempera"),
    ("enamel", "enamel"), ("spray paint", "spray paint"), ("charcoal", "charcoal"), ("ink", "ink"),
    ("graphite", "graphite"), ("pencil", "pencil"), ("encaustic", "encaustic"), ("collage", "collage"),
]
SUPPORTS = [("linen", "linen"), ("canvas", "canvas"), ("panel", "panel"), ("board", "board"),
            ("paper", "paper"), ("wood", "panel"), ("masonite", "panel"), ("aluminium|aluminum", "metal")]
TECHNIQUE_RU = {
    "oil on canvas": "масло на холсте", "oil on linen": "масло на льне", "acrylic on canvas": "акрил на холсте",
    "mixed media on canvas": "смешанная техника на холсте", "oil on panel": "масло на панели",
    "works on paper": "работы на бумаге", "tempera": "темпера", "other": "другое",
}
UNSOLD = re.compile(r"\b(unsold|bought[\s-]in|passed|withdrawn|not sold|no sale)\b", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
CURRENCY = {"$": "USD", "US$": "USD", "USD": "USD", "£": "GBP", "GBP": "GBP", "€": "EUR", "EUR": "EUR",
            "HK$": "HKD", "HKD": "HKD", "CHF": "CHF"}


# ---------- разбор текста лота ----------

NUM = r"(\d+(?:[.,]\d+)?(?:\s+\d/\d)?)"
DIMS = re.compile(NUM + r"\s*(?:x|×|by)\s*" + NUM + r"(?:\s*(?:x|×|by)\s*" + NUM + r")?\s*(cm|in\b|in\.|inches|\")", re.I)


def _num(s):
    s = s.replace(",", ".")
    if " " in s:  # 30 1/2
        whole, frac = s.split()
        a, b = frac.split("/")
        return float(whole) + float(a) / float(b)
    return float(s)


def parse_dims(text):
    """Найти размеры. Возвращает высоту и ширину в см (аукционы пишут высоту первой)."""
    found = {}
    for m in DIMS.finditer(text):
        unit = "cm" if m.group(4).lower() == "cm" else "in"
        if unit not in found:
            found[unit] = (_num(m.group(1)), _num(m.group(2)), _num(m.group(3)) if m.group(3) else None, m.group(0))
    if not found:
        return None
    unit = "cm" if "cm" in found else "in"
    h, w, d, raw = found[unit]
    k = 1 if unit == "cm" else CM_PER_IN
    return {"height_cm": round(h * k, 1), "width_cm": round(w * k, 1),
            "depth_cm": round(d * k, 1) if d else None, "dims_raw": raw, "original_unit": unit}


def parse_medium(text):
    """Найти строку с техникой вида «oil on canvas», «acrylic and oil stick on linen»."""
    for line in text.splitlines():
        m = re.search(r"([a-z][a-z ,&\-]*?)\s+on\s+(?:\w+\s+)?(canvas|linen|panel|board|paper|wood|masonite|aluminium|aluminum)",
                      line, re.I)
        if m:
            return m.group(0).strip()
    return ""


def parse_price(text):
    """Цена продажи рядом со словами «realised / realized / sold for». Эстимейт не берём."""
    money = re.compile(r"(US\$|HK\$|USD|GBP|EUR|HKD|CHF|\$|£|€)\s?([\d][\d,\.\s]*\d|\d)|([\d][\d,\.]*\d)\s?(USD|GBP|EUR|HKD|CHF)")
    keyword = re.compile(r"(price realis|price realiz|sold for|hammer|realised|realized|продано за)", re.I)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if keyword.search(line) and not re.search(r"estimate", line, re.I):
            for chunk in (line, *(lines[i + 1:i + 2])):
                m = money.search(chunk)
                if m:
                    cur = CURRENCY[(m.group(1) or m.group(4)).upper()]
                    amount = float(re.sub(r"[,\s]", "", m.group(2) or m.group(3)).rstrip("."))
                    return {"sale_price": amount, "currency": cur,
                            # Крупные аукционы публикуют цену с комиссией покупателя.
                            "price_includes_premium": not re.search(r"hammer", line, re.I)}
    return {}


def parse_date(text):
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", text, re.I)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b", text, re.I)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    return ""


def parse_lot(text):
    """Черновик записи из скопированного текста лота. Человек проверяет и подтверждает."""
    rec = {"warnings": []}
    if UNSOLD.search(text):
        rec["warnings"].append("В тексте сказано, что лот не продан или снят с торгов — такую запись сохранить нельзя.")
        rec["unsold"] = True
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        rec["artist_name"] = re.sub(r"\s*\((?:b\.|born|\d{4}).*?\)\s*$", "", lines[0]).strip()
    if len(lines) > 1:
        rec["artwork_title"] = lines[1].strip("“”\"' ")
    m = re.search(r"\b(?:painted|executed|dated)\s+(?:in\s+)?(\d{4})\b", text, re.I)
    if m:
        rec["artwork_year"] = int(m.group(1))
    dims = parse_dims(text)
    if dims:
        rec.update(dims)
    else:
        rec["warnings"].append("Размер не найден — впишите его вручную.")
    rec["medium_raw"] = parse_medium(text)
    if not rec["medium_raw"]:
        rec["warnings"].append("Техника не найдена — впишите её вручную.")
    price = parse_price(text)
    if price:
        rec.update(price)
    else:
        rec["warnings"].append("Цена продажи не найдена. Можно сохранить без цены.")
    rec["sale_date"] = parse_date(text)
    for house in ("Christie's", "Sotheby's", "Phillips", "Bonhams", "Heritage", "Doyle", "Swann", "Hindman", "Rago", "Wright"):
        if house.lower().rstrip("'s") in text.lower():
            rec["source_name"] = house
            break
    url = re.search(r"https?://\S+", text)
    if url:
        rec["source_url"] = url.group(0).rstrip(").,")
    return rec


# ---------- нормализация ----------

def normalize_technique(medium_raw):
    t = (medium_raw or "").lower()
    media = [name for pat, name in MEDIA if re.search(r"\b" + pat + r"\b", t)]
    if "oil" in media and "oil stick" in media:
        media.remove("oil")
    support = next((name for pat, name in SUPPORTS if re.search(r"\b(" + pat + r")\b", t)), "")
    medium = media[0] if len(media) == 1 else ("mixed media" if media else "")
    if support == "paper" and medium in ("gouache", "watercolor", "pastel", "charcoal", "ink", "graphite", "pencil",
                                         "acrylic", "oil", "mixed media", "oil stick"):
        group = "works on paper"
    elif medium == "tempera":
        group = "tempera"
    elif medium == "oil" and support in ("canvas", "linen", "panel"):
        group = f"oil on {support}"
    elif medium == "acrylic" and support == "canvas":
        group = "acrylic on canvas"
    elif medium in ("mixed media", "oil stick", "spray paint", "enamel", "collage") and support in ("canvas", "linen"):
        group = "mixed media on canvas"
    else:
        group = "other"
    return {"medium_normalized": medium or "другое", "support_normalized": support or "другое", "technique_group": group}


def category(rec):
    """Крупная группа для статистики: живопись, графика (работы на бумаге) или смешанная техника."""
    group = rec.get("technique_group") or "other"
    if group == "works on paper" or rec.get("support_normalized") == "paper":
        return "graphics"
    if group == "mixed media on canvas" or rec.get("medium_normalized") in ("mixed media", "collage"):
        return "mixed"
    return "painting"


def derive(rec):
    """Посчитать производные поля: дюймы, ориентация, пропорция, площадь, группа размера, стандарт."""
    h, w = rec.get("height_cm"), rec.get("width_cm")
    if not (h and w):
        return rec
    rec["height_in"], rec["width_in"] = round(h / CM_PER_IN, 1), round(w / CM_PER_IN, 1)
    r = max(h, w) / min(h, w)
    rec["orientation"] = "square" if abs(r - 1) < 0.03 else ("vertical" if h > w else "horizontal")
    rec["aspect_ratio"] = round(r, 3)
    rec["ratio_class"] = next((name for x, name in RATIOS if abs(r - x) / x <= 0.02), "other")
    rec["area_sq_cm"] = round(h * w)
    long = max(h, w)
    rec["size_bucket"] = "S" if long <= 61 else "M" if long <= 122 else "L" if long <= 183 else "XL"
    lo, hi = sorted((round(h / CM_PER_IN * 2) / 2, round(w / CM_PER_IN * 2) / 2))
    std = next(((a, b) for a, b in STD_IN if abs(a - lo) <= 0.5 and abs(b - hi) <= 0.5), None)
    rec["std_size_in"] = (f"{std[1]}x{std[0]}" if h > w else f"{std[0]}x{std[1]}") if std else None
    return rec


# ---------- хранение ----------

FIELDS = ["artist_name", "artwork_title", "artwork_year", "sale_date", "source_name", "source_url", "market_type",
          "confirmation_level", "sale_price", "currency", "price_includes_premium", "city", "country",
          "dims_raw", "original_unit", "height_cm", "width_cm", "depth_cm", "height_in", "width_in",
          "orientation", "aspect_ratio", "ratio_class", "area_sq_cm", "size_bucket", "std_size_in",
          "medium_raw", "medium_normalized", "support_normalized", "technique_group", "entry_method", "note"]


def _db():
    conn = db()
    conn.execute(f"""CREATE TABLE IF NOT EXISTS sale_records (
        id TEXT PRIMARY KEY, {", ".join(f + " TEXT" for f in FIELDS)}, created_at TEXT)""")
    return conn


class SaleError(ValueError):
    pass


def prepare(rec, entry_method="manual"):
    """Проверить продажу и посчитать поля. Возвращает строку для таблицы sale_records."""
    rec = {k: v for k, v in rec.items() if v not in ("", None)}
    if rec.get("unsold"):
        raise SaleError("Лот не продан или снят с торгов — это не продажа.")
    if not rec.get("sold_confirmed"):
        raise SaleError("Отметьте, что продажа подтверждена источником.")
    if not re.match(r"https?://", rec.get("source_url", "")):
        raise SaleError("Нужна ссылка на источник, где видно, что работа продана.")
    for f in ("artist_name", "height_cm", "width_cm"):
        if not rec.get(f):
            raise SaleError({"artist_name": "Укажите художника.", "height_cm": "Укажите высоту.",
                             "width_cm": "Укажите ширину."}[f])
    rec["height_cm"], rec["width_cm"] = float(rec["height_cm"]), float(rec["width_cm"])
    if rec.get("sale_price"):
        rec["sale_price"] = float(str(rec["sale_price"]).replace(",", "").replace(" ", ""))
    rec.setdefault("confirmation_level", "A1")
    rec.setdefault("market_type", "auction" if rec["confirmation_level"] == "A1" else "gallery_primary")
    rec.update(normalize_technique(rec.get("medium_raw", "")))
    derive(rec)
    rec["entry_method"] = entry_method
    key = "|".join(str(rec.get(k, "")).strip().lower() for k in ("artist_name", "artwork_title", "sale_date", "source_url"))
    rid = hashlib.sha1(key.encode()).hexdigest()[:16]
    return [rid, *[None if rec.get(f) is None else str(rec.get(f)) for f in FIELDS],
            dt.datetime.now().isoformat(timespec="seconds")]


INSERT = f"INSERT OR REPLACE INTO sale_records VALUES (?, {', '.join('?' * len(FIELDS))}, ?)"


def save(rec, entry_method="manual"):
    """Проверить и сохранить продажу. Повтор той же продажи обновляет запись, а не создаёт новую."""
    row = prepare(rec, entry_method)
    with closing(_db()) as conn:
        conn.execute(INSERT, row)
        conn.commit()
    return row[0]


def delete(rid):
    with closing(_db()) as conn:
        conn.execute("DELETE FROM sale_records WHERE id=?", (rid,))
        conn.commit()


def all_records(where="1=1"):
    with closing(_db()) as conn:
        cur = conn.execute(f"SELECT * FROM sale_records WHERE {where} ORDER BY sale_date DESC, created_at DESC")
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    out = []
    for row in rows:
        r = dict(zip(cols, row))
        for f in ("height_cm", "width_cm", "height_in", "width_in", "aspect_ratio", "area_sq_cm", "sale_price"):
            r[f] = float(r[f]) if r.get(f) not in (None, "") else None
        r["price_includes_premium"] = r.get("price_includes_premium") == "True"
        r["technique_ru"] = TECHNIQUE_RU.get(r.get("technique_group") or "other", "другое")
        r["category"] = category(r)
        out.append(r)
    return out


def import_csv(text):
    """Импорт таблицы (CSV из Excel/Google Таблиц). Столбцы — как в FIELDS; height/width можно в см или дюймах."""
    sample = text[:2000]
    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
    added, errors = 0, []
    for n, row in enumerate(csv.DictReader(io.StringIO(text), dialect=dialect), start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if row.get("height_in") and not row.get("height_cm"):
            row["height_cm"] = float(row["height_in"].replace(",", ".")) * CM_PER_IN
            row["width_cm"] = float(row["width_in"].replace(",", ".")) * CM_PER_IN
        row["sold_confirmed"] = row.get("sold_confirmed", "да").lower() in ("1", "да", "yes", "true", "y")
        try:
            save(row, entry_method="csv")
            added += 1
        except (SaleError, ValueError) as e:
            errors.append(f"Строка {n}: {e}")
    return {"added": added, "errors": errors}


# ---------- открытые наборы данных ----------

LEE2024 = {
    "name": "Lee et al., 2024",
    "title": "Buying a Work of Art or an Artist? (Springer Nature figshare, CC BY 4.0)",
    "url": "https://springernature.figshare.com/articles/dataset/24746268",
    "about": "аукционные продажи работ 590 известных современных художников",
}
# Коды столбца genre в transactions.csv (сверены с файлом df_for_ml_improved_up_to_2012.csv по case_id).
LEE_GENRES = {"4": "painting", "Paintings": "painting", "2": "print", "Prints": "print", "Works on Paper": "paper"}
LEE_MEDIUM = {"painting": "painting", "print": "print on paper", "paper": "work on paper"}


def import_lee2024(path):
    """Живопись и графика из открытого набора Lee et al. (2024): только аукционы в долларах США."""
    def num(x):
        try:
            return float((x or "").replace(",", "").strip())
        except ValueError:
            return None
    rows, skipped = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            kind = LEE_GENRES.get(r["genre"])
            h, w, price = num(r["height"]), num(r["width"]), num(r["price_usd"])
            if not kind or r["currency"] != "USD" or r["country"] == "Doha" or not (h and w and price):
                skipped += 1
                continue
            city = "New York" if r["country"] in ("1", "New York") else r["country"].replace("&amp;", "&")
            try:
                rows.append(prepare({
                    "artist_name": r["artist_name"].strip(), "artwork_title": r["lot_title"].strip(),
                    "artwork_year": r["year_of_artwork"], "sale_date": parse_date(r["event date"]) or r["event_year"],
                    "source_name": r["auction_house"].replace("&amp;", "&").strip(), "city": city, "country": "USA",
                    # У набора нет ссылок на отдельные лоты — ссылаемся на сам набор и номер записи в нём.
                    "source_url": f"{LEE2024['url']}#case_id={r['case_id']}",
                    "sold_confirmed": True, "confirmation_level": "A1",
                    "sale_price": price, "currency": "USD", "price_includes_premium": r["Ham_Prem"] == "Premium",
                    "height_cm": h * CM_PER_IN, "width_cm": w * CM_PER_IN, "medium_raw": LEE_MEDIUM[kind],
                    "note": LEE2024["name"],
                }, entry_method="dataset:lee2024"))
            except (SaleError, ValueError):
                skipped += 1
    with closing(_db()) as conn:
        conn.execute("DELETE FROM sale_records WHERE entry_method='dataset:lee2024'")
        conn.executemany(INSERT, rows)
        conn.commit()
    return {"added": len(rows), "skipped": skipped}


def stats_records():
    """Короткие записи для статистики на странице (без лишних полей, чтобы страница открывалась быстро)."""
    with closing(_db()) as conn:
        rows = conn.execute("SELECT height_cm, width_cm, orientation, size_bucket, std_size_in, technique_group, "
                            "support_normalized, medium_normalized, sale_price, currency, price_includes_premium, "
                            "substr(sale_date, 1, 4), entry_method FROM sale_records").fetchall()
    out = []
    for h, w, o, b, std, tg, sup, med, price, cur, prem, year, em in rows:
        r = {"technique_group": tg, "support_normalized": sup, "medium_normalized": med}
        out.append({"h": float(h), "w": float(w), "o": o, "b": b, "s": std, "c": category(r),
                    "p": float(price) if price else None, "cur": cur, "prem": prem == "True",
                    "y": int(year) if (year or "").isdigit() else None, "d": em.startswith("dataset")})
    return out
