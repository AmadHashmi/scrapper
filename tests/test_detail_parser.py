from __future__ import annotations

import unittest
from pathlib import Path

from olx_scraper.detail_parser import parse_detail_page


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "detail_page.html"
DETAIL_URL = "https://www.olx.com.pk/item/5-marla-luxury-house-available-for-sale-in-citi-housing-sialkot-iid-1113625415"


class DetailParserFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE_PATH.exists():
            raise FileNotFoundError(
                f"Missing fixture: {FIXTURE_PATH}. Save one OLX detail page HTML to this file before running tests."
            )
        cls.html = FIXTURE_PATH.read_text(encoding="utf-8")

    def test_parses_description_and_specs(self) -> None:
        detail = parse_detail_page(self.html, DETAIL_URL)

        self.assertTrue(detail["description"].strip())
        self.assertTrue(detail["area_text"].strip())
        self.assertIsNotNone(detail["bedrooms"])
        self.assertIsNotNone(detail["bathrooms"])

    def test_parses_address_and_gallery(self) -> None:
        detail = parse_detail_page(self.html, DETAIL_URL)

        self.assertTrue(detail["address"].strip())
        self.assertGreater(detail["image_count"], 0)
        self.assertEqual(detail["image_count"], len(detail["gallery_image_urls"]))
        self.assertEqual(len(set(detail["gallery_image_urls"])), len(detail["gallery_image_urls"]))

    def test_phone_fields_exist_with_valid_confidence(self) -> None:
        detail = parse_detail_page(self.html, DETAIL_URL)

        self.assertIn("phone", detail)
        self.assertIn("masked_phone", detail)
        self.assertIn("phone_confidence", detail)
        self.assertIn(detail["phone_confidence"], {"high", "medium", "masked", "none"})


if __name__ == "__main__":
    unittest.main()
