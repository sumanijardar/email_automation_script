import asyncio
import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List

import aiohttp
from tqdm.asyncio import tqdm

from src.config import config
from src.database import Database
from src.dvr_client import DVRClient
from src.models import DVR, SetupResult, SetupStatus


def setup_logging():
    log_conf = config.get("logging")
    log_file = Path(log_conf["file"])
    log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            log_file,
            maxBytes=log_conf.get("rotate_size_mb", 10) * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ]
    logging.basicConfig(
        level=log_conf.get("level", "INFO"),
        format=fmt,
        handlers=handlers,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


logger = logging.getLogger("main")


import colorama
from colorama import Fore, Style

colorama.init(autoreset=True)


def get_status_colored(status_str: str) -> str:
    if status_str == "SUCCESS":
        return f"{Fore.GREEN}{Style.BRIGHT}[SUCCESS]{Style.RESET_ALL}"
    elif status_str == "SUCCESS_SAVED_ONLY":
        return f"{Fore.YELLOW}{Style.BRIGHT}[SUCCESS_SAVED_ONLY]{Style.RESET_ALL}"
    elif status_str == "FAILED":
        return f"{Fore.RED}{Style.BRIGHT}[FAILED]{Style.RESET_ALL}"
    return f"{Fore.WHITE}[{status_str}]{Style.RESET_ALL}"


async def process_dvrs(dvrs: List[DVR]) -> List[SetupResult]:
    max_concurrent = config.get("execution", "max_concurrent", default=25)
    semaphore = asyncio.Semaphore(max_concurrent)
    connector = aiohttp.TCPConnector(limit=max_concurrent * 2, ssl=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        client = DVRClient(session)

        async def bounded(dvr: DVR) -> SetupResult:
            async with semaphore:
                try:
                    result = await client.configure(dvr)
                    status_badge = get_status_colored(result.status.value)
                    logger.info(
                        f"{status_badge} {Fore.CYAN}{dvr.ip}{Style.RESET_ALL} "
                        f"({Fore.MAGENTA}{result.model}{Style.RESET_ALL}) - {result.duration_ms}ms"
                    )
                except Exception as e:
                    logger.error(f"{Fore.RED}[EXCEPTION]{Style.RESET_ALL} {dvr.ip}: {e}")
                    from datetime import datetime
                    result = SetupResult(
                        atmid=dvr.atmid, ip=dvr.ip, port=dvr.port,
                        model="Unknown", status=SetupStatus.FAILED,
                        remark=f"Unhandled: {str(e)[:100]}",
                        duration_ms=0, timestamp=datetime.now(),
                    )
                return result

        tasks = [bounded(dvr) for dvr in dvrs]
        results = await tqdm.gather(*tasks, desc=f"{Fore.BLUE}Configuring DVRs{Style.RESET_ALL}")
    return results


def print_summary(results: List[SetupResult], elapsed: float):
    total = len(results)
    if total == 0:
        return
    success = sum(1 for r in results if r.status == SetupStatus.SUCCESS)
    partial = sum(1 for r in results if r.status == SetupStatus.SAVED_ONLY)
    failed = sum(1 for r in results if r.status == SetupStatus.FAILED)
    avg_ms = sum(r.duration_ms for r in results) / total

    print("\n" + Fore.CYAN + "=" * 60)
    print(f"  {Style.BRIGHT}EXECUTION SUMMARY{Style.NORMAL}")
    print(Fore.CYAN + "=" * 60)
    print(f"  Total time:      {Fore.WHITE}{elapsed:.2f}s")
    print(f"  Avg per DVR:     {Fore.WHITE}{avg_ms:.0f}ms")
    print(f"  Total DVRs:      {Fore.WHITE}{total}")
    print(f"  {Fore.GREEN}Full Success:    {Fore.GREEN}{Style.BRIGHT}{success} ({success/total*100:.1f}%){Style.RESET_ALL}")
    print(f"  {Fore.YELLOW}Saved Only:      {Fore.YELLOW}{Style.BRIGHT}{partial} ({partial/total*100:.1f}%){Style.RESET_ALL}")
    print(f"  {Fore.RED}Failed/Offline:  {Fore.RED}{Style.BRIGHT}{failed} ({failed/total*100:.1f}%){Style.RESET_ALL}")
    print(Fore.CYAN + "=" * 60 + "\n")


async def run_batch(limit=None):
    db = Database()
    
    # Step 1: Fetch all DVRs from DB into memory and close DB connection
    await db.connect()
    try:
        dvrs = await db.fetch_live_dvrs(limit=limit)
    finally:
        await db.close()

    if not dvrs:
        logger.warning("No live DVRs found in database")
        return

    logger.info(
        f"{Fore.GREEN}Loaded {len(dvrs)} DVRs into memory. "
        f"Database connection closed during execution.{Style.RESET_ALL}"
    )

    # Step 2: Process all DVRs (Fast pre-ping + Email & Event setup)
    start = time.time()
    results = await process_dvrs(dvrs)
    elapsed = time.time() - start

    # Step 3: Reconnect to DB and Bulk Save all records in 1 fast query
    logger.info(f"{Fore.CYAN}Connecting to database to save {len(results)} log records in bulk...{Style.RESET_ALL}")
    await db.connect()
    try:
        await db.bulk_insert_results(results)
    finally:
        await db.close()

    # Step 4: Summary output
    print_summary(results, elapsed)


async def run_single(ip, port, username, password):
    db = Database()
    db_connected = False
    atmid = "MANUAL_TEST"

    try:
        await db.connect()
        db_connected = True
        existing_dvr = await db.find_dvr_by_ip(ip, port)
        if existing_dvr:
            atmid = existing_dvr.atmid
            if username == "admin" and existing_dvr.username:
                username = existing_dvr.username
            if password == "trans@123" and existing_dvr.password:
                password = existing_dvr.password
    except Exception as e:
        logger.warning(f"Could not connect to database for pre-lookup: {e}")

    dvr = DVR(atmid=atmid, ip=ip, port=port,
              username=username, password=password)

    async with aiohttp.ClientSession() as session:
        client = DVRClient(session)
        result = await client.configure(dvr)
        
        status_color = Fore.GREEN if result.status == SetupStatus.SUCCESS else (Fore.YELLOW if result.status == SetupStatus.SAVED_ONLY else Fore.RED)
        
        print(f"\n {Fore.WHITE}{Style.BRIGHT}ATMID  :{Style.RESET_ALL} {Fore.CYAN}{result.atmid}")
        print(f" {Fore.WHITE}{Style.BRIGHT}Status :{Style.RESET_ALL} {status_color}{Style.BRIGHT}{result.status.value}{Style.RESET_ALL}")
        print(f" {Fore.WHITE}{Style.BRIGHT}Model  :{Style.RESET_ALL} {Fore.MAGENTA}{result.model}")
        print(f" {Fore.WHITE}{Style.BRIGHT}Remark :{Style.RESET_ALL} {result.remark}")
        print(f" {Fore.WHITE}{Style.BRIGHT}Time   :{Style.RESET_ALL} {result.duration_ms}ms")

    if db_connected:
        try:
            await db.insert_result(result)
            print(f" {Fore.WHITE}{Style.BRIGHT}DB Log :{Style.RESET_ALL} {Fore.GREEN}Saved to dvr_email_setup_logs{Style.RESET_ALL}\n")
        except Exception as e:
            logger.error(f"Failed to save result to DB: {e}")
            print(f" {Fore.WHITE}{Style.BRIGHT}DB Log :{Style.RESET_ALL} {Fore.RED}Failed to save ({e}){Style.RESET_ALL}\n")
        finally:
            await db.close()


def main():
    parser = argparse.ArgumentParser(description="Hikvision DVR Email Setup")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_batch = sub.add_parser("batch", help="Run for all live DVRs")
    p_batch.add_argument("--limit", type=int, help="Limit number of DVRs")

    p_single = sub.add_parser("single", help="Test single DVR")
    p_single.add_argument("--ip", required=True)
    p_single.add_argument("--port", type=int, default=81)
    p_single.add_argument("--user", default="admin")
    p_single.add_argument("--password", default="trans@123")

    args = parser.parse_args()
    setup_logging()

    try:
        if args.cmd == "batch":
            asyncio.run(run_batch(limit=args.limit))
        elif args.cmd == "single":
            asyncio.run(run_single(args.ip, args.port, args.user, args.password))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")


if __name__ == "__main__":
    main()