from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class StateDB:
    def __init__(self, db_path: str = "./logs/state.db"):
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def init_db(self) -> None:
        """Initialize database schema with tables and indexes."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    emiten_name TEXT,
                    year INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_url TEXT,
                    local_path TEXT,
                    file_size_bytes INTEGER DEFAULT 0,
                    status TEXT NOT NULL, -- 'SUCCESS', 'FAILED', 'SKIPPED_CORRUPT'
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_ticker_period_file UNIQUE (ticker, year, period, file_name)
                );
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS missing_xlsx_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    emiten_name TEXT,
                    year INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    attachments_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_missing UNIQUE (ticker, year, period)
                );
                """
            )

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads (status);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_year_period ON downloads (year, period);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_ticker ON downloads (ticker);")
            conn.commit()

    def is_already_downloaded(
        self, ticker: str, year: int, period: str, file_name: str, local_path: Optional[str] = None
    ) -> bool:
        """
        Check if file is already successfully downloaded.
        Verifies both SQLite record and physical file existence on disk with size > 0.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT local_path, status, file_size_bytes FROM downloads
                WHERE ticker = ? AND year = ? AND period = ? AND file_name = ?
                """,
                (ticker, year, period, file_name),
            )
            row = cursor.fetchone()
            if not row:
                return False

            if row["status"] != "SUCCESS":
                return False

            target_path = local_path or row["local_path"]
            if not target_path or not os.path.isfile(target_path):
                return False

            if os.path.getsize(target_path) <= 0:
                return False

            return True

    def record_download(
        self,
        ticker: str,
        emiten_name: str,
        year: int,
        period: str,
        file_name: str,
        file_url: str,
        local_path: str,
        file_size_bytes: int,
        status: str,
        error_message: Optional[str] = None,
        retry_count: int = 0,
    ) -> None:
        """Insert or update download state record."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT INTO downloads (
                    ticker, emiten_name, year, period, file_name,
                    file_url, local_path, file_size_bytes, status,
                    error_message, retry_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, year, period, file_name) DO UPDATE SET
                    emiten_name = excluded.emiten_name,
                    file_url = excluded.file_url,
                    local_path = excluded.local_path,
                    file_size_bytes = excluded.file_size_bytes,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    retry_count = downloads.retry_count + excluded.retry_count,
                    updated_at = excluded.updated_at
                """,
                (
                    ticker,
                    emiten_name,
                    year,
                    period,
                    file_name,
                    file_url,
                    local_path,
                    file_size_bytes,
                    status,
                    error_message,
                    retry_count,
                    now,
                ),
            )
            conn.commit()

    def record_missing_xlsx(
        self,
        ticker: str,
        emiten_name: str,
        year: int,
        period: str,
        attachments_summary: str,
    ) -> None:
        """Record emiten that submitted a report without an XLSX attachment."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT INTO missing_xlsx_reports (
                    ticker, emiten_name, year, period, attachments_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, year, period) DO UPDATE SET
                    emiten_name = excluded.emiten_name,
                    attachments_summary = excluded.attachments_summary,
                    created_at = excluded.created_at
                """,
                (ticker, emiten_name, year, period, attachments_summary, now),
            )
            conn.commit()

    def get_failed_downloads(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve all downloads currently marked as FAILED."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if ticker:
                cursor.execute(
                    "SELECT * FROM downloads WHERE status = 'FAILED' AND ticker = ? ORDER BY year, period, ticker",
                    (ticker,),
                )
            else:
                cursor.execute(
                    "SELECT * FROM downloads WHERE status = 'FAILED' ORDER BY year, period, ticker"
                )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_status_summary(self) -> Dict[str, Any]:
        """Aggregate download counts, failure reasons, and disk storage metrics."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT status, COUNT(*) as count FROM downloads GROUP BY status")
            status_counts = {row["status"]: row["count"] for row in cursor.fetchall()}

            cursor.execute("SELECT SUM(file_size_bytes) as total_bytes FROM downloads WHERE status = 'SUCCESS'")
            row = cursor.fetchone()
            total_bytes = row["total_bytes"] or 0

            cursor.execute("SELECT COUNT(*) as count FROM missing_xlsx_reports")
            missing_count = cursor.fetchone()["count"]

            cursor.execute(
                """
                SELECT year, period,
                       SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) as success_count,
                       SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as failed_count,
                       SUM(CASE WHEN status = 'SKIPPED_CORRUPT' THEN 1 ELSE 0 END) as corrupt_count,
                       SUM(CASE WHEN status = 'SUCCESS' THEN file_size_bytes ELSE 0 END) as total_size
                FROM downloads
                GROUP BY year, period
                ORDER BY year ASC, period ASC
                """
            )
            breakdown = [dict(row) for row in cursor.fetchall()]

            cursor.execute(
                """
                SELECT year, period, COUNT(*) as count
                FROM missing_xlsx_reports
                GROUP BY year, period
                ORDER BY year ASC, period ASC
                """
            )
            missing_breakdown = [dict(row) for row in cursor.fetchall()]

            return {
                "status_counts": status_counts,
                "total_bytes": total_bytes,
                "missing_count": missing_count,
                "breakdown": breakdown,
                "missing_breakdown": missing_breakdown,
            }

    def export_audit_csv(self, output_path: str = "./logs/audit_report.csv") -> str:
        """Export full download history to a CSV file for auditing."""
        out_file = Path(output_path).resolve()
        out_file.parent.mkdir(parents=True, exist_ok=True)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT ticker, emiten_name, year, period, file_name, file_url,
                       local_path, file_size_bytes, status, error_message, retry_count, updated_at
                FROM downloads
                ORDER BY year ASC, period ASC, ticker ASC
                """
            )
            rows = cursor.fetchall()

            with open(out_file, "w", newline="", encoding="utf-8") as csvfile:
                fieldnames = [
                    "ticker",
                    "emiten_name",
                    "year",
                    "period",
                    "file_name",
                    "file_url",
                    "local_path",
                    "file_size_bytes",
                    "status",
                    "error_message",
                    "retry_count",
                    "updated_at",
                ]
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row))

        return str(out_file)
