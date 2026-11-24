from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Literal, Optional
import yaml
from pydantic import BaseModel, Field


class ApiConfig(BaseModel):
    base_url: str = "https://www.idx.co.id/primary/ListedCompany/GetFinancialReport"
    portal_url: str = "https://www.idx.co.id/id/perusahaan-tercatat/laporan-keuangan-dan-tahunan"
    report_type: str = "rdf"
    emiten_type: str = "s"
    page_size: int = 50
    pagination_mode: Literal["auto", "page_1_based", "page_0_based", "offset"] = "auto"


class CdpConfig(BaseModel):
    host: str = "localhost"
    port: int = 9222
    user_data_dir: str = "./chrome_profile"
    chrome_path: str = ""

    @property
    def cdp_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class DelaysConfig(BaseModel):
    file_download_min: float = 1.5
    file_download_max: float = 3.5
    page_request_min: float = 2.0
    page_request_max: float = 4.5


class PathsConfig(BaseModel):
    download_dir: str = "./data/raw_lk"
    db_path: str = "./logs/state.db"
    log_file: str = "./logs/scraper.log"
    audit_report: str = "./logs/audit_report.csv"


class NetworkConfig(BaseModel):
    max_retries: int = 3
    backoff_seconds: List[int] = Field(default_factory=lambda: [3, 6, 12])
    timeout_ms: int = 30000


class AppConfig(BaseModel):
    years: List[int] = Field(default_factory=lambda: [2022, 2023, 2024, 2025, 2026])
    periods: List[str] = Field(default_factory=lambda: ["tw1", "tw2", "tw3", "audit"])
    period_mapping: Dict[str, str] = Field(
        default_factory=lambda: {
            "tw1": "TW1",
            "tw2": "TW2",
            "tw3": "TW3",
            "audit": "FY",
        }
    )
    api: ApiConfig = Field(default_factory=ApiConfig)
    cdp: CdpConfig = Field(default_factory=CdpConfig)
    delays: DelaysConfig = Field(default_factory=DelaysConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    def ensure_directories(self) -> None:
        """Ensure all required output and log directories exist."""
        Path(self.paths.download_dir).mkdir(parents=True, exist_ok=True)
        Path(self.paths.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.paths.log_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.paths.audit_report).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cdp.user_data_dir).mkdir(parents=True, exist_ok=True)


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """Load and validate configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        config = AppConfig()
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        config = AppConfig(**data)

    config.ensure_directories()
    return config
