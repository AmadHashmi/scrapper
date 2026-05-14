from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .http_client import HttpClient
from .models import ListingRecord


LOGGER = logging.getLogger(__name__)


def _safe_extension_from_url(url: str) -> str:
    parsed = urlparse(url)
    extension = Path(parsed.path).suffix.lower()
    if extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return extension
    return ".jpg"


def download_listing_images(
    records: Iterable[ListingRecord],
    output_images_dir: Path,
    http_client: HttpClient,
    deadline_monotonic: float | None = None,
) -> tuple[int, int, bool]:
    """Download listing images and set local file path on each record."""

    os.makedirs(output_images_dir, exist_ok=True)
    downloaded_count = 0
    failed_count = 0
    stopped_due_to_runtime = False

    for record in records:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            stopped_due_to_runtime = True
            break

        image_sources = record.gallery_image_urls or ([record.image_url] if record.image_url else [])
        if not image_sources:
            failed_count += 1
            continue

        downloaded_filenames: list[str] = []

        for index, image_url in enumerate(image_sources, start=1):
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                stopped_due_to_runtime = True
                break

            extension = _safe_extension_from_url(image_url)
            image_filename = f"ad-{record.listing_id}-{index}{extension}"
            image_path = output_images_dir / image_filename

            try:
                response = http_client.get(image_url)
                if response.status_code != 200:
                    failed_count += 1
                    LOGGER.warning(
                        "Image download failed (%s): %s",
                        response.status_code,
                        image_url,
                    )
                    continue

                with open(image_path, "wb") as output_handle:
                    output_handle.write(response.content)

                downloaded_filenames.append(image_filename)
                downloaded_count += 1
            except Exception as exc:  # noqa: BLE001 - log and continue for robust runs.
                failed_count += 1
                LOGGER.warning("Image download error for %s: %s", image_url, exc)

            http_client.polite_sleep()

        if downloaded_filenames:
            record.image_file = str(output_images_dir / downloaded_filenames[0])
            record.image_filenames = ", ".join(downloaded_filenames)
            record.image_count = len(downloaded_filenames)

        if stopped_due_to_runtime:
            break

    return downloaded_count, failed_count, stopped_due_to_runtime
