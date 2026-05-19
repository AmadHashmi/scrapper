#!/usr/bin/env python
"""Two-pass OLX scraper orchestrator with checkpointing.

Modes:
- discover: crawl search pages and enqueue listing URLs + base metadata
- enrich: process queue items one-by-one, scrape detail page data, and persist output
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from olx_scraper.config import DEFAULT_USER_AGENT, ScraperSettings
from olx_scraper.detail_parser import parse_detail_page
from olx_scraper.exporter import export_to_excel, export_to_jsonl
from olx_scraper.http_client import HttpClient
from olx_scraper.image_downloader import download_listing_images
from olx_scraper.models import ListingRecord
from olx_scraper.olx_parser import parse_listings
from olx_scraper.scraper import build_page_url
from persistence.db import CheckpointDB


LOGGER = logging.getLogger(__name__)
ADAPTIVE_COOLDOWN_MINUTES = [15, 45, 120]


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OLX discover/enrich orchestrator")
    parser.add_argument("--mode", choices=["discover", "enrich"], required=True)
    parser.add_argument("--url", required=True, help="OLX search URL (supports {page} placeholder)")
    parser.add_argument("--type", choices=["all", "rent", "sale"], default="all")
    parser.add_argument("--max-pages", type=int, default=5, help="Max search pages to discover")
    parser.add_argument("--max-listings", type=int, default=100, help="Max queue listings to enrich")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--delay-min", type=float, default=1.2)
    parser.add_argument("--delay-max", type=float, default=2.8)
    parser.add_argument("--page-delay", type=float, default=45.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--backoff", type=float, default=1.2)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--db-path", default="output/checkpoint.sqlite3")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--excel-name", default="olx_property_listings.xlsx")
    parser.add_argument("--jsonl-name", default="listings.jsonl")
    parser.add_argument("--images-dir", default="images")
    parser.add_argument("--download-images", action="store_true", help="Download gallery images in enrich mode")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def build_settings(args: argparse.Namespace) -> ScraperSettings:
    return ScraperSettings(
        request_timeout_seconds=args.timeout,
        delay_min_seconds=args.delay_min,
        delay_max_seconds=args.delay_max,
        page_delay_seconds=args.page_delay,
        max_retries=args.retries,
        backoff_factor=args.backoff,
        max_pages=max(1, args.max_pages),
        output_directory=args.output_dir,
        images_subdirectory=args.images_dir,
        excel_filename=args.excel_name,
    )


def matches_listing_type(record: ListingRecord, listing_type: str) -> bool:
    if listing_type == "all":
        return True

    haystack = " ".join(
        [
            record.title.lower(),
            record.details_text.lower(),
            record.location_text.lower(),
        ]
    )
    if listing_type == "rent":
        return "rent" in haystack
    return "sale" in haystack or "sell" in haystack


def wait_with_logs(total_seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, total_seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        chunk = min(remaining, 60.0)
        time.sleep(chunk)
        still_remaining = deadline - time.monotonic()
        if still_remaining > 10:
            LOGGER.info("Cooldown active: %.0fs remaining", still_remaining)


def discover_mode(args: argparse.Namespace, settings: ScraperSettings, db: CheckpointDB) -> int:
    http = HttpClient(
        user_agent=args.user_agent,
        timeout_seconds=settings.request_timeout_seconds,
        min_delay_seconds=settings.delay_min_seconds,
        max_delay_seconds=settings.delay_max_seconds,
        max_retries=settings.max_retries,
        backoff_factor=settings.backoff_factor,
    )
    discovered_total = 0
    try:
        for page_num in range(1, args.max_pages + 1):
            page_url = build_page_url(args.url, page_num)
            LOGGER.info("Discover page %d/%d: %s", page_num, args.max_pages, page_url)
            response = http.get(page_url)

            if response.status_code == 429:
                LOGGER.warning("Rate limited in discover mode on page %d; stopping early", page_num)
                return 1
            if response.status_code >= 400:
                LOGGER.warning("Page fetch failed with HTTP %s: %s", response.status_code, page_url)
                continue

            page_records = parse_listings(response.text, page_url)
            page_records = [record for record in page_records if matches_listing_type(record, args.type)]
            if not page_records:
                LOGGER.info("No listings parsed on page %d", page_num)
            inserted = db.upsert_discovered(page_records)
            discovered_total += inserted
            LOGGER.info("Queued %d listing(s) from page %d", inserted, page_num)

            if page_num < args.max_pages:
                jitter = random.uniform(0.75, 1.25)
                sleep_seconds = max(0.0, settings.page_delay_seconds * jitter)
                if sleep_seconds > 0:
                    LOGGER.info("Sleeping %.1fs before next discover page", sleep_seconds)
                    wait_with_logs(sleep_seconds)
    finally:
        http.close()

    counts = db.queue_counts()
    LOGGER.info("Discover complete. Newly queued=%d, totals=%s", discovered_total, counts)
    return 0


def compose_enriched_record(source: ListingRecord, detail: dict[str, object]) -> ListingRecord:
    source.description = str(detail.get("description") or source.description)
    source.area_text = str(detail.get("area_text") or source.area_text)
    source.bedrooms = detail.get("bedrooms") if detail.get("bedrooms") is not None else source.bedrooms
    source.bathrooms = detail.get("bathrooms") if detail.get("bathrooms") is not None else source.bathrooms
    source.address = str(detail.get("address") or source.address)
    source.seller_type = str(detail.get("seller_type") or source.seller_type)
    source.property_type = str(detail.get("property_type") or source.property_type)
    source.posted_time_text = str(detail.get("posted_time_text") or source.posted_time_text)
    source.phone = str(detail.get("phone") or source.phone)
    source.masked_phone = str(detail.get("masked_phone") or source.masked_phone)
    source.phone_confidence = str(detail.get("phone_confidence") or source.phone_confidence)

    gallery = detail.get("gallery_image_urls")
    if isinstance(gallery, list):
        source.gallery_image_urls = [str(item) for item in gallery if str(item).strip()]
        source.image_count = len(source.gallery_image_urls)
        if source.image_count > 0 and not source.image_url:
            source.image_url = source.gallery_image_urls[0]
    return source


def apply_rate_limit_cooldown(db: CheckpointDB, rate_limit_hits: int) -> datetime:
    index = min(max(rate_limit_hits - 1, 0), len(ADAPTIVE_COOLDOWN_MINUTES) - 1)
    cooldown_minutes = ADAPTIVE_COOLDOWN_MINUTES[index]
    cooldown_until = datetime.utcnow() + timedelta(minutes=cooldown_minutes)
    cooldown_iso = cooldown_until.replace(microsecond=0).isoformat()
    db.set_run_state("cooldown_until", cooldown_iso)
    LOGGER.warning("Rate limited. Cooldown set to %d minute(s) until %s", cooldown_minutes, cooldown_iso)
    return cooldown_until


def get_active_cooldown(db: CheckpointDB) -> datetime | None:
    raw = db.get_run_state("cooldown_until")
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value <= datetime.utcnow():
        db.set_run_state("cooldown_until", "")
        return None
    return value


def enrich_mode(args: argparse.Namespace, settings: ScraperSettings, db: CheckpointDB) -> int:
    db.reset_in_progress_to_retry()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / args.jsonl_name

    consecutive_rate_limits = 0
    processed = 0
    http = HttpClient(
        user_agent=args.user_agent,
        timeout_seconds=settings.request_timeout_seconds,
        min_delay_seconds=settings.delay_min_seconds,
        max_delay_seconds=settings.delay_max_seconds,
        max_retries=settings.max_retries,
        backoff_factor=settings.backoff_factor,
    )
    try:
        while processed < args.max_listings:
            active_cooldown = get_active_cooldown(db)
            if active_cooldown is not None:
                wait_seconds = (active_cooldown - datetime.utcnow()).total_seconds()
                if wait_seconds > 0:
                    wait_with_logs(wait_seconds)

            item = db.claim_next_for_enrichment()
            if item is None:
                LOGGER.info("Queue exhausted for now; nothing left to enrich.")
                break

            listing_id = str(item["listing_id"])
            listing_url = str(item["listing_url"])
            source_record = db.load_source_record(str(item["source_json"]))
            attempts = int(item["attempts"]) + 1

            try:
                response = http.get(listing_url)
                if response.status_code == 429:
                    consecutive_rate_limits += 1
                    cooldown_until = apply_rate_limit_cooldown(db, consecutive_rate_limits)
                    db.mark_retry(
                        listing_id,
                        f"HTTP 429 rate limit on attempt {attempts}",
                        cooldown_until.replace(microsecond=0).isoformat(),
                    )
                    continue

                if response.status_code >= 500:
                    retry_at = (datetime.utcnow() + timedelta(minutes=10)).replace(microsecond=0).isoformat()
                    db.mark_retry(listing_id, f"HTTP {response.status_code}", retry_at)
                    continue

                if response.status_code >= 400:
                    if attempts >= 3:
                        db.mark_failed(listing_id, f"HTTP {response.status_code}")
                    else:
                        retry_at = (datetime.utcnow() + timedelta(minutes=30)).replace(microsecond=0).isoformat()
                        db.mark_retry(listing_id, f"HTTP {response.status_code}", retry_at)
                    continue

                detail = parse_detail_page(response.text, listing_url)
                enriched_record = compose_enriched_record(source_record, detail)

                if args.download_images and enriched_record.gallery_image_urls:
                    download_listing_images(
                        [enriched_record],
                        output_images_dir=output_dir / args.images_dir,
                        http_client=http,
                    )

                db.save_listing_payload(listing_id, enriched_record.__dict__)
                db.mark_done(listing_id)

                # Keep an always-fresh canonical JSONL artifact while the run progresses.
                done_records = db.list_done_records()
                export_to_jsonl(done_records, jsonl_path)

                processed += 1
                consecutive_rate_limits = 0
                jitter = random.uniform(settings.delay_min_seconds, settings.delay_max_seconds)
                if jitter > 0:
                    time.sleep(jitter)
            except Exception as exc:  # noqa: BLE001
                if attempts >= 4:
                    db.mark_failed(listing_id, str(exc))
                else:
                    retry_at = (datetime.utcnow() + timedelta(minutes=15)).replace(microsecond=0).isoformat()
                    db.mark_retry(listing_id, str(exc), retry_at)

    finally:
        http.close()

    done_records = db.list_done_records()
    jsonl_count = export_to_jsonl(done_records, jsonl_path)
    excel_path = output_dir / args.excel_name
    excel_count = export_to_excel(done_records, excel_path)
    LOGGER.info(
        "Enrich complete. Processed=%d done=%d jsonl=%s (%d) excel=%s (%d)",
        processed,
        len(done_records),
        jsonl_path,
        jsonl_count,
        excel_path,
        excel_count,
    )
    LOGGER.info("Queue status: %s", json.dumps(db.queue_counts(), ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)

    if args.max_pages < 1:
        LOGGER.error("--max-pages must be >= 1")
        return 2
    if args.max_listings < 1:
        LOGGER.error("--max-listings must be >= 1")
        return 2
    if args.delay_min < 0 or args.delay_max < 0 or args.delay_min > args.delay_max:
        LOGGER.error("Invalid delay range")
        return 2

    settings = build_settings(args)
    db = CheckpointDB(Path(args.db_path))
    try:
        if args.mode == "discover":
            return discover_mode(args, settings, db)
        return enrich_mode(args, settings, db)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
