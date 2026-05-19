from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import pandas as pd
from openpyxl.styles import Font

from .models import ListingRecord


EXPORT_COLUMNS = [
    "listing_id",
    "title",
    "price_text",
    "location_text",
    "address",
    "posted_time_text",
    "details_text",
    "description",
    "area_text",
    "bedrooms",
    "bathrooms",
    "seller_type",
    "property_type",
    "phone",
    "masked_phone",
    "phone_confidence",
    "listing_url",
    "image_url",
    "image_file",
    "image_filenames",
    "image_count",
    "scraped_at_utc",
]


def export_to_excel(records: Iterable[ListingRecord], output_file: Path) -> int:
    """Export records to Excel and apply basic readability formatting."""

    os.makedirs(output_file.parent, exist_ok=True)

    rows = [record.__dict__ for record in records]
    dataframe = pd.DataFrame(rows)
    if dataframe.empty:
        dataframe = pd.DataFrame(columns=EXPORT_COLUMNS)
    else:
        dataframe = dataframe.reindex(columns=EXPORT_COLUMNS)

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name="Listings")

        worksheet = writer.sheets["Listings"]
        worksheet.freeze_panes = "A2"

        # Improve readability with bold header and adaptive width.
        for column_idx, column_name in enumerate(dataframe.columns, start=1):
            cell = worksheet.cell(row=1, column=column_idx)
            cell.font = Font(bold=True)

            value_lengths = [len(str(value)) for value in dataframe[column_name].fillna("")]
            max_cell_length = max([len(str(column_name)), *value_lengths])
            worksheet.column_dimensions[cell.column_letter].width = min(max_cell_length + 2, 70)

    return len(dataframe)


def export_to_jsonl(records: Iterable[ListingRecord], output_file: Path) -> int:
    """Export records as JSON Lines for machine-readable ingestion workflows."""

    os.makedirs(output_file.parent, exist_ok=True)

    count = 0
    with output_file.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.__dict__, ensure_ascii=False) + "\n")
            count += 1

    return count
