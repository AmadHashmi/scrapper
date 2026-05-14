from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ScraperSettings:
    """Runtime configuration for scraping and export."""

    request_timeout_seconds: int = 20
    delay_min_seconds: float = 1.2
    delay_max_seconds: float = 2.8
    page_delay_seconds: float = 45.0
    max_retries: int = 3
    backoff_factor: float = 1.2
    max_pages: int = 3
    output_directory: str = "output"
    images_subdirectory: str = "images"
    excel_filename: str = "olx_property_listings.xlsx"
    max_runtime_minutes: Optional[float] = None


DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
