from __future__ import annotations

import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from olx_scraper.olx_parser import parse_listings


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "search_page.html"
BASE_URL = "https://www.olx.com.pk/sialkot_g4060686/houses_c1721"


class ParserFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE_PATH.exists():
            raise FileNotFoundError(
                f"Missing fixture: {FIXTURE_PATH}. Save the OLX search HTML to this file before running tests."
            )
        cls.html = FIXTURE_PATH.read_text(encoding="utf-8")
        cls.expected_count = len(BeautifulSoup(cls.html, "html.parser").select('li[aria-label="Listing"]'))

    def test_parse_expected_number_of_listings(self) -> None:
        listings = parse_listings(self.html, BASE_URL)
        self.assertEqual(len(listings), self.expected_count)
        self.assertGreater(len(listings), 0)

    def test_required_fields_are_populated(self) -> None:
        listings = parse_listings(self.html, BASE_URL)
        for listing in listings:
            self.assertTrue(listing.title.strip())
            self.assertTrue(listing.price_text.strip())
            self.assertTrue(listing.listing_url.strip())
            self.assertTrue(listing.listing_id.strip())
            self.assertIsInstance(listing.image_url, str)


if __name__ == "__main__":
    unittest.main()
