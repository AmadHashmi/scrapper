from __future__ import annotations

import logging
import re
from typing import Iterable, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import ListingRecord


LOGGER = logging.getLogger(__name__)

LISTING_CARD_SELECTOR = 'li[aria-label="Listing"]'
DETAIL_LINK_SELECTOR = 'a[href*="/item/"][title]'
TITLE_SELECTOR = 'div[aria-label="Title"] h2'
PRICE_SELECTOR = 'div[aria-label="Price"] span'
LOCATION_SELECTOR = 'span[aria-label="Location"]'
SUBTITLE_SELECTOR = 'div[aria-label="Subtitle"]'
AD_ID_PATTERN = re.compile(r"iid-(\d+)")
BEDROOMS_PATTERN = re.compile(r"(?P<count>\d+)\s*(?:bed|beds|bedroom|bedrooms)\b", re.IGNORECASE)
BATHROOMS_PATTERN = re.compile(r"(?P<count>\d+)\s*(?:bath|baths|bathroom|bathrooms)\b", re.IGNORECASE)
AREA_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>marla|kanal|sq\s*ft|sqft|square\s*feet|m2|sqm|sq\s*m|square\s*meter|square\s*meters|sq\s*yards|sqyards|square\s*yards?)",
    re.IGNORECASE,
)
WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    """Normalize text by removing non-breaking spaces and collapsing whitespace."""

    if value is None:
        return ""
    cleaned_value = str(value).replace("\xa0", " ").replace("\u00a0", " ").replace("\u200b", "")
    return WHITESPACE_PATTERN.sub(" ", cleaned_value).strip()


def _first_match(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text)
    if match is None:
        return ""
    return normalize_text(match.group(0))


def _parse_numeric_attribute(text: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(text)
    if match is None:
        return None
    try:
        return int(match.group("count"))
    except (ValueError, IndexError, AttributeError):
        return None


def _parse_area_attribute(text: str) -> str:
    return _first_match(text, AREA_PATTERN)


def _card_nodes(soup: BeautifulSoup) -> list[Tag]:
    return list(soup.select(LISTING_CARD_SELECTOR))


def _first_non_empty_text(node: Tag, selectors: Iterable[str], default: str = "") -> str:
    for selector in selectors:
        candidate = node.select_one(selector)
        if candidate is None:
            continue
        text = normalize_text(candidate.get_text(" ", strip=True))
        if text:
            return text
    return default


def _extract_detail_link(card: Tag, base_url: str) -> str:
    anchor = card.select_one(DETAIL_LINK_SELECTOR)
    if anchor is None:
        return ""
    href = normalize_text(anchor.get("href"))
    if not href:
        return ""
    return urljoin(base_url, href)


def _extract_ad_id(listing_url: str) -> str:
    match = AD_ID_PATTERN.search(listing_url)
    if match is None:
        return ""
    return match.group(1)


def _extract_srcset_url(srcset_value: str) -> str:
    cleaned_value = normalize_text(srcset_value)
    if not cleaned_value:
        return ""
    first_entry = cleaned_value.split(",")[0].strip()
    if not first_entry:
        return ""
    return first_entry.split(" ")[0].strip()


def _extract_image_from_noscript(card: Tag) -> str:
    for noscript_node in card.find_all("noscript"):
        nested_html = noscript_node.decode_contents().strip()
        if not nested_html:
            continue
        nested_soup = BeautifulSoup(nested_html, "html.parser")
        nested_image = nested_soup.select_one("img")
        if nested_image is not None:
            for attribute_name in ("data-src", "src"):
                candidate = normalize_text(nested_image.get(attribute_name))
                if candidate:
                    return candidate

        nested_source = nested_soup.select_one("source[data-srcset], source[srcset]")
        if nested_source is not None:
            for attribute_name in ("data-srcset", "srcset"):
                candidate = _extract_srcset_url(nested_source.get(attribute_name, ""))
                if candidate:
                    return candidate
    return ""


def _extract_image_url(card: Tag) -> str:
    image_node = card.select_one("img[data-src], img[src]")
    if image_node is not None:
        for attribute_name in ("data-src", "src"):
            candidate = normalize_text(image_node.get(attribute_name))
            if candidate:
                return candidate

    source_node = card.select_one("source[data-srcset], source[srcset]")
    if source_node is not None:
        for attribute_name in ("data-srcset", "srcset"):
            candidate = _extract_srcset_url(source_node.get(attribute_name, ""))
            if candidate:
                return candidate

    return _extract_image_from_noscript(card)


def _extract_subtitle_text(card: Tag) -> str:
    subtitle_node = card.select_one(SUBTITLE_SELECTOR)
    if subtitle_node is None:
        return ""
    return normalize_text(subtitle_node.get_text(" ", strip=True))


def _build_record(card: Tag, base_url: str, fallback_index: int) -> ListingRecord | None:
    listing_url = _extract_detail_link(card, base_url)
    if not listing_url:
        return None

    ad_id = _extract_ad_id(listing_url)
    unique_id = ad_id or listing_url or f"listing-{fallback_index}"

    title = _first_non_empty_text(card, (TITLE_SELECTOR,), default="")
    if not title:
        anchor = card.select_one(DETAIL_LINK_SELECTOR)
        title = normalize_text(anchor.get("title")) if anchor is not None else ""

    price_text = _first_non_empty_text(card, (PRICE_SELECTOR,), default="")
    location_text = _first_non_empty_text(card, (LOCATION_SELECTOR,), default="")
    subtitle_text = _extract_subtitle_text(card)

    area_text = _parse_area_attribute(subtitle_text)
    bedrooms = _parse_numeric_attribute(subtitle_text, BEDROOMS_PATTERN)
    bathrooms = _parse_numeric_attribute(subtitle_text, BATHROOMS_PATTERN)

    return ListingRecord(
        listing_id=unique_id,
        title=title,
        price_text=price_text,
        location_text=location_text,
        posted_time_text="",
        details_text=subtitle_text,
        area_text=area_text,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        listing_url=listing_url,
        image_url=_extract_image_url(card),
    )


def parse_listings(html: str, base_url: str) -> List[ListingRecord]:
    """Parse OLX listing cards from search-result HTML.

    The parser uses the stable aria-label hooks visible in the static HTML and
    deduplicates records within a page using the extracted ad ID when available.
    """

    soup = BeautifulSoup(html, "lxml")
    cards = _card_nodes(soup)
    records: list[ListingRecord] = []
    seen_keys: set[str] = set()

    for index, card in enumerate(cards, start=1):
        record = _build_record(card, base_url=base_url, fallback_index=index)
        if record is None:
            continue

        dedupe_key = _extract_ad_id(record.listing_url) or record.listing_url
        if dedupe_key in seen_keys:
            continue

        seen_keys.add(dedupe_key)
        records.append(record)

    LOGGER.info("Parsed %s listings from %s", len(records), base_url)
    return records


def parse_listing_cards(html: str, page_url: str) -> List[ListingRecord]:
    """Backward-compatible wrapper used by the current scraper scaffold."""

    return parse_listings(html=html, base_url=page_url)
