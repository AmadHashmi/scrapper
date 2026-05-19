from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import List
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .config import DEFAULT_USER_AGENT, ScraperSettings
from .detail_parser import parse_detail_page
from .exporter import export_to_excel
from .http_client import HttpClient
from .image_downloader import download_listing_images
from .models import ListingRecord, ScrapeStats
from .olx_parser import parse_listing_cards


LOGGER = logging.getLogger(__name__)


def build_page_url(base_url: str, page_number: int) -> str:
    """Build page URL by replacing {page} token or setting query param."""

    if "{page}" in base_url:
        return base_url.format(page=page_number)

    parsed = urlparse(base_url)
    query_map = parse_qs(parsed.query)
    query_map["page"] = [str(page_number)]
    encoded_query = urlencode(query_map, doseq=True)
    return urlunparse(parsed._replace(query=encoded_query))


class OLXPropertyScraper:
    """Main application service for scraping, media download and export."""

    def __init__(self, settings: ScraperSettings, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.settings = settings
        self.http_client = HttpClient(
            user_agent=user_agent,
            timeout_seconds=settings.request_timeout_seconds,
            min_delay_seconds=settings.delay_min_seconds,
            max_delay_seconds=settings.delay_max_seconds,
            max_retries=settings.max_retries,
            backoff_factor=settings.backoff_factor,
        )

    @staticmethod
    def _is_runtime_exceeded(deadline_monotonic: float | None) -> bool:
        return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic

    @staticmethod
    def _remaining_runtime_seconds(deadline_monotonic: float | None) -> float | None:
        if deadline_monotonic is None:
            return None
        return max(0.0, deadline_monotonic - time.monotonic())

    @staticmethod
    def _merge_detail_data(record: ListingRecord, detail_data: dict) -> None:
        description = detail_data.get("description", "")
        if description:
            record.description = description

        area_text = detail_data.get("area_text", "")
        if area_text:
            record.area_text = area_text

        bedrooms = detail_data.get("bedrooms")
        if bedrooms is not None:
            record.bedrooms = bedrooms

        bathrooms = detail_data.get("bathrooms")
        if bathrooms is not None:
            record.bathrooms = bathrooms

        address = detail_data.get("address", "")
        if address:
            record.address = address

        seller_type = detail_data.get("seller_type", "")
        if seller_type:
            record.seller_type = seller_type

        property_type = detail_data.get("property_type", "")
        if property_type:
            record.property_type = property_type

        posted_time_text = detail_data.get("posted_time_text", "")
        if posted_time_text:
            record.posted_time_text = posted_time_text

        phone = detail_data.get("phone", "")
        if phone:
            record.phone = phone

        masked_phone = detail_data.get("masked_phone", "")
        if masked_phone:
            record.masked_phone = masked_phone

        phone_confidence = detail_data.get("phone_confidence", "")
        if phone_confidence:
            record.phone_confidence = phone_confidence

        gallery_image_urls = detail_data.get("gallery_image_urls", [])
        if gallery_image_urls:
            record.gallery_image_urls = gallery_image_urls
            if not record.image_url:
                record.image_url = gallery_image_urls[0]

        image_count = detail_data.get("image_count")
        if isinstance(image_count, int) and image_count >= 0:
            record.image_count = image_count

    def scrape(self, search_base_url: str) -> tuple[List[ListingRecord], ScrapeStats, Path]:
        """Scrape configured number of pages and export result files."""

        stats = ScrapeStats()
        all_records: list[ListingRecord] = []

        output_dir = Path(self.settings.output_directory)
        images_dir = output_dir / self.settings.images_subdirectory
        excel_path = output_dir / self.settings.excel_filename
        deadline_monotonic = None
        if self.settings.max_runtime_minutes is not None:
            deadline_monotonic = time.monotonic() + (self.settings.max_runtime_minutes * 60)

        os.makedirs(output_dir, exist_ok=True)

        for page_number in range(1, self.settings.max_pages + 1):
            if self._is_runtime_exceeded(deadline_monotonic):
                stats.runtime_limited = True
                break

            page_url = build_page_url(search_base_url, page_number)
            stats.pages_requested += 1

            try:
                response = self.http_client.get(page_url)
                if response.status_code != 200:
                    stats.pages_failed += 1
                    LOGGER.warning("Skipping page %s due to status %s", page_url, response.status_code)
                else:
                    page_records = parse_listing_cards(response.text, page_url)

                    for record in page_records:
                        if self._is_runtime_exceeded(deadline_monotonic):
                            stats.runtime_limited = True
                            break

                        if not record.listing_url:
                            continue

                        stats.detail_pages_requested += 1
                        try:
                            detail_response = self.http_client.get(record.listing_url)
                            if detail_response.status_code != 200:
                                stats.detail_pages_failed += 1
                                LOGGER.warning(
                                    "Skipping detail page %s due to status %s",
                                    record.listing_url,
                                    detail_response.status_code,
                                )
                            else:
                                detail_data = parse_detail_page(detail_response.text, record.listing_url)
                                self._merge_detail_data(record, detail_data)
                                stats.detail_pages_succeeded += 1
                        except Exception as detail_exc:  # noqa: BLE001 - keep run alive on bad detail pages.
                            stats.detail_pages_failed += 1
                            LOGGER.warning("Failed to parse detail page %s: %s", record.listing_url, detail_exc)

                        if self._is_runtime_exceeded(deadline_monotonic):
                            stats.runtime_limited = True
                            break

                        self.http_client.polite_sleep()

                    all_records.extend(page_records)
                    stats.pages_succeeded += 1
                    stats.listings_found += len(page_records)
            except Exception as exc:  # noqa: BLE001 - keep run alive on one bad page.
                stats.pages_failed += 1
                LOGGER.exception("Failed to scrape page %s: %s", page_url, exc)

            if self._is_runtime_exceeded(deadline_monotonic):
                stats.runtime_limited = True
                break

            if page_number < self.settings.max_pages and self.settings.page_delay_seconds > 0:
                page_delay = self.settings.page_delay_seconds * random.uniform(0.75, 1.25)
                remaining_runtime = self._remaining_runtime_seconds(deadline_monotonic)
                if remaining_runtime is not None and page_delay > remaining_runtime:
                    stats.runtime_limited = True
                    LOGGER.info(
                        "Skipping page delay (%.1fs) because only %.1fs runtime remains",
                        page_delay,
                        remaining_runtime,
                    )
                    break

                LOGGER.info("Waiting %ss before next page...", int(round(page_delay)))
                time.sleep(page_delay)

        # De-duplicate by listing URL after all pages are processed.
        deduped_records: list[ListingRecord] = []
        seen_urls: set[str] = set()
        for record in all_records:
            if record.listing_url in seen_urls:
                continue
            deduped_records.append(record)
            seen_urls.add(record.listing_url)

        downloaded_count, failed_count, timed_out_during_images = download_listing_images(
            records=deduped_records,
            output_images_dir=images_dir,
            http_client=self.http_client,
            deadline_monotonic=deadline_monotonic,
        )

        stats.images_downloaded = downloaded_count
        stats.images_failed = failed_count
        if timed_out_during_images:
            stats.runtime_limited = True

        exported_count = export_to_excel(deduped_records, excel_path)
        stats.listings_exported = exported_count

        return deduped_records, stats, excel_path

    def close(self) -> None:
        self.http_client.close()
