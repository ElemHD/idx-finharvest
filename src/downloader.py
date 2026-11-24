from __future__ import annotations

import base64
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from playwright.sync_api import Page, APIResponse

from src.utils import logger, sanitize_filename

ZIP_MAGIC_BYTES = b"PK\x03\x04"
MIN_FILE_SIZE_BYTES = 1024


@dataclass
class DownloadResult:
    success: bool
    status: str  # "SUCCESS", "FAILED", "SKIPPED_CORRUPT"
    file_size_bytes: int
    local_path: str
    error_message: Optional[str] = None


JS_BLOB_FETCH_SCRIPT = """
async (url) => {
    try {
        const response = await window.fetch(url, {
            method: 'GET',
            credentials: 'include'
        });
        if (!response.ok) {
            return { ok: false, status: response.status, body: await response.text() };
        }
        const buffer = await response.arrayBuffer();
        const bytes = new Uint8Array(buffer);
        let binary = '';
        const chunkSize = 8192;
        for (let i = 0; i < bytes.length; i += chunkSize) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
        }
        return {
            ok: true,
            status: response.status,
            base64: btoa(binary),
            size: bytes.length
        };
    } catch (err) {
        return {
            ok: false,
            status: 0,
            error: String(err)
        };
    }
}
"""


def resolve_download_url(file_path: str, base_domain: str = "https://www.idx.co.id") -> str:
    """
    Construct safe, encoded full URL from API File_Path.
    Preserves slashes and query parameters while escaping spaces and specials.
    """
    file_path = file_path.strip()
    if file_path.startswith("http://") or file_path.startswith("https://"):
        full_url = file_path
    else:
        prefix = "" if file_path.startswith("/") else "/"
        full_url = f"{base_domain}{prefix}{file_path}"

    # Safely quote spaces and illegal characters without breaking protocol or slashes
    return urllib.parse.quote(full_url, safe=":/%&?=")


def build_target_filepath(
    download_dir: str,
    year: int,
    period_label: str,
    ticker: str,
    original_file_name: str,
) -> Path:
    """
    Generate structured destination path:
    ./data/raw_lk/{YEAR}/{PERIOD_LABEL}/{TICKER}_{YEAR}_{PERIOD_LABEL}_{ORIGINAL_FILE_NAME}.xlsx
    """
    sanitized_name = sanitize_filename(original_file_name)
    if not sanitized_name.lower().endswith(".xlsx"):
        sanitized_name = f"{sanitized_name}.xlsx"

    target_filename = f"{ticker}_{year}_{period_label}_{sanitized_name}"
    folder_path = Path(download_dir) / str(year) / period_label
    folder_path.mkdir(parents=True, exist_ok=True)
    return folder_path / target_filename


def validate_xlsx_bytes(content: bytes) -> Tuple[bool, str]:
    """
    Validate binary content:
    1. Size > 1024 bytes.
    2. Magic bytes starting with ZIP/OpenXML standard b'PK\\x03\\x04'.
    """
    if len(content) < MIN_FILE_SIZE_BYTES:
        return False, f"Ukuran file terlalu kecil ({len(content)} bytes < {MIN_FILE_SIZE_BYTES} bytes)"

    if not content.startswith(ZIP_MAGIC_BYTES):
        snippet = content[:200].lower()
        if b"<!doctype" in snippet or b"<html" in snippet or b"just a moment" in snippet:
            return False, "Menerima halaman HTML/Cloudflare challenge alih-alih file XLSX"
        return False, f"Magic bytes tidak sesuai. Diharapkan b'PK\\x03\\x04', ditemukan: {content[:4]!r}"

    return True, "Valid XLSX"


class BinaryDownloader:
    def __init__(self, page: Page, timeout_ms: int = 30000):
        self.page = page
        self.timeout_ms = timeout_ms

    def fetch_bytes(self, url: str) -> Tuple[Optional[bytes], Optional[str]]:
        """
        Download binary bytes using primary Playwright APIRequestContext,
        with automated fallback to in-browser JavaScript blob fetch.
        """
        try:
            resp: APIResponse = self.page.request.get(url, timeout=self.timeout_ms)
            if resp.ok:
                body = resp.body()
                if body.startswith(ZIP_MAGIC_BYTES):
                    return body, None
                logger.debug(f"page.request.get mengembalikan data tanpa magic bytes ZIP, mencoba fallback...")
            else:
                logger.debug(f"page.request.get gagal dengan status HTTP {resp.status}, mencoba fallback...")
        except Exception as e:
            logger.debug(f"page.request.get exception ({e}), mencoba fallback in-browser...")

        # Fallback to in-browser fetch when APIRequestContext is blocked by Cloudflare cookies.
        try:
            res = self.page.evaluate(JS_BLOB_FETCH_SCRIPT, url)
            if res.get("ok") and "base64" in res:
                content = base64.b64decode(res["base64"])
                return content, None
            status = res.get("status", 0)
            err = res.get("error") or res.get("body", "")
            return None, f"Fallback JS fetch gagal (HTTP {status}): {err[:200]}"
        except Exception as e:
            return None, f"Gagal mengunduh bytes via fallback: {e}"

    def download_and_validate(
        self,
        url: str,
        destination_path: Path,
    ) -> DownloadResult:
        """
        Download file, validate integrity (anti-corrupt), and save atomically via .tmp file.
        """
        dest_str = str(destination_path.resolve())
        temp_path = destination_path.with_suffix(".tmp")

        try:
            content, fetch_error = self.fetch_bytes(url)
            if content is None:
                return DownloadResult(
                    success=False,
                    status="FAILED",
                    file_size_bytes=0,
                    local_path=dest_str,
                    error_message=fetch_error or "Gagal mengambil file bytes",
                )

            is_valid, validation_msg = validate_xlsx_bytes(content)
            if not is_valid:
                logger.warning(f"Validasi file gagal untuk {url}: {validation_msg}")
                return DownloadResult(
                    success=False,
                    status="SKIPPED_CORRUPT",
                    file_size_bytes=len(content),
                    local_path=dest_str,
                    error_message=validation_msg,
                )

            # Write to .tmp and atomically replace so interrupted runs never leave partial .xlsx files.
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "wb") as f:
                f.write(content)

            os.replace(temp_path, destination_path)
            file_size = len(content)

            logger.info(f"Berhasil mengunduh dan memvalidasi file: {destination_path.name} ({file_size:,} bytes)")
            return DownloadResult(
                success=True,
                status="SUCCESS",
                file_size_bytes=file_size,
                local_path=dest_str,
            )

        except Exception as e:
            logger.error(f"Error saat mengunduh {url}: {e}", exc_info=True)
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            return DownloadResult(
                success=False,
                status="FAILED",
                file_size_bytes=0,
                local_path=dest_str,
                error_message=str(e),
            )
