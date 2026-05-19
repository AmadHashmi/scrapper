from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from olx_scraper.models import ListingRecord


QUEUE_STATUSES = ("pending", "in_progress", "done", "failed", "retry")


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


class CheckpointDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def init_schema(self) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS listings_queue (
                    listing_id TEXT PRIMARY KEY,
                    listing_url TEXT NOT NULL UNIQUE,
                    source_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    last_error TEXT,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'retry'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_data (
                    listing_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    phone TEXT,
                    masked_phone TEXT,
                    phone_confidence TEXT,
                    saved_at TEXT NOT NULL,
                    FOREIGN KEY (listing_id) REFERENCES listings_queue(listing_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_status_retry ON listings_queue(status, next_retry_at)"
            )

    def set_run_state(self, key: str, value: str) -> None:
        timestamp = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO run_state(state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE
                SET state_value = excluded.state_value,
                    updated_at = excluded.updated_at
                """,
                (key, value, timestamp),
            )

    def get_run_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT state_value FROM run_state WHERE state_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return str(row["state_value"])

    def reset_in_progress_to_retry(self) -> int:
        timestamp = now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE listings_queue
                SET status = 'retry',
                    next_retry_at = ?,
                    updated_at = ?,
                    last_error = COALESCE(last_error, 'Recovered after interrupted run')
                WHERE status = 'in_progress'
                """,
                (timestamp, timestamp),
            )
        return int(cursor.rowcount)

    def upsert_discovered(self, records: list[ListingRecord]) -> int:
        discovered = 0
        timestamp = now_iso()
        with self.transaction() as conn:
            for record in records:
                source_json = json.dumps(record.__dict__, ensure_ascii=False)
                cursor = conn.execute(
                    """
                    INSERT INTO listings_queue(
                        listing_id,
                        listing_url,
                        source_json,
                        status,
                        discovered_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(listing_id) DO UPDATE
                    SET listing_url = excluded.listing_url,
                        source_json = excluded.source_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        record.listing_id,
                        record.listing_url,
                        source_json,
                        timestamp,
                        timestamp,
                    ),
                )
                if cursor.rowcount > 0:
                    discovered += 1
        return discovered

    def claim_next_for_enrichment(self) -> sqlite3.Row | None:
        timestamp = now_iso()
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT listing_id, listing_url, source_json, attempts
                FROM listings_queue
                WHERE status IN ('pending', 'retry')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY discovered_at ASC
                LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                return None

            conn.execute(
                """
                UPDATE listings_queue
                SET status = 'in_progress',
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE listing_id = ?
                """,
                (timestamp, row["listing_id"]),
            )
            return row

    def mark_done(self, listing_id: str) -> None:
        timestamp = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE listings_queue
                SET status = 'done',
                    next_retry_at = NULL,
                    last_error = NULL,
                    updated_at = ?
                WHERE listing_id = ?
                """,
                (timestamp, listing_id),
            )

    def mark_retry(self, listing_id: str, error_text: str, next_retry_at: str) -> None:
        timestamp = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE listings_queue
                SET status = 'retry',
                    last_error = ?,
                    next_retry_at = ?,
                    updated_at = ?
                WHERE listing_id = ?
                """,
                (error_text[:500], next_retry_at, timestamp, listing_id),
            )

    def mark_failed(self, listing_id: str, error_text: str) -> None:
        timestamp = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE listings_queue
                SET status = 'failed',
                    last_error = ?,
                    next_retry_at = NULL,
                    updated_at = ?
                WHERE listing_id = ?
                """,
                (error_text[:500], timestamp, listing_id),
            )

    def save_listing_payload(self, listing_id: str, payload: dict[str, Any]) -> None:
        timestamp = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO listing_data(listing_id, payload_json, phone, masked_phone, phone_confidence, saved_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_id) DO UPDATE
                SET payload_json = excluded.payload_json,
                    phone = excluded.phone,
                    masked_phone = excluded.masked_phone,
                    phone_confidence = excluded.phone_confidence,
                    saved_at = excluded.saved_at
                """,
                (
                    listing_id,
                    json.dumps(payload, ensure_ascii=False),
                    str(payload.get("phone") or ""),
                    str(payload.get("masked_phone") or ""),
                    str(payload.get("phone_confidence") or "none"),
                    timestamp,
                ),
            )

    def load_source_record(self, source_json: str) -> ListingRecord:
        raw = json.loads(source_json)
        return ListingRecord(**raw)

    def list_done_records(self) -> list[ListingRecord]:
        rows = self.conn.execute(
            """
            SELECT payload_json
            FROM listing_data
            ORDER BY saved_at ASC
            """
        ).fetchall()
        output: list[ListingRecord] = []
        for row in rows:
            data = json.loads(str(row["payload_json"]))
            output.append(ListingRecord(**data))
        return output

    def queue_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM listings_queue
            GROUP BY status
            """
        ).fetchall()
        counts = {status: 0 for status in QUEUE_STATUSES}
        for row in rows:
            counts[str(row["status"])] = int(row["total"])
        return counts
