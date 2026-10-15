from datetime import datetime
import os
from pathlib import Path
import sys
from threading import Thread

from loguru import logger


def _write_daily_logs(read_fd: int, log_dir: Path) -> None:
    current_path = None
    output_file = None

    try:
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break

            path = log_dir / f"{datetime.now():%Y%m%d}.txt"
            if path != current_path:
                if output_file is not None:
                    output_file.close()
                output_file = path.open("ab")
                current_path = path

            output_file.write(chunk)
            output_file.flush()
    finally:
        if output_file is not None:
            output_file.close()
        os.close(read_fd)


def configure_daily_logging() -> None:
    """Send Python and native stdout/stderr output to Log/YYYYMMDD.txt."""
    log_dir = Path(__file__).resolve().parents[1] / "Log"
    log_dir.mkdir(parents=True, exist_ok=True)

    stdout_fd = sys.stdout.fileno()
    stderr_fd = sys.stderr.fileno()
    read_fd, write_fd = os.pipe()

    writer = Thread(
        target=_write_daily_logs,
        args=(read_fd, log_dir),
        name="daily-log-writer",
        daemon=True,
    )
    writer.start()

    try:
        os.dup2(write_fd, stdout_fd)
        os.dup2(write_fd, stderr_fd)
    finally:
        os.close(write_fd)

    sys.stdout = os.fdopen(
        os.dup(stdout_fd), "w", buffering=1, encoding="utf-8", errors="replace"
    )
    sys.stderr = os.fdopen(
        os.dup(stderr_fd), "w", buffering=1, encoding="utf-8", errors="replace"
    )

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        colorize=False,
        backtrace=True,
        diagnose=False,
    )
