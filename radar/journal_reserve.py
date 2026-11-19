"""Retain screened but unpublished stories across daily harvests."""

import datetime as dt
import json
import re
from collections import Counter


EVENT = re.compile(r"выставк|биеннале|ярмарк|фестивал|экспозиц|\bexhibition\b", re.I)
RETENTION = {"news": 7, "event": 60, "practice": 120, "discovery": 90, "archive": 365}


def expiry(item, today, news_days):
    direction = item["direction"]
    published = item.get("published")
    date = dt.date.fromisoformat(published[:10]) if published else None
    if direction == "news":
        if not date:
            return None
        event = bool(EVENT.search(item["title"]))
        end = item.get("event_end") or item.get("deadline")
        if event and end:
            return "event", min(dt.date.fromisoformat(end[:10]), date + dt.timedelta(days=RETENTION["event"]))
        return ("event" if event else "news"), date + dt.timedelta(days=news_days)
    if direction == "practice":
        bucket = "practice"
    elif direction in {"archive", "people"}:
        bucket = "archive"
    else:
        bucket = "discovery"
    return bucket, (today if bucket == "archive" else date or today) + dt.timedelta(days=RETENTION[bucket])


class Reserve:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS editorial_reserve (
            url TEXT PRIMARY KEY, item TEXT NOT NULL, bucket TEXT NOT NULL,
            expires TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            verified_on TEXT
        )""")

    def replenish(self, candidates, now, news_days):
        today = now.date()
        rows = []
        for card in candidates:
            item = card.item
            window = expiry(item, today, news_days)
            if window and window[1] >= today:
                bucket, until = window
                rows.append((card.key, json.dumps(item, ensure_ascii=False), bucket,
                             until.isoformat(), today.isoformat(), today.isoformat()))
        self.connection.executemany("""INSERT INTO editorial_reserve
            (url, item, bucket, expires, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET item=excluded.item, bucket=excluded.bucket, last_seen=excluded.last_seen,
              expires=CASE WHEN json_extract(excluded.item, '$.published') IS NULL
                                OR excluded.bucket = 'archive' THEN editorial_reserve.expires
                           ELSE excluded.expires END,
              verified_on=CASE WHEN editorial_reserve.item=excluded.item THEN editorial_reserve.verified_on END
        """, rows)
        self.connection.execute("DELETE FROM editorial_reserve WHERE expires < ?", (today.isoformat(),))

    def pool(self, now):
        return [{**json.loads(item), "reserve_bucket": bucket, "reserve_expires": expires,
                 "reserve_first_seen": first, "reserve_verified": bool(verified)}
                for item, bucket, expires, first, verified in self.connection.execute(
                    "SELECT item, bucket, expires, first_seen, verified_on FROM editorial_reserve WHERE expires >= ?",
                    (now.date().isoformat(),))]

    def confirm(self, urls, now):
        self.connection.executemany("UPDATE editorial_reserve SET verified_on=? WHERE url=?",
                                    [(now.date().isoformat(), url) for url in urls])

    def confirmed(self, now):
        return {row[0] for row in self.connection.execute(
            "SELECT url FROM editorial_reserve WHERE expires >= ? AND verified_on IS NOT NULL", (now.date().isoformat(),))}

    def remove(self, urls):
        self.connection.executemany("DELETE FROM editorial_reserve WHERE url=?", [(url,) for url in urls])

    def report(self, now):
        rows = self.pool(now)
        return {"pending": dict(Counter(r["reserve_bucket"] for r in rows if not r["reserve_verified"])),
                "verified": dict(Counter(r["reserve_bucket"] for r in rows if r["reserve_verified"]))}
