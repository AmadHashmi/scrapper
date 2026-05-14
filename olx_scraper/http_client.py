from __future__ import annotations

import logging
import random
import time
from typing import Optional

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LOGGER = logging.getLogger(__name__)


class HttpClient:
    """Requests client with retries and polite random delays."""

    def __init__(
        self,
        user_agent: str,
        timeout_seconds: int,
        min_delay_seconds: float,
        max_delay_seconds: float,
        max_retries: int,
        backoff_factor: float,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.session = self._build_session(
            user_agent=user_agent,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
        )

    @staticmethod
    def _build_session(user_agent: str, max_retries: int, backoff_factor: float) -> Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
            respect_retry_after_header=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Connection": "keep-alive",
            }
        )
        return session

    def polite_sleep(self) -> None:
        sleep_seconds = random.uniform(self.min_delay_seconds, self.max_delay_seconds)
        LOGGER.debug("Sleeping %.2f seconds between requests", sleep_seconds)
        time.sleep(sleep_seconds)

    def get(self, url: str, params: Optional[dict] = None) -> Response:
        response = self.session.get(url, timeout=self.timeout_seconds, params=params)
        return response

    def close(self) -> None:
        self.session.close()
