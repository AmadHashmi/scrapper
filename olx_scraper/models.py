from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ListingRecord:
    """Represents one parsed property listing."""

    listing_id: str
    title: str
    price_text: str
    location_text: str
    posted_time_text: str
    details_text: str
    area_text: str
    bedrooms: Optional[int]
    bathrooms: Optional[int]
    listing_url: str
    image_url: str
    image_file: str = ""
    description: str = ""
    address: str = ""
    seller_type: str = ""
    property_type: str = ""
    phone: str = ""
    masked_phone: str = ""
    phone_confidence: str = "none"
    gallery_image_urls: list[str] = field(default_factory=list)
    image_filenames: str = ""
    image_count: int = 0
    scraped_at_utc: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))


@dataclass
class ScrapeStats:
    """Simple summary stats for one scraper run."""

    pages_requested: int = 0
    pages_succeeded: int = 0
    pages_failed: int = 0
    detail_pages_requested: int = 0
    detail_pages_succeeded: int = 0
    detail_pages_failed: int = 0
    listings_found: int = 0
    listings_exported: int = 0
    images_downloaded: int = 0
    images_failed: int = 0
    runtime_limited: bool = False
