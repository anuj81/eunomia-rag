"""Centralized logging configuration for eunomia-rag.

Same shape as the middleware's logging_setup so operators can read both
services' logs without learning two formats.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


_LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_TAG = "eunomia_rag_handler"
_LOG_FILENAME = "eunomia-rag.log"

_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}


def _resolve_log_dir(raw: Path) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        project_root = Path(__file__).resolve().parent.parent
        p = project_root / p
    return p


def _drop_existing_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if getattr(h, _HANDLER_TAG, False):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def configure_logging(settings: "Settings") -> None:
    cfg = settings.logging
    level = _LEVEL_MAP[cfg.level]
    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    _drop_existing_handlers(root)
    root.setLevel(level)

    if cfg.console:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        sh.setLevel(level)
        setattr(sh, _HANDLER_TAG, True)
        root.addHandler(sh)

    if cfg.file:
        log_dir = _resolve_log_dir(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            filename=log_dir / _LOG_FILENAME,
            maxBytes=cfg.rotation.max_bytes,
            backupCount=cfg.rotation.backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        fh.setLevel(level)
        setattr(fh, _HANDLER_TAG, True)
        root.addHandler(fh)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        _drop_existing_handlers(lg)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(level)

    if level > logging.DEBUG:
        for name in ("httpx", "httpcore", "urllib3", "requests"):
            logging.getLogger(name).setLevel(logging.WARNING)

    # Sentence-transformers / huggingface can be very chatty.
    for name in ("sentence_transformers", "transformers", "huggingface_hub"):
        logging.getLogger(name).setLevel(max(level, logging.INFO))
