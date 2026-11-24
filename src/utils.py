from __future__ import annotations

import logging
import os
import random
import re
import sys
import time
from pathlib import Path

# Fix Windows console UTF-8 encoding for unicode characters
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from rich.console import Console

console = Console(highlight=False)
logger = logging.getLogger("idx_harvester")


def setup_logger(log_file: str = "./logs/scraper.log", level: int = logging.INFO) -> logging.Logger:
    """Set up file and stream logging for the application."""
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    
    logger.setLevel(level)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename for Windows, macOS, and Linux.
    Removes invalid characters: <>:"/\\|?* and ASCII control codes.
    """
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename)
    # Windows rejects trailing dots and spaces in path segments.
    clean = clean.strip(" .")
    # Prefix Windows DOS device names that cannot be created as regular files.
    base = clean.split(".")[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}
    if base in reserved:
        clean = f"_{clean}"
    return clean or "unnamed_file"


def sleep_jitter(min_seconds: float, max_seconds: float, reason: str = "") -> None:
    """Sleep for a random duration between min and max seconds to simulate human pacing."""
    if max_seconds < min_seconds:
        max_seconds = min_seconds
    delay = random.uniform(min_seconds, max_seconds)
    if reason:
        logger.debug(f"Pacing delay ({reason}): {delay:.2f}s")
    time.sleep(delay)


def notify_user_alert() -> None:
    """Trigger terminal bell and OS-specific alert sound."""
    sys.stdout.write("\a")
    sys.stdout.flush()

    if sys.platform == "win32":
        try:
            import winsound
            winsound.Beep(1000, 400)
            time.sleep(0.1)
            winsound.Beep(1500, 500)
        except Exception:
            pass


def prompt_cloudflare_hitl(message: str = "Cloudflare Challenge terdeteksi!") -> None:
    """Prompt human user to solve Cloudflare challenge and wait for confirmation."""
    notify_user_alert()
    console.print("\n" + "=" * 75, style="bold red")
    console.print(f"[!] PERHATIAN: {message}", style="bold red on yellow")
    console.print(
        "    1. Buka jendela browser Chrome yang sedang berjalan.\n"
        "    2. Selesaikan tantangan Turnstile / Captcha Cloudflare jika muncul.\n"
        "    3. Pastikan halaman IDX menampilkan data dengan normal.\n"
        "    4. Kembali ke terminal ini dan tekan ENTER untuk melanjutkan...",
        style="yellow"
    )
    console.print("=" * 75 + "\n", style="bold red")
    
    try:
        input("Tekan [ENTER] setelah verifikasi selesai...")
    except (KeyboardInterrupt, EOFError):
        console.print("\n[!] Pembatalan diterima oleh pengguna.", style="bold yellow")
        raise
