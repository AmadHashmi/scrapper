# OLX.pk Property Scraper

Production-minded Python scraper for OLX property listing pages that:

- fetches listing pages politely (delays + retries)
- parses structured listing fields
- downloads listing images
- exports to Excel (`.xlsx`) with image file paths

## Tech Stack

- Python 3.10+
- requests
- BeautifulSoup4 + lxml
- pandas
- openpyxl

## Project Structure

```text
scrapper/
  main.py
  requirements.txt
  olx_scraper/
    config.py
    exporter.py
    http_client.py
    image_downloader.py
    models.py
    olx_parser.py
    scraper.py
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

Use a URL with `{page}` placeholder:

```bash
python main.py --url "https://www.olx.com.pk/properties_c3?page={page}" --pages 5 --verbose
```

Or a normal URL (the scraper injects `page=` automatically):

```bash
python main.py --url "https://www.olx.com.pk/properties_c3" --pages 5
```

For gentler multi-page scraping, add a page-level delay:

```bash
python main.py --url "https://www.olx.com.pk/properties_c3?page={page}" --pages 10 --page-delay 45 --retries 1 --verbose
```

`--page-delay` is the base pause in seconds between pages (default: `45`).
The scraper adds a random jitter of +/-25% automatically, so `45` becomes roughly `34-56` seconds.

Outputs are written under `output/` by default:

- `output/olx_property_listings.xlsx`
- `output/images/*`

## Notes on Reliability and Respectful Scraping

- Retries handle transient `429` / `5xx` responses.
- Random delays reduce bursty traffic patterns.
- Page-level pacing (`--page-delay`) helps avoid throttling on consecutive page requests.
- Keep request rate low and avoid scraping too many pages too quickly.
- Review OLX terms and robots policy before large-scale runs.

## Common Enhancements

- Add detailed listing-page scraping for extra attributes.
- Add Playwright fallback if content moves behind heavy JS rendering.
- Add proxy rotation and exponential cooldown when rate-limited.
- Add tests with saved HTML fixtures for parser stability.
