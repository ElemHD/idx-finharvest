from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import List, Optional

# Ensure UTF-8 output encoding on Windows console
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from src.browser_manager import connect_browser, disconnect_browser, launch_chrome_process
from src.config_loader import AppConfig, load_config
from src.downloader import BinaryDownloader, build_target_filepath, resolve_download_url
from src.idx_client import IdxApiClient
from src.state_db import StateDB
from src.utils import logger, notify_user_alert, sanitize_filename, setup_logger, sleep_jitter

app = typer.Typer(
    help="IDX LK Harvester - Pengunduh Otomatis Laporan Keuangan Bursa Efek Indonesia (Excel .xlsx)",
    add_completion=False,
)
console = Console()


def format_bytes(size: float) -> str:
    """Format byte counts to human-readable string (KB, MB, GB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024.0:
            return f"{size:3.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


@app.command()
def launch_browser(
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="Path ke file konfigurasi YAML"),
):
    """
    Meluncurkan browser Google Chrome dengan port CDP (9222) untuk sesi IDX.
    """
    config = load_config(config_file)
    setup_logger(config.paths.log_file)

    console.print(
        Panel.fit(
            f"[bold cyan]IDX LK Harvester - Browser Launcher[/bold cyan]\n\n"
            f"Port CDP: [bold green]{config.cdp.port}[/bold green]\n"
            f"User Data Dir: [yellow]{config.cdp.user_data_dir}[/yellow]\n"
            f"URL: [blue]{config.api.portal_url}[/blue]",
            border_style="cyan",
        )
    )

    try:
        launch_chrome_process(config.cdp, target_url=config.api.portal_url)
        console.print(
            "\n[bold green][OK] Selesai![/bold green] Silakan verifikasi bahwa browser Chrome telah terbuka.\n"
            "[italic]Jika ada tantangan Cloudflare (Turnstile/Captcha), selesaikan di jendela Chrome tersebut.[/italic]\n"
            "Setelah itu, Anda dapat menjalankan perintah:\n"
            "  [bold cyan]python main.py harvest[/bold cyan]\n"
        )
    except Exception as e:
        console.print(f"[bold red][FAIL] Gagal meluncurkan browser:[/bold red] {e}")
        logger.error(f"Gagal meluncurkan browser: {e}", exc_info=True)
        raise typer.Exit(code=1)


@app.command()
def harvest(
    years: Optional[str] = typer.Option(
        None,
        "--years",
        "-y",
        help="Daftar tahun yang dipisahkan koma (contoh: 2022,2023,2024,2025,2026)",
    ),
    periods: Optional[str] = typer.Option(
        None,
        "--periods",
        "-p",
        help="Daftar periode yang dipisahkan koma (tw1,tw2,tw3,audit)",
    ),
    ticker: Optional[str] = typer.Option(
        None,
        "--ticker",
        "-t",
        help="Filter emiten tertentu (contoh: BBCA,BMRI) untuk pengujian",
    ),
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="Path ke file konfigurasi YAML"),
):
    """
    Menjalankan proses scraping & pengunduhan laporan keuangan Excel (.xlsx) dari IDX.
    """
    config = load_config(config_file)
    setup_logger(config.paths.log_file)
    db = StateDB(config.paths.db_path)

    target_years: List[int] = (
        [int(y.strip()) for y in years.split(",") if y.strip().isdigit()]
        if years
        else config.years
    )
    target_periods: List[str] = (
        [p.strip().lower() for p in periods.split(",") if p.strip()]
        if periods
        else config.periods
    )
    ticker_filter: Optional[List[str]] = (
        [t.strip().upper() for t in ticker.split(",") if t.strip()]
        if ticker
        else None
    )

    console.print(
        Panel.fit(
            f"[bold cyan]IDX LK Harvester - Memulai Proses Unduhan[/bold cyan]\n\n"
            f"Tahun Target   : [bold yellow]{target_years}[/bold yellow]\n"
            f"Periode Target : [bold yellow]{target_periods}[/bold yellow]\n"
            f"Filter Emiten  : [bold green]{ticker_filter or 'SEMUA EMITEN'}[/bold green]\n"
            f"Output Folder  : [italic]{config.paths.download_dir}[/italic]\n"
            f"Database State : [italic]{config.paths.db_path}[/italic]",
            border_style="cyan",
        )
    )

    try:
        pw, browser, context, page = connect_browser(config.cdp, target_url=config.api.portal_url)
    except Exception as e:
        console.print(f"\n[bold red][FAIL] Gagal terhubung ke browser Chrome:[/bold red] {e}")
        console.print(
            "[yellow]Petunjuk:[/yellow] Jalankan terlebih dahulu: [bold cyan]python main.py launch-browser[/bold cyan]"
        )
        raise typer.Exit(code=1)

    api_client = IdxApiClient(page, config.api, config.delays, config.network)
    downloader = BinaryDownloader(page, timeout_ms=config.network.timeout_ms)

    try:
        api_client.detect_pagination(sample_year=target_years[0], sample_period=target_periods[0])
    except Exception as e:
        logger.warning(f"Pre-flight pagination check gagal: {e}. Menggunakan mode default.")

    total_downloaded = 0
    total_skipped = 0
    total_failed = 0
    total_missing_xlsx = 0

    interrupted = False

    def handle_sigint(sig, frame):
        nonlocal interrupted
        interrupted = True
        console.print("\n[bold yellow][!] Sinyal pembatalan (Ctrl+C) diterima. Menyelesaikan operasi terakhir...[/bold yellow]")

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        for year in target_years:
            if interrupted:
                break
            for period in target_periods:
                if interrupted:
                    break

                period_label = config.period_mapping.get(period, period.upper())
                console.print(
                    f"\n[bold blue]=== Memproses [{year} - {period.upper()} (Folder: {period_label})] ===[/bold blue]"
                )

                try:
                    report_iterator = api_client.fetch_all_reports(
                        year=year,
                        period=period,
                        ticker_filter=ticker_filter,
                    )
                except Exception as e:
                    console.print(f"[bold red][FAIL] Error saat memanggil API untuk [{year}-{period}]:[/bold red] {e}")
                    logger.error(f"Error fetch_all_reports [{year}-{period}]: {e}", exc_info=True)
                    continue

                for emiten in report_iterator:
                    if interrupted:
                        break

                    emiten_code = (emiten.get("KodeEmiten") or "").strip().upper()
                    emiten_name = (emiten.get("NamaEmiten") or "").strip()
                    attachments = emiten.get("Attachments") or []

                    xlsx_attachments = []
                    for att in attachments:
                        fn = (att.get("File_Name") or "").strip()
                        ft = (att.get("File_Type") or "").strip().lower()
                        if "financialstatement" in fn.lower() and (fn.lower().endswith(".xlsx") or ft == ".xlsx"):
                            xlsx_attachments.append(att)

                    if not xlsx_attachments:
                        att_summary = "; ".join(
                            f"{a.get('File_Name', '')} ({a.get('File_Type', '')})" for a in attachments
                        ) or "No attachments found"
                        db.record_missing_xlsx(emiten_code, emiten_name, year, period, att_summary)
                        total_missing_xlsx += 1
                        logger.debug(f"Emiten {emiten_code} [{year}-{period}] tidak memiliki lampiran XLSX.")
                        continue

                    for att in xlsx_attachments:
                        if interrupted:
                            break

                        orig_file_name = att.get("File_Name") or f"FinancialStatement-{year}-{period}-{emiten_code}.xlsx"
                        file_path_raw = att.get("File_Path") or ""
                        if not file_path_raw:
                            continue

                        dest_file = build_target_filepath(
                            config.paths.download_dir,
                            year,
                            period_label,
                            emiten_code,
                            orig_file_name,
                        )

                        if db.is_already_downloaded(emiten_code, year, period, orig_file_name, str(dest_file)):
                            logger.debug(f"Skip duplikat (sudah ada): {dest_file.name}")
                            total_skipped += 1
                            continue

                        full_url = resolve_download_url(file_path_raw)
                        console.print(
                            f"  [cyan]-> Mengunduh:[/cyan] [bold]{emiten_code}[/bold] - [dim]{dest_file.name}[/dim]"
                        )

                        dl_res = downloader.download_and_validate(full_url, dest_file)

                        db.record_download(
                            ticker=emiten_code,
                            emiten_name=emiten_name,
                            year=year,
                            period=period,
                            file_name=orig_file_name,
                            file_url=full_url,
                            local_path=dl_res.local_path,
                            file_size_bytes=dl_res.file_size_bytes,
                            status=dl_res.status,
                            error_message=dl_res.error_message,
                        )

                        if dl_res.success:
                            total_downloaded += 1
                            console.print(
                                f"    [bold green][OK] Berhasil:[/bold green] {format_bytes(dl_res.file_size_bytes)}"
                            )
                        else:
                            total_failed += 1
                            console.print(
                                f"    [bold red][FAIL] Gagal ({dl_res.status}):[/bold red] {dl_res.error_message}"
                            )

                        sleep_jitter(
                            config.delays.file_download_min,
                            config.delays.file_download_max,
                            "antara unduhan file",
                        )

    finally:
        disconnect_browser(pw, browser)

    console.print("\n" + "=" * 70, style="bold cyan")
    console.print("[bold green]Proses Pengunduhan Selesai / Terhenti![/bold green]")
    console.print(f"  * Total Berhasil Diunduh : [bold green]{total_downloaded:,}[/bold green]")
    console.print(f"  * Total Dilewati (Ada)   : [bold cyan]{total_skipped:,}[/bold cyan]")
    console.print(f"  * Total Gagal            : [bold red]{total_failed:,}[/bold red]")
    console.print(f"  * Emiten Tanpa XLSX      : [bold yellow]{total_missing_xlsx:,}[/bold yellow]")
    console.print("=" * 70 + "\n", style="bold cyan")


@app.command()
def retry_failed(
    ticker: Optional[str] = typer.Option(None, "--ticker", "-t", help="Filter emiten tertentu"),
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="Path ke file konfigurasi YAML"),
):
    """
    Mengulang proses unduhan khusus untuk file yang berstatus FAILED di database.
    """
    config = load_config(config_file)
    setup_logger(config.paths.log_file)
    db = StateDB(config.paths.db_path)

    failed_list = db.get_failed_downloads(ticker)
    if not failed_list:
        console.print("[bold green]Tidak ada file berstatus FAILED di database. Semua bersih![/bold green]")
        return

    console.print(f"[yellow]Ditemukan {len(failed_list)} file berstatus FAILED untuk diulang.[/yellow]")

    try:
        pw, browser, context, page = connect_browser(config.cdp, target_url=config.api.portal_url)
    except Exception as e:
        console.print(f"[bold red]✗ Gagal terhubung ke Chrome CDP:[/bold red] {e}")
        raise typer.Exit(code=1)

    downloader = BinaryDownloader(page, timeout_ms=config.network.timeout_ms)
    repaired_count = 0
    still_failed_count = 0

    try:
        for row in failed_list:
            t = row["ticker"]
            y = row["year"]
            p = row["period"]
            fn = row["file_name"]
            url = row["file_url"]
            local_path = Path(row["local_path"])

            console.print(f"[cyan]Retry:[/cyan] {t} [{y}-{p}] - {fn}")
            dl_res = downloader.download_and_validate(url, local_path)

            db.record_download(
                ticker=t,
                emiten_name=row["emiten_name"],
                year=y,
                period=p,
                file_name=fn,
                file_url=url,
                local_path=dl_res.local_path,
                file_size_bytes=dl_res.file_size_bytes,
                status=dl_res.status,
                error_message=dl_res.error_message,
                retry_count=1,
            )

            if dl_res.success:
                repaired_count += 1
                console.print(f"  [bold green][OK] Berhasil diperbaiki:[/bold green] {format_bytes(dl_res.file_size_bytes)}")
            else:
                still_failed_count += 1
                console.print(f"  [bold red][FAIL] Masih gagal:[/bold red] {dl_res.error_message}")

            sleep_jitter(config.delays.file_download_min, config.delays.file_download_max, "retry delay")

    finally:
        disconnect_browser(pw, browser)

    console.print(
        f"\n[bold]Hasil Retry:[/bold] Berhasil: [green]{repaired_count}[/green], Tetap Gagal: [red]{still_failed_count}[/red]"
    )


@app.command()
def status(
    config_file: str = typer.Option("config.yaml", "--config", "-c", help="Path ke file konfigurasi YAML"),
    export_csv: bool = typer.Option(True, "--export-csv/--no-export-csv", help="Ekspor laporan audit ke CSV"),
):
    """
    Menampilkan ringkasan progres dan audit laporan keuangan di terminal.
    """
    config = load_config(config_file)
    db = StateDB(config.paths.db_path)
    summary = db.get_status_summary()

    status_counts = summary["status_counts"]
    total_bytes = summary["total_bytes"]
    missing_count = summary["missing_count"]
    breakdown = summary["breakdown"]

    sum_table = Table(title="Ringkasan Status Unduhan IDX LK", border_style="cyan")
    sum_table.add_column("Metrik", style="bold")
    sum_table.add_column("Nilai", style="bold green")

    sum_table.add_row("Total File Sukses (SUCCESS)", f"{status_counts.get('SUCCESS', 0):,}")
    sum_table.add_row("Total File Gagal (FAILED)", f"{status_counts.get('FAILED', 0):,}")
    sum_table.add_row("Total Corrupt (SKIPPED_CORRUPT)", f"{status_counts.get('SKIPPED_CORRUPT', 0):,}")
    sum_table.add_row("Emiten Tanpa Lampiran XLSX", f"{missing_count:,}")
    sum_table.add_row("Total Ukuran File di Disk", format_bytes(total_bytes))

    console.print(sum_table)

    if breakdown:
        bd_table = Table(title="Rincian per Tahun & Periode", border_style="blue")
        bd_table.add_column("Tahun", justify="center")
        bd_table.add_column("Periode", justify="center")
        bd_table.add_column("Sukses", justify="right", style="green")
        bd_table.add_column("Gagal", justify="right", style="red")
        bd_table.add_column("Corrupt", justify="right", style="yellow")
        bd_table.add_column("Total Ukuran", justify="right")

        for b in breakdown:
            bd_table.add_row(
                str(b["year"]),
                str(b["period"]).upper(),
                str(b["success_count"]),
                str(b["failed_count"]),
                str(b["corrupt_count"]),
                format_bytes(b["total_size"] or 0),
            )

        console.print(bd_table)

    if export_csv:
        csv_path = db.export_audit_csv(config.paths.audit_report)
        console.print(f"[green][OK] Laporan audit diekspor ke:[/green] [bold]{csv_path}[/bold]")


if __name__ == "__main__":
    app()
