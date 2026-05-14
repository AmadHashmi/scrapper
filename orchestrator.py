#!/usr/bin/env python
"""
OLX scraper orchestrator.

Runs multiple gentle scraping sessions separated by cooldown periods to work
around OLX IP-based rate limiting. Each session writes to a timestamped Excel
file so data from previous sessions is never overwritten.

Usage examples
--------------
# Quick test (1 session, no cooldown):
python orchestrator.py --url "..." --sessions 1 --cooldown-minutes 0 --session-pages 1 --page-delay 30 --max-runtime 5

# Demo run (1 session with 5 min cooldown shown):
python orchestrator.py --url "..." --sessions 1 --cooldown-minutes 5 --session-pages 2 --page-delay 90 --max-runtime 10

# Overnight run (5 sessions, 45 min cooldown between each):
python orchestrator.py --url "..." --sessions 5 --cooldown-minutes 45 --session-pages 2 --page-delay 90 --max-runtime 15
"""

from __future__ import annotations

import argparse
import logging
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


LOGGER = logging.getLogger(__name__)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OLX scraper orchestrator – runs sessions with cooldown between them"
    )
    parser.add_argument(
        "--url",
        required=True,
        help="OLX search URL with {page} placeholder",
    )
    parser.add_argument("--sessions", type=int, default=3, help="Total number of scraping sessions to run")
    parser.add_argument("--session-pages", type=int, default=2, help="Pages to scrape per session")
    parser.add_argument(
        "--page-delay",
        type=float,
        default=90.0,
        help="Base delay in seconds between pages inside a session (+/-25%% jitter applied by scraper)",
    )
    parser.add_argument(
        "--max-runtime",
        type=float,
        default=10.0,
        help="Maximum runtime in minutes per session",
    )
    parser.add_argument(
        "--cooldown-minutes",
        type=float,
        default=30.0,
        help="Base cooldown in minutes between sessions",
    )
    parser.add_argument(
        "--cooldown-jitter",
        type=float,
        default=20.0,
        help="Jitter percentage applied to cooldown (e.g. 20 means +/-20%%)",
    )
    parser.add_argument("--output-dir", default="output", help="Directory for output files")
    parser.add_argument("--retries", type=int, default=1, help="Retry attempts per request")
    parser.add_argument("--verbose", action="store_true", help="Pass --verbose to the scraper and show debug logs")
    return parser.parse_args()


def build_scraper_cmd(
    url: str,
    pages: int,
    page_delay: float,
    max_runtime: float,
    output_file: Path,
    retries: int,
    verbose: bool,
) -> list[str]:
    cmd = [
        sys.executable,
        "main.py",
        "--url", url,
        "--pages", str(pages),
        "--page-delay", str(page_delay),
        "--max-runtime", str(max_runtime),
        "--output", str(output_file),
        "--retries", str(retries),
    ]
    if verbose:
        cmd.append("--verbose")
    return cmd


def run_session(session_num: int, total_sessions: int, cmd: list[str], output_file: Path) -> bool:
    """Run one scraper session; return True on success."""
    LOGGER.info("=== Session %d/%d starting ===", session_num, total_sessions)
    LOGGER.info("Output file: %s", output_file)
    LOGGER.debug("Command: %s", " ".join(cmd))

    start = time.monotonic()
    try:
        result = subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        LOGGER.info("Session %d interrupted by user.", session_num)
        raise

    elapsed = time.monotonic() - start
    if result.returncode == 0:
        LOGGER.info(
            "=== Session %d/%d finished (%.0fs) ===",
            session_num,
            total_sessions,
            elapsed,
        )
        if output_file.exists():
            size_kb = output_file.stat().st_size // 1024
            LOGGER.info("Output confirmed: %s (%d KB)", output_file, size_kb)
        else:
            LOGGER.warning("Output file not found after session: %s", output_file)
        return True
    else:
        LOGGER.warning(
            "Session %d exited with code %d (elapsed %.0fs)",
            session_num,
            result.returncode,
            elapsed,
        )
        return False


def cooldown_sleep(cooldown_minutes: float, jitter_pct: float) -> None:
    """Sleep for cooldown duration with jitter; handles Ctrl+C gracefully."""
    jitter_fraction = jitter_pct / 100.0
    actual_seconds = cooldown_minutes * 60 * random.uniform(
        1 - jitter_fraction, 1 + jitter_fraction
    )
    actual_minutes = actual_seconds / 60
    LOGGER.info(
        "Cooldown: waiting %.0fs (~%.1f min) before next session...",
        actual_seconds,
        actual_minutes,
    )
    deadline = time.monotonic() + actual_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Log progress every 60 s so the user knows the process is alive.
            chunk = min(remaining, 60.0)
            time.sleep(chunk)
            remaining = deadline - time.monotonic()
            if remaining > 5:
                LOGGER.info("Cooldown: %.0fs remaining...", remaining)
    except KeyboardInterrupt:
        LOGGER.info("Cooldown interrupted by user.")
        raise


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)

    if args.sessions < 1:
        LOGGER.error("--sessions must be >= 1")
        return 2
    if args.session_pages < 1:
        LOGGER.error("--session-pages must be >= 1")
        return 2
    if args.cooldown_minutes < 0:
        LOGGER.error("--cooldown-minutes must be >= 0")
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    LOGGER.info(
        "Orchestrator starting: %d session(s), %d page(s)/session, "
        "%.0fs page-delay, %.1f min cooldown (+/-%.0f%% jitter)",
        args.sessions,
        args.session_pages,
        args.page_delay,
        args.cooldown_minutes,
        args.cooldown_jitter,
    )

    completed_sessions: list[tuple[int, Path]] = []
    failed_sessions: list[int] = []

    for session_num in range(1, args.sessions + 1):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"olx_property_listings_{timestamp}.xlsx"

        cmd = build_scraper_cmd(
            url=args.url,
            pages=args.session_pages,
            page_delay=args.page_delay,
            max_runtime=args.max_runtime,
            output_file=output_file,
            retries=args.retries,
            verbose=args.verbose,
        )

        try:
            success = run_session(session_num, args.sessions, cmd, output_file)
        except KeyboardInterrupt:
            LOGGER.info("Orchestrator interrupted after session %d. Exiting.", session_num)
            break

        if success:
            completed_sessions.append((session_num, output_file))
        else:
            failed_sessions.append(session_num)

        # Cooldown between sessions (not after the very last one).
        if session_num < args.sessions and args.cooldown_minutes > 0:
            try:
                cooldown_sleep(args.cooldown_minutes, args.cooldown_jitter)
            except KeyboardInterrupt:
                LOGGER.info("Orchestrator interrupted during cooldown. Exiting.")
                break

    # ── Final summary ──────────────────────────────────────────────────────────
    LOGGER.info(
        "=== Orchestrator done: %d completed, %d failed ===",
        len(completed_sessions),
        len(failed_sessions),
    )
    for num, path in completed_sessions:
        status = "EXISTS" if path.exists() else "MISSING"
        size_info = f" ({path.stat().st_size // 1024} KB)" if path.exists() else ""
        LOGGER.info("  Session %d | %s | %s%s", num, status, path, size_info)
    for num in failed_sessions:
        LOGGER.warning("  Session %d | FAILED", num)

    return 0 if not failed_sessions else 1


if __name__ == "__main__":
    sys.exit(main())
