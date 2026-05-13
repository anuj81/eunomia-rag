"""CLI entrypoint: `python -m src.indexer`.

Use this for cron-driven re-indexing. The same logic is exposed over HTTP by
`POST /v1/index/refresh` for admin-triggered refreshes.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from ..config import get_settings, reset_settings_cache
from ..logging_setup import configure_logging
from .pipeline import run_indexing

logger = logging.getLogger("eunomia_rag.indexer")


def _parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eunomia-rag-indexer",
        description="Pull table metadata from OpenMetadata, embed, and upsert to Qdrant.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop the Qdrant collection before re-indexing (full rebuild).",
    )
    parser.add_argument(
        "--verbose",
        type=str,
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        default=None,
        help="Log level override.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="YAML config path (default: config/eunomia-rag.yaml or $EUNOMIA_RAG_CONFIG).",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = _parse_cli()

    import os
    if args.config:
        os.environ["EUNOMIA_RAG_CONFIG"] = args.config
    if args.verbose:
        os.environ["EUNOMIA_RAG_LOGGING__LEVEL"] = args.verbose

    reset_settings_cache()
    settings = get_settings()
    configure_logging(settings)

    logger.info(
        "Indexer starting | qdrant=%s collection=%s om=%s reset=%s",
        settings.qdrant.url, settings.qdrant.collection,
        settings.openmetadata.url, args.reset,
    )
    try:
        result = run_indexing(settings, reset=args.reset)
    except Exception:
        logger.exception("Indexer run failed")
        return 1
    logger.info(
        "Indexer done | fetched=%d indexed=%d collection=%s reset=%s",
        result.fetched, result.indexed, result.collection, result.reset,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
