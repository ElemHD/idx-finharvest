from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from playwright.sync_api import Page

from src.config_loader import ApiConfig, DelaysConfig, NetworkConfig
from src.utils import console, logger, prompt_cloudflare_hitl, sleep_jitter


JS_FETCH_SCRIPT = """
async (url) => {
    try {
        const response = await window.fetch(url, {
            method: 'GET',
            headers: {
                'Accept': 'application/json, text/plain, */*',
                'X-Requested-With': 'XMLHttpRequest'
            }
        });
        const status = response.status;
        const text = await response.text();
        return {
            status: status,
            body: text,
            ok: response.ok
        };
    } catch (err) {
        return {
            status: 0,
            body: String(err),
            ok: false,
            error: true
        };
    }
}
"""


class IdxApiClient:
    def __init__(
        self,
        page: Page,
        api_config: ApiConfig,
        delays_config: DelaysConfig,
        network_config: NetworkConfig,
    ):
        self.page = page
        self.api_config = api_config
        self.delays = delays_config
        self.network = network_config
        self.detected_pagination_mode = api_config.pagination_mode

    def _build_url(self, index_from: int, page_size: int, year: int, period: str) -> str:
        params = {
            "indexFrom": str(index_from),
            "pageSize": str(page_size),
            "year": str(year),
            "reportType": self.api_config.report_type,
            "EmitenType": self.api_config.emiten_type,
            "periode": period,
        }
        query_string = urllib.parse.urlencode(params)
        return f"{self.api_config.base_url}?{query_string}"

    def fetch_raw(self, url: str) -> Dict[str, Any]:
        """
        Execute in-browser fetch with Cloudflare HITL detection and automatic retries.
        """
        retries = 0
        backoffs = self.network.backoff_seconds

        while True:
            try:
                res = self.page.evaluate(JS_FETCH_SCRIPT, url)
            except Exception as e:
                logger.warning(f"Error evaluating fetch in browser: {e}")
                res = {"status": 0, "body": str(e), "ok": False, "error": True}

            status = res.get("status", 0)
            body = res.get("body", "")

            is_cf_blocked = (
                status in (403, 429, 503)
                or "Just a moment..." in body
                or "cf-turnstile" in body
                or "<!DOCTYPE html" in body.lower()
                or "<html" in body.lower()
            )

            if is_cf_blocked:
                logger.warning(f"Cloudflare challenge terdeteksi (HTTP {status}) pada URL: {url}")
                prompt_cloudflare_hitl(
                    f"Cloudflare Challenge terdeteksi (HTTP {status}) saat mengakses API IDX!"
                )
                continue

            if res.get("ok") and status == 200:
                try:
                    data = json.loads(body)
                    return data
                except json.JSONDecodeError as err:
                    logger.warning(f"Response bukan JSON valid: {err}. Snippet: {body[:200]}")
                    prompt_cloudflare_hitl("Respons API IDX bukan format JSON yang valid!")
                    continue

            retries += 1
            if retries > self.network.max_retries:
                raise ConnectionError(
                    f"Gagal mengambil API IDX setelah {self.network.max_retries} percobaan. HTTP {status}: {body[:200]}"
                )

            delay_idx = min(retries - 1, len(backoffs) - 1)
            wait_time = backoffs[delay_idx]
            logger.info(f"Percobaan ke-{retries} gagal (HTTP {status}). Menunggu {wait_time}s sebelum mencoba lagi...")
            time.sleep(wait_time)

    def detect_pagination(self, sample_year: int = 2024, sample_period: str = "tw1") -> str:
        """
        Detect whether the API pagination uses 1-based page, 0-based page, or row offset.
        """
        if self.api_config.pagination_mode != "auto":
            self.detected_pagination_mode = self.api_config.pagination_mode
            logger.info(f"Menggunakan mode pagination terkonfigurasi: {self.detected_pagination_mode}")
            return self.detected_pagination_mode

        logger.info("Melakukan Pre-flight check skema pagination API IDX...")
        console.print("[cyan]Mendeteksi skema pagination API IDX...[/cyan]")

        url_p1 = self._build_url(index_from=1, page_size=2, year=sample_year, period=sample_period)
        url_p2 = self._build_url(index_from=2, page_size=2, year=sample_year, period=sample_period)
        url_p0 = self._build_url(index_from=0, page_size=2, year=sample_year, period=sample_period)

        data_p1 = self.fetch_raw(url_p1)
        data_p2 = self.fetch_raw(url_p2)
        data_p0 = self.fetch_raw(url_p0)

        results_p1 = data_p1.get("Results") or []
        results_p2 = data_p2.get("Results") or []
        results_p0 = data_p0.get("Results") or []

        ticker_p1 = [r.get("KodeEmiten") for r in results_p1]
        ticker_p2 = [r.get("KodeEmiten") for r in results_p2]
        ticker_p0 = [r.get("KodeEmiten") for r in results_p0]

        logger.debug(f"Pagination sample: p0={ticker_p0}, p1={ticker_p1}, p2={ticker_p2}")

        if ticker_p1 and ticker_p2 and ticker_p1 != ticker_p2:
            if ticker_p0 == ticker_p1:
                self.detected_pagination_mode = "page_1_based"
            elif ticker_p0 != ticker_p1 and bool(ticker_p0):
                self.detected_pagination_mode = "page_0_based"
            else:
                self.detected_pagination_mode = "page_1_based"
        else:
            # Equal pages at indexFrom=1 and indexFrom=2 indicate row-offset pagination.
            self.detected_pagination_mode = "offset"

        logger.info(f"Skema pagination terdeteksi: {self.detected_pagination_mode}")
        console.print(f"[green]Skema pagination terdeteksi:[/green] [bold]{self.detected_pagination_mode}[/bold]")
        return self.detected_pagination_mode

    def get_index_from(self, page_number: int, page_size: int) -> int:
        """
        Compute the indexFrom parameter value according to the detected pagination mode.
        page_number is 1-indexed (1, 2, 3...).
        """
        if self.detected_pagination_mode == "page_1_based":
            return page_number
        elif self.detected_pagination_mode == "page_0_based":
            return page_number - 1
        elif self.detected_pagination_mode == "offset":
            return (page_number - 1) * page_size
        else:
            return page_number

    def fetch_all_reports(
        self,
        year: int,
        period: str,
        ticker_filter: Optional[List[str]] = None,
        on_page_fetched: Optional[Callable[[int, int, int], None]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """
        Iterate through all pages for a given year and period, yielding each emiten record.
        Handles rate limit jitter and stops when all items are fetched or Results are empty.
        """
        page_number = 1
        total_items: Optional[int] = None
        processed_items = 0
        seen_tickers = set()

        ticker_filter_upper = set(t.strip().upper() for t in ticker_filter) if ticker_filter else None
        matched_tickers = set()

        while True:
            index_from = self.get_index_from(page_number, self.api_config.page_size)
            url = self._build_url(
                index_from=index_from,
                page_size=self.api_config.page_size,
                year=year,
                period=period,
            )

            logger.info(f"Mengambil API [{year}-{period}] Halaman {page_number} (indexFrom={index_from})...")
            data = self.fetch_raw(url)

            result_count = data.get("ResultCount", 0)
            if total_items is None:
                total_items = result_count
                logger.info(f"Total emiten tercatat untuk [{year}-{period}]: {total_items}")

            results = data.get("Results") or []
            if not results:
                logger.info(f"Hasil kosong pada halaman {page_number}. Selesai untuk [{year}-{period}].")
                break

            page_emiten_count = 0
            for item in results:
                ticker = (item.get("KodeEmiten") or "").strip().upper()
                if not ticker:
                    continue

                # Guard against infinite loops if pagination wraps around.
                if ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)
                page_emiten_count += 1
                processed_items += 1

                if ticker_filter_upper:
                    if ticker not in ticker_filter_upper:
                        continue
                    matched_tickers.add(ticker)

                yield item

            if on_page_fetched:
                on_page_fetched(page_number, processed_items, total_items or 0)

            if ticker_filter_upper and matched_tickers >= ticker_filter_upper:
                logger.info(f"Semua emiten yang difilter ({matched_tickers}) telah ditemukan. Selesai untuk [{year}-{period}].")
                break

            if total_items and processed_items >= total_items:
                logger.info(f"Semua emiten ({processed_items}/{total_items}) telah diproses untuk [{year}-{period}].")
                break

            if len(results) < self.api_config.page_size:
                logger.info(f"Halaman {page_number} mengembalikan {len(results)} item (< {self.api_config.page_size}). Selesai.")
                break

            page_number += 1

            sleep_jitter(self.delays.page_request_min, self.delays.page_request_max, "antar halaman API")
