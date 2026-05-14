from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from olx_scraper.config import DEFAULT_USER_AGENT, ScraperSettings
from olx_scraper.scraper import OLXPropertyScraper


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OLX.pk property scraper")
    parser.add_argument(
        "--url",
        required=True,
        help=(
            "OLX search URL. Supports either {page} placeholder or normal URL query. "
            "Example: https://www.olx.com.pk/properties_c3?page={page}"
        ),
    )
    parser.add_argument("--pages", type=int, default=3, help="Number of pages to scrape")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")
    parser.add_argument("--delay-min", type=float, default=1.2, help="Minimum delay between requests")
    parser.add_argument("--delay-max", type=float, default=2.8, help="Maximum delay between requests")
    parser.add_argument(
        "--page-delay",
        type=float,
        default=45.0,
        help="Base delay in seconds between pages (applies +/-25%% jitter)",
    )
    parser.add_argument("--retries", type=int, default=3, help="Retry attempts for transient errors")
    parser.add_argument("--backoff", type=float, default=1.2, help="Backoff factor for retries")
    parser.add_argument("--output-dir", default="output", help="Output directory path")
    parser.add_argument("--images-dir", default="images", help="Images subdirectory name")
    parser.add_argument("--excel-name", default="olx_property_listings.xlsx", help="Excel output filename")
    parser.add_argument(
        "--output",
        default=None,
        help="Full path for the Excel output file (overrides --output-dir and --excel-name)",
    )
    parser.add_argument(
        "--max-runtime",
        type=float,
        default=None,
        help="Maximum runtime in minutes for the whole process (search + detail + downloads)",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP user-agent header")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)

    if args.pages < 1:
        logging.error("--pages must be >= 1")
        return 2
    if args.delay_min < 0 or args.delay_max < 0 or args.delay_min > args.delay_max:
        logging.error("Invalid delay range")
        return 2
    if args.page_delay < 0:
        logging.error("--page-delay must be >= 0")
        return 2
    if args.max_runtime is not None and args.max_runtime <= 0:
        logging.error("--max-runtime must be > 0 when provided")
        return 2

    if args.output is not None:
        output_path = Path(args.output)
        output_dir = str(output_path.parent)
        excel_name = output_path.name
    else:
        output_dir = args.output_dir
        excel_name = args.excel_name

    settings = ScraperSettings(
        request_timeout_seconds=args.timeout,
        delay_min_seconds=args.delay_min,
        delay_max_seconds=args.delay_max,
        page_delay_seconds=args.page_delay,
        max_retries=args.retries,
        backoff_factor=args.backoff,
        max_pages=args.pages,
        output_directory=output_dir,
        images_subdirectory=args.images_dir,
        excel_filename=excel_name,
        max_runtime_minutes=args.max_runtime,
    )

    scraper = OLXPropertyScraper(settings=settings, user_agent=args.user_agent)
    try:
        records, stats, excel_path = scraper.scrape(args.url)
    finally:
        scraper.close()

    logging.info("Scraped %s records (%s unique exported)", stats.listings_found, stats.listings_exported)
    logging.info("Pages: requested=%s succeeded=%s failed=%s", stats.pages_requested, stats.pages_succeeded, stats.pages_failed)
    logging.info(
        "Detail pages: requested=%s succeeded=%s failed=%s",
        stats.detail_pages_requested,
        stats.detail_pages_succeeded,
        stats.detail_pages_failed,
    )
    logging.info("Images: downloaded=%s failed_or_missing=%s", stats.images_downloaded, stats.images_failed)
    logging.info("Excel output: %s", excel_path)
    if stats.runtime_limited:
        logging.warning("Stopped early due to --max-runtime limit; exported collected results.")

    if not records:
        logging.warning("No listings found. Check URL, selectors, and anti-bot defenses.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
