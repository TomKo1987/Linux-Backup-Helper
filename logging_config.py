from __future__ import annotations
from pathlib import Path
import logging, os, threading
from logging.handlers import RotatingFileHandler

__all__ = ["setup_logger", "get_log_file_path"]

_LOG_DIR  = Path(os.environ.get("HOME") or Path.home()) / ".config" / "Backup Helper" / "logs"
_LOG_FILE = _LOG_DIR / "backup_helper.log"

_RAW_LEVEL = os.environ.get("LOG_LEVEL", "").upper()
if _RAW_LEVEL:
    _DEFAULT_LEVEL = getattr(logging, _RAW_LEVEL, None)
    if not isinstance(_DEFAULT_LEVEL, int):
        print(f"WARNING: unknown LOG_LEVEL '{_RAW_LEVEL}', defaulting to INFO", flush=True)
        _DEFAULT_LEVEL = logging.INFO
else:
    _DEFAULT_LEVEL = logging.INFO

_FORMATTER = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(name)s  —  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_handler_lock:        threading.Lock            = threading.Lock()
_shared_file_handler: RotatingFileHandler | None = None


def _get_file_handler() -> RotatingFileHandler | None:
    global _shared_file_handler
    if _shared_file_handler is not None:
        return _shared_file_handler
    with _handler_lock:
        if _shared_file_handler is not None:
            return _shared_file_handler
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            _shared_file_handler = RotatingFileHandler(
                _LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            _shared_file_handler.setFormatter(_FORMATTER)
            _shared_file_handler.setLevel(_DEFAULT_LEVEL)
        except OSError as exc:
            print(f"WARNING: could not create log file handler: {exc}", flush=True)
    return _shared_file_handler


def setup_logger(name: str, level: int = _DEFAULT_LEVEL) -> logging.Logger:
    logger = logging.getLogger(name)

    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in logger.handlers
    )
    if not has_stream:
        ch = logging.StreamHandler()
        ch.setFormatter(_FORMATTER)
        ch.setLevel(level)
        logger.addHandler(ch)

    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        fh = _get_file_handler()
        if fh:
            logger.addHandler(fh)

    logger.setLevel(level)
    logger.propagate = False
    return logger


def get_log_file_path() -> Path:
    return _LOG_FILE
