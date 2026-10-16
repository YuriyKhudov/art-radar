"""Проверка разбора текста лота и нормализации. Тексты ниже — выдуманные образцы в формате аукционов."""

import tempfile
import unittest
from pathlib import Path

import radar.notify as notify
from radar import sales

CHRISTIES_LIKE = """Jane Example (b. 1988)
Blue Interior
signed and dated 'J. Example 2019' (on the reverse)
oil on canvas
40 x 30 in. (101.6 x 76.2 cm.)
Painted in 2019.
Price realised USD 12,600
Estimate USD 8,000 – USD 12,000
Post-War to Present, New York, 12 May 2025"""

SOTHEBYS_LIKE = """John Sample
Night Figures
Acrylic and oil stick on linen
152.4 by 121.9 cm. 60 by 48 in.
Estimate 20,000 - 30,000 USD
Sold for 25,400 USD
Contemporary Curated, March 3, 2026"""

UNSOLD_LIKE = """Ann Test
Untitled
oil on panel
12 x 16 in.
Estimate $3,000 - $5,000
Lot passed"""


class ParseTest(unittest.TestCase):
    def test_christies_like(self):
        r = sales.parse_lot(CHRISTIES_LIKE)
        self.assertEqual(r["artist_name"], "Jane Example")
        self.assertEqual(r["artwork_title"], "Blue Interior")
        self.assertEqual((r["height_cm"], r["width_cm"]), (101.6, 76.2))
        self.assertEqual(r["medium_raw"].lower(), "oil on canvas")
        self.assertEqual((r["sale_price"], r["currency"], r["price_includes_premium"]), (12600, "USD", True))
        self.assertEqual(r["sale_date"], "2025-05-12")
        self.assertEqual(r["artwork_year"], 2019)

    def test_sothebys_like(self):
        r = sales.parse_lot(SOTHEBYS_LIKE)
        self.assertEqual((r["height_cm"], r["width_cm"]), (152.4, 121.9))
        self.assertEqual(r["sale_price"], 25400)  # не эстимейт
        self.assertEqual(r["sale_date"], "2026-03-03")
        self.assertEqual(sales.normalize_technique(r["medium_raw"])["technique_group"], "mixed media on canvas")

    def test_unsold_is_flagged(self):
        r = sales.parse_lot(UNSOLD_LIKE)
        self.assertTrue(r.get("unsold"))
        self.assertNotIn("sale_price", r)  # эстимейт не принимается за цену

    def test_derive(self):
        r = sales.derive({"height_cm": 76.2, "width_cm": 61.0})
        self.assertEqual((r["orientation"], r["ratio_class"], r["size_bucket"], r["std_size_in"]),
                         ("vertical", "5:4", "M", "30x24"))
        self.assertEqual(sales.derive({"height_cm": 50, "width_cm": 50})["orientation"], "square")

    def test_technique_groups(self):
        g = lambda t: sales.normalize_technique(t)["technique_group"]
        self.assertEqual(g("oil on linen"), "oil on linen")
        self.assertEqual(g("Acrylic on canvas"), "acrylic on canvas")
        self.assertEqual(g("gouache on paper"), "works on paper")
        self.assertEqual(g("egg tempera on panel"), "tempera")

    def test_categories(self):
        c = lambda t: sales.category(sales.normalize_technique(t))
        self.assertEqual(c("oil on canvas"), "painting")
        self.assertEqual(c("acrylic on canvas"), "painting")
        self.assertEqual(c("gouache on paper"), "graphics")
        self.assertEqual(c("charcoal on paper"), "graphics")
        self.assertEqual(c("acrylic and oil stick on linen"), "mixed")


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = notify.DB_FILE
        notify.DB_FILE = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        notify.DB_FILE = self.old
        self.tmp.cleanup()

    def rec(self, **kw):
        return {"artist_name": "Jane Example", "artwork_title": "Blue", "height_cm": 101.6, "width_cm": 76.2,
                "medium_raw": "oil on canvas", "source_url": "https://example.com/lot/1", "sold_confirmed": True, **kw}

    def test_rules(self):
        with self.assertRaises(sales.SaleError):
            sales.save(self.rec(source_url=""))       # без доказательства нельзя
        with self.assertRaises(sales.SaleError):
            sales.save(self.rec(sold_confirmed=False))  # без подтверждения нельзя
        with self.assertRaises(sales.SaleError):
            sales.save(self.rec(unsold=True))          # непроданный лот нельзя
        sales.save(self.rec())
        sales.save(self.rec())                         # повтор не создаёт дубль
        recs = sales.all_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["technique_group"], "oil on canvas")
        self.assertEqual(recs[0]["std_size_in"], "40x30")

    def test_csv(self):
        text = ("artist_name;artwork_title;height_in;width_in;medium_raw;sale_price;currency;source_url\n"
                "A;T1;20;16;oil on canvas;5000;USD;https://example.com/1\n"
                "B;T2;;;oil on canvas;;;https://example.com/2\n")
        res = sales.import_csv(text)
        self.assertEqual(res["added"], 1)
        self.assertEqual(len(res["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
