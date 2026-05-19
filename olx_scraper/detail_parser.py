from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup, Tag

from .olx_parser import normalize_text


LOGGER = logging.getLogger(__name__)

WINDOW_STATE_PATTERN = re.compile(r"window\.state\s*=\s*(\{.*?\})\s*;\s*</script>", re.DOTALL)
WINDOW_INITIAL_STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*</script>",
    re.DOTALL,
)
DATA_LAYER_ROOMS_PATTERN = re.compile(r'"rooms"\s*:\s*"?(\d+)"?')
DATA_LAYER_BATHS_PATTERN = re.compile(r'"bathrooms"\s*:\s*"?(\d+)"?')
DATA_LAYER_AREA_VALUE_PATTERN = re.compile(r'"ft"\s*:\s*"?([\d\.]+)"?')
DATA_LAYER_AREA_UNIT_PATTERN = re.compile(r'"ft_unit"\s*:\s*"([^"]+)"')
DATA_LAYER_SELLER_TYPE_PATTERN = re.compile(r'"seller_type"\s*:\s*"([^"]+)"')
DATA_LAYER_CATEGORY_NAME_PATTERN = re.compile(r'"category_2_name"\s*:\s*"([^"]+)"')
PHONE_FULL_PATTERN = re.compile(r"(?<!\d)(?:\+92|0)3\d{2}[-\s]?\d{7}(?!\d)")
PHONE_MASKED_PATTERN = re.compile(r"(?<!\d)(?:\+92|0)3[\dXx\*]{2}[-\s]?[\dXx\*]{7}(?!\d)")
PHONE_KEY_HINTS = ("phone", "mobile", "whatsapp", "contact")


def _extract_srcset_url(srcset_value: str | None) -> str:
    cleaned = normalize_text(srcset_value)
    if not cleaned:
        return ""
    first_entry = cleaned.split(",")[0].strip()
    if not first_entry:
        return ""
    return first_entry.split(" ")[0].strip()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _extract_window_state(html: str) -> dict[str, Any]:
    match = WINDOW_STATE_PATTERN.search(html)
    if match is None:
        return {}

    raw_json = match.group(1)
    try:
        state = json.loads(raw_json)
    except json.JSONDecodeError:
        LOGGER.debug("Failed to decode window.state JSON", exc_info=True)
        return {}

    if not isinstance(state, dict):
        return {}
    return state


def _extract_initial_state(html: str) -> dict[str, Any]:
    match = WINDOW_INITIAL_STATE_PATTERN.search(html)
    if match is None:
        return {}

    raw_json = match.group(1)
    try:
        state = json.loads(raw_json)
    except json.JSONDecodeError:
        LOGGER.debug("Failed to decode window.__INITIAL_STATE__ JSON", exc_info=True)
        return {}

    if not isinstance(state, dict):
        return {}
    return state


def _normalize_phone(value: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", value)
    if cleaned.startswith("92") and not cleaned.startswith("+92"):
        cleaned = f"+{cleaned}"
    return cleaned


def _extract_phone_candidates_from_value(value: Any) -> tuple[list[str], list[str]]:
    text = normalize_text(str(value))
    if not text:
        return [], []

    full_matches = [_normalize_phone(match.group(0)) for match in PHONE_FULL_PATTERN.finditer(text)]
    masked_matches = [normalize_text(match.group(0)) for match in PHONE_MASKED_PATTERN.finditer(text)]

    # Filter masked entries that are actually complete numbers.
    masked_matches = [
        value for value in masked_matches if "x" in value.lower() or "*" in value
    ]
    return full_matches, masked_matches


def _walk_for_phone_candidates(
    node: Any,
    path: tuple[str, ...],
    full_out: list[tuple[str, str]],
    masked_out: list[tuple[str, str]],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_normalized = normalize_text(str(key)).lower()
            next_path = (*path, key_normalized)

            if isinstance(value, (str, int, float)):
                full_values, masked_values = _extract_phone_candidates_from_value(value)
                if full_values or masked_values:
                    joined_path = ".".join(next_path)
                    for full in full_values:
                        full_out.append((joined_path, full))
                    for masked in masked_values:
                        masked_out.append((joined_path, masked))

            if isinstance(value, (dict, list, tuple)):
                _walk_for_phone_candidates(value, next_path, full_out, masked_out)
        return

    if isinstance(node, list):
        for index, value in enumerate(node):
            _walk_for_phone_candidates(value, (*path, str(index)), full_out, masked_out)
        return

    if isinstance(node, (str, int, float)):
        full_values, masked_values = _extract_phone_candidates_from_value(node)
        joined_path = ".".join(path)
        for full in full_values:
            full_out.append((joined_path, full))
        for masked in masked_values:
            masked_out.append((joined_path, masked))


def _path_contains_phone_hint(path: str) -> bool:
    return any(hint in path for hint in PHONE_KEY_HINTS)


def _pick_best_phone(
    full_candidates: list[tuple[str, str]],
    masked_candidates: list[tuple[str, str]],
) -> tuple[str, str, str]:
    """Return (phone, masked_phone, confidence)."""
    if full_candidates:
        hinted = [candidate for candidate in full_candidates if _path_contains_phone_hint(candidate[0])]
        if hinted:
            return hinted[0][1], "", "high"
        return full_candidates[0][1], "", "medium"

    if masked_candidates:
        hinted = [candidate for candidate in masked_candidates if _path_contains_phone_hint(candidate[0])]
        if hinted:
            return "", hinted[0][1], "masked"
        return "", masked_candidates[0][1], "masked"

    return "", "", "none"


def _extract_phone_details(html: str, state_blobs: list[dict[str, Any]]) -> dict[str, str]:
    full_candidates: list[tuple[str, str]] = []
    masked_candidates: list[tuple[str, str]] = []

    for blob in state_blobs:
        _walk_for_phone_candidates(blob, tuple(), full_candidates, masked_candidates)

    phone, masked_phone, confidence = _pick_best_phone(full_candidates, masked_candidates)
    if phone or masked_phone:
        return {
            "phone": phone,
            "masked_phone": masked_phone,
            "phone_confidence": confidence,
        }

    # Fallback to whole-page text scan for partially masked numbers.
    page_full, page_masked = _extract_phone_candidates_from_value(html)
    fallback_phone = page_full[0] if page_full else ""
    fallback_masked = page_masked[0] if page_masked else ""
    fallback_confidence = "medium" if fallback_phone else ("masked" if fallback_masked else "none")
    return {
        "phone": fallback_phone,
        "masked_phone": fallback_masked,
        "phone_confidence": fallback_confidence,
    }


def _extract_ad_state_data(state: dict[str, Any]) -> dict[str, Any]:
    ad_container = state.get("ad")
    if not isinstance(ad_container, dict):
        return {}

    ad_data = ad_container.get("data")
    if not isinstance(ad_data, dict):
        return {}

    return ad_data


def _parse_epoch_to_iso(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""


def _extract_location_from_state(ad_data: dict[str, Any]) -> str:
    locations = ad_data.get("location")
    if not isinstance(locations, list):
        return ""

    labels: list[str] = []
    for item in locations:
        if not isinstance(item, dict):
            continue
        label = normalize_text(item.get("name") or item.get("label") or item.get("localizedName"))
        if label:
            labels.append(label)

    if not labels:
        return ""

    return ", ".join(labels)


def _extract_property_type_from_state(ad_data: dict[str, Any]) -> str:
    categories = ad_data.get("category")
    if not isinstance(categories, list):
        return ""

    last_label = ""
    for category in categories:
        if not isinstance(category, dict):
            continue
        label = normalize_text(category.get("name") or category.get("label"))
        if label:
            last_label = label

    return last_label


def _extract_seller_type_from_state(ad_data: dict[str, Any]) -> str:
    contact_info = ad_data.get("contactInfo")
    if not isinstance(contact_info, dict):
        return ""

    roles = contact_info.get("roles")
    if not isinstance(roles, list):
        return ""

    labels: list[str] = []
    for role in roles:
        if isinstance(role, str):
            role_label = normalize_text(role)
        elif isinstance(role, dict):
            role_label = normalize_text(role.get("name") or role.get("label") or role.get("type"))
        else:
            role_label = ""

        if role_label:
            labels.append(role_label)

    if not labels:
        return ""

    return labels[0]


def _extract_formatted_fields(ad_data: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    field_items = ad_data.get("formattedExtraFields")
    if not isinstance(field_items, list):
        return result

    for item in field_items:
        if not isinstance(item, dict):
            continue

        label = normalize_text(item.get("key") or item.get("label") or item.get("name"))
        value = normalize_text(item.get("value") or item.get("formattedValue"))
        if not label or not value:
            continue

        result[label.lower()] = value

    return result


def _extract_extra_fields(ad_data: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    extra_fields = ad_data.get("extraFields")
    if not isinstance(extra_fields, dict):
        return result

    for raw_key, raw_value in extra_fields.items():
        key = normalize_text(raw_key).lower()
        if not key:
            continue

        if isinstance(raw_value, dict):
            value = normalize_text(
                raw_value.get("value")
                or raw_value.get("formattedValue")
                or raw_value.get("label")
                or raw_value.get("name")
            )
        else:
            value = normalize_text(raw_value)

        if value:
            result[key] = value

    return result


def _extract_key_value_pairs_from_overview(soup: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}
    overview = soup.select_one('[aria-label="Overview"]')
    if overview is None:
        return result

    for row in overview.select("div"):
        spans = row.find_all("span", recursive=False)
        if len(spans) < 2:
            continue

        label = normalize_text(spans[0].get_text(" ", strip=True)).lower()
        value = normalize_text(spans[1].get_text(" ", strip=True))
        if not label or not value:
            continue

        if len(label) > 40 or len(value) > 120:
            continue

        result[label] = value

    # OLX often renders detail labels and values with stable text but dynamic classes.
    for label_node in overview.select("span"):
        label = normalize_text(label_node.get_text(" ", strip=True)).lower()
        if label not in {"bedrooms", "bathrooms", "area", "type", "seller type"}:
            continue

        value_node = label_node.find_next("span")
        if value_node is None or value_node is label_node:
            continue

        value = normalize_text(value_node.get_text(" ", strip=True))
        if value and value.lower() != label:
            result[label] = value

    return result


def _extract_description_from_html(soup: BeautifulSoup) -> str:
    description_section = soup.select_one('[aria-label="Description"]')
    if description_section is None:
        return ""

    for selector in ("span", "p", "div"):
        node = description_section.select_one(selector)
        if node is None:
            continue
        text = normalize_text(node.get_text(" ", strip=True))
        if text and text.lower() != "description":
            return text

    return normalize_text(description_section.get_text(" ", strip=True))


def _extract_address_from_html(soup: BeautifulSoup) -> str:
    location_node = soup.select_one('[aria-label="Overview"] [aria-label="Location"]')
    if location_node is None:
        location_node = soup.select_one('[aria-label="Location"]')
    if location_node is None:
        return ""
    return normalize_text(location_node.get_text(" ", strip=True))


def _extract_posted_time_from_html(soup: BeautifulSoup) -> str:
    posted_node = soup.select_one('[aria-label="Creation date"]')
    if posted_node is None:
        return ""
    return normalize_text(posted_node.get_text(" ", strip=True))


def _extract_gallery_urls(soup: BeautifulSoup) -> list[str]:
    gallery = soup.select_one('[aria-label="Gallery"]')
    if gallery is None:
        return []

    urls: list[str] = []

    for image in gallery.select("img[src], img[data-src]"):
        urls.append(normalize_text(image.get("data-src") or image.get("src")))

    for source in gallery.select("source[srcset], source[data-srcset]"):
        srcset_url = _extract_srcset_url(source.get("data-srcset") or source.get("srcset"))
        if srcset_url:
            urls.append(srcset_url)

    return _dedupe_preserve_order(urls)


def _parse_int(value: str) -> int | None:
    match = re.search(r"\d+", value)
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _extract_from_data_layer_text(html: str) -> dict[str, Any]:
    result: dict[str, Any] = {}

    rooms_match = DATA_LAYER_ROOMS_PATTERN.search(html)
    if rooms_match is not None:
        result["bedrooms"] = _parse_int(rooms_match.group(1))

    baths_match = DATA_LAYER_BATHS_PATTERN.search(html)
    if baths_match is not None:
        result["bathrooms"] = _parse_int(baths_match.group(1))

    area_value_match = DATA_LAYER_AREA_VALUE_PATTERN.search(html)
    area_unit_match = DATA_LAYER_AREA_UNIT_PATTERN.search(html)
    if area_value_match is not None and area_unit_match is not None:
        result["area_text"] = normalize_text(f"{area_value_match.group(1)} {area_unit_match.group(1)}")

    seller_match = DATA_LAYER_SELLER_TYPE_PATTERN.search(html)
    if seller_match is not None:
        result["seller_type"] = normalize_text(seller_match.group(1))

    category_match = DATA_LAYER_CATEGORY_NAME_PATTERN.search(html)
    if category_match is not None:
        result["property_type"] = normalize_text(category_match.group(1))

    return result


def parse_detail_page(html: str, detail_url: str) -> dict[str, Any]:
    """Parse additional listing fields from an OLX detail page."""

    soup = BeautifulSoup(html, "lxml")
    state = _extract_window_state(html)
    initial_state = _extract_initial_state(html)
    ad_data = _extract_ad_state_data(state)

    state_fields = _extract_formatted_fields(ad_data)
    state_fields.update(_extract_extra_fields(ad_data))
    overview_fields = _extract_key_value_pairs_from_overview(soup)
    datalayer_fields = _extract_from_data_layer_text(html)

    description = normalize_text(ad_data.get("rawDescription") or ad_data.get("description"))
    if not description:
        description = _extract_description_from_html(soup)

    address = _extract_location_from_state(ad_data) or _extract_address_from_html(soup)
    seller_type = _extract_seller_type_from_state(ad_data)
    property_type = _extract_property_type_from_state(ad_data)

    area_text = (
        state_fields.get("area")
        or state_fields.get("area_unit")
        or state_fields.get("property_area")
        or overview_fields.get("area")
        or datalayer_fields.get("area_text", "")
    )
    bedrooms = _parse_int(
        state_fields.get("bedrooms", "")
        or state_fields.get("bedroom", "")
        or overview_fields.get("bedrooms", "")
    )
    bathrooms = _parse_int(
        state_fields.get("bathrooms", "")
        or state_fields.get("bathroom", "")
        or overview_fields.get("bathrooms", "")
    )

    if bedrooms is None:
        bedrooms = datalayer_fields.get("bedrooms")
    if bathrooms is None:
        bathrooms = datalayer_fields.get("bathrooms")

    if not property_type:
        property_type = (
            state_fields.get("type")
            or overview_fields.get("type", "")
            or datalayer_fields.get("property_type", "")
        )
    if not seller_type:
        seller_type = (
            state_fields.get("seller type")
            or overview_fields.get("seller type", "")
            or datalayer_fields.get("seller_type", "")
        )

    posted_time_text = _parse_epoch_to_iso(ad_data.get("createdAt"))
    if not posted_time_text:
        posted_time_text = _extract_posted_time_from_html(soup)

    gallery_image_urls = _extract_gallery_urls(soup)
    phone_details = _extract_phone_details(html, [state, initial_state, ad_data])

    if not description:
        LOGGER.warning(
            "Description missing on detail page %s. Page may require JS rendering; Playwright may be needed later.",
            detail_url,
        )

    return {
        "description": description,
        "area_text": normalize_text(area_text),
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "address": address,
        "seller_type": seller_type,
        "property_type": property_type,
        "posted_time_text": posted_time_text,
        "phone": phone_details["phone"],
        "masked_phone": phone_details["masked_phone"],
        "phone_confidence": phone_details["phone_confidence"],
        "gallery_image_urls": gallery_image_urls,
        "image_count": len(gallery_image_urls),
    }
