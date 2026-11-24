from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from src.config_loader import CdpConfig
from src.utils import console, logger


def find_chrome_executable(configured_path: str = "") -> str:
    """Detect Google Chrome binary path across Windows, macOS, and Linux."""
    if configured_path and Path(configured_path).is_file():
        return str(Path(configured_path).resolve())

    for name in ["chrome", "chrome.exe", "google-chrome", "google-chrome-stable", "chromium"]:
        found = shutil.which(name)
        if found and Path(found).is_file():
            return found

    candidates = []
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", r"C:\Users\%USERNAME%\AppData\Local")
        prog_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        prog_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates = [
            os.path.join(prog_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(prog_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]

    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    raise FileNotFoundError(
        "Google Chrome executable tidak ditemukan di path standar. "
        "Harap tentukan path di `config.yaml` pada bagian `cdp.chrome_path`."
    )


def is_port_listening(host: str, port: int) -> bool:
    """Check if a network port is open and accepting TCP connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect((host, port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


def launch_chrome_process(
    cdp_config: CdpConfig,
    target_url: str = "https://www.idx.co.id/id/perusahaan-tercatat/laporan-keuangan-dan-tahunan",
) -> None:
    """Launch local Google Chrome process configured with CDP remote debugging port."""
    if is_port_listening(cdp_config.host, cdp_config.port):
        logger.info(f"Port CDP {cdp_config.port} sudah aktif. Menggunakan sesi browser yang ada.")
        console.print(f"[green]Port CDP {cdp_config.port} sudah aktif.[/green] Menggunakan sesi browser yang ada.")
        return

    chrome_bin = find_chrome_executable(cdp_config.chrome_path)
    user_data_path = str(Path(cdp_config.user_data_dir).resolve())
    Path(user_data_path).mkdir(parents=True, exist_ok=True)

    cmd = [
        chrome_bin,
        f"--remote-debugging-port={cdp_config.port}",
        f"--user-data-dir={user_data_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        target_url,
    ]

    logger.info(f"Meluncurkan Chrome: {' '.join(cmd)}")
    console.print(f"[cyan]Meluncurkan Google Chrome pada port CDP {cdp_config.port}...[/cyan]")

    # Detach Chrome process so it stays open after the CLI exits.
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )

    max_wait = 15.0
    start_time = time.time()
    while time.time() - start_time < max_wait:
        if is_port_listening(cdp_config.host, cdp_config.port):
            logger.info("Chrome CDP port berhasil terdeteksi dan siap menerima koneksi.")
            console.print("[bold green]Browser Chrome berhasil diluncurkan dan port CDP siap![/bold green]")
            return
        time.sleep(0.5)

    raise TimeoutError(f"Timeout menunggu Chrome membuka port CDP {cdp_config.port}.")


def connect_browser(
    cdp_config: CdpConfig,
    target_url: str = "https://www.idx.co.id/id/perusahaan-tercatat/laporan-keuangan-dan-tahunan",
) -> Tuple[Playwright, Browser, BrowserContext, Page]:
    """
    Connect Playwright to the active Chrome CDP session and return the active IDX page.
    If no IDX page is open, navigates the current page to the target URL.
    """
    if not is_port_listening(cdp_config.host, cdp_config.port):
        logger.info(f"Port {cdp_config.port} belum aktif, mencoba meluncurkan Chrome otomatis...")
        launch_chrome_process(cdp_config, target_url)

    pw = sync_playwright().start()
    cdp_endpoint = cdp_config.cdp_url
    logger.info(f"Menghubungkan Playwright ke CDP: {cdp_endpoint}")

    try:
        browser = pw.chromium.connect_over_cdp(cdp_endpoint)
    except Exception as e:
        pw.stop()
        raise ConnectionError(
            f"Gagal menghubungkan Playwright ke {cdp_endpoint}. "
            "Pastikan Google Chrome telah dibuka dengan opsi --remote-debugging-port=9222. "
            f"Detail error: {e}"
        )

    context = browser.contexts[0] if browser.contexts else browser.new_context()
    pages = context.pages

    target_page: Optional[Page] = None
    for p in pages:
        if "idx.co.id" in p.url:
            target_page = p
            break

    if target_page is None:
        target_page = pages[0] if pages else context.new_page()
        logger.info(f"Membuka halaman target IDX: {target_url}")
        target_page.goto(target_url, wait_until="domcontentloaded")
    else:
        logger.info(f"Menemukan tab IDX yang aktif: {target_page.url}")

    return pw, browser, context, target_page


def disconnect_browser(pw: Playwright, browser: Browser) -> None:
    """Disconnect Playwright from CDP session without closing the user's browser."""
    try:
        browser.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
