import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.config_loader import AppConfig, load_config
from src.downloader import (
    MIN_FILE_SIZE_BYTES,
    ZIP_MAGIC_BYTES,
    build_target_filepath,
    resolve_download_url,
    validate_xlsx_bytes,
)
from src.idx_client import IdxApiClient
from src.state_db import StateDB
from src.utils import sanitize_filename


class TestConfigLoader(unittest.TestCase):
    def test_default_config(self):
        config = load_config("config.yaml")
        self.assertIn(2022, config.years)
        self.assertIn(2026, config.years)
        self.assertIn("tw1", config.periods)
        self.assertEqual(config.period_mapping["audit"], "FY")
        self.assertEqual(config.cdp.port, 9222)


class TestUtils(unittest.TestCase):
    def test_sanitize_filename(self):
        dirty = 'FinancialStatement:2024*TW1?BBCA"test<file>.xlsx'
        clean = sanitize_filename(dirty)
        self.assertNotIn(":", clean)
        self.assertNotIn("*", clean)
        self.assertNotIn("?", clean)
        self.assertNotIn('"', clean)
        self.assertNotIn("<", clean)
        self.assertNotIn(">", clean)
        self.assertTrue(clean.endswith(".xlsx"))

    def test_reserved_filename(self):
        clean = sanitize_filename("CON.xlsx")
        self.assertEqual(clean, "_CON.xlsx")


class TestDownloaderValidation(unittest.TestCase):
    def test_valid_xlsx_bytes(self):
        mock_xlsx = ZIP_MAGIC_BYTES + b"x" * 2000
        valid, msg = validate_xlsx_bytes(mock_xlsx)
        self.assertTrue(valid)
        self.assertEqual(msg, "Valid XLSX")

    def test_file_too_small(self):
        mock_small = ZIP_MAGIC_BYTES + b"x" * 10
        valid, msg = validate_xlsx_bytes(mock_small)
        self.assertFalse(valid)
        self.assertIn("terlalu kecil", msg)

    def test_html_challenge_rejected(self):
        mock_html = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>Cloudflare</body></html>" + b" " * 1000
        valid, msg = validate_xlsx_bytes(mock_html)
        self.assertFalse(valid)
        self.assertIn("HTML/Cloudflare", msg)

    def test_resolve_download_url(self):
        rel_path = "/Portals/0/StaticData/FinancialStatement 2024 TW1 BBCA.xlsx"
        url = resolve_download_url(rel_path)
        self.assertTrue(url.startswith("https://www.idx.co.id/"))
        self.assertIn("%20", url)

        abs_url = "https://static.idx.co.id/files/BBCA.xlsx"
        self.assertEqual(resolve_download_url(abs_url), abs_url)

    def test_build_target_filepath(self):
        path = build_target_filepath("./test_data", 2024, "TW1", "BBCA", "FinancialStatement-2024-I-BBCA.xlsx")
        self.assertTrue(str(path).endswith("BBCA_2024_TW1_FinancialStatement-2024-I-BBCA.xlsx"))
        self.assertIn("2024", str(path))
        self.assertIn("TW1", str(path))


class TestStateDB(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_state.db")
        self.db = StateDB(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_and_check_download(self):
        test_file = os.path.join(self.test_dir, "BBCA.xlsx")
        with open(test_file, "wb") as f:
            f.write(ZIP_MAGIC_BYTES + b"dummycontent" * 100)

        self.db.record_download(
            ticker="BBCA",
            emiten_name="Bank Central Asia Tbk",
            year=2024,
            period="tw1",
            file_name="BBCA.xlsx",
            file_url="https://idx.co.id/BBCA.xlsx",
            local_path=test_file,
            file_size_bytes=os.path.getsize(test_file),
            status="SUCCESS",
        )

        self.assertTrue(self.db.is_already_downloaded("BBCA", 2024, "tw1", "BBCA.xlsx", test_file))
        self.assertFalse(self.db.is_already_downloaded("BMRI", 2024, "tw1", "BMRI.xlsx"))

    def test_missing_xlsx_recording(self):
        self.db.record_missing_xlsx("GOTO", "GoTo Gojek Tokopedia Tbk", 2024, "tw1", "Only PDF available")
        summary = self.db.get_status_summary()
        self.assertEqual(summary["missing_count"], 1)

    def test_failed_and_retry_list(self):
        self.db.record_download(
            ticker="TLKM",
            emiten_name="Telkom Indonesia Tbk",
            year=2023,
            period="audit",
            file_name="TLKM.xlsx",
            file_url="https://idx.co.id/TLKM.xlsx",
            local_path="/fake/path/TLKM.xlsx",
            file_size_bytes=0,
            status="FAILED",
            error_message="Network Timeout",
        )

        failed = self.db.get_failed_downloads()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["ticker"], "TLKM")
        self.assertEqual(failed[0]["error_message"], "Network Timeout")


if __name__ == "__main__":
    unittest.main()
