"""eunomia-rag — FastAPI entrypoint.

Mirrors the launch contract of eunomia-middleware/src/main.py:

    python -m src.main                       # argparse CLI
    uvicorn src.main:app                     # programmatic
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from fastapi import FastAPI

from .api.routes import router
from .config import get_settings, reset_settings_cache
from .logging_setup import configure_logging

load_dotenv()
configure_logging(get_settings())

app = FastAPI(title="Eunomia RAG")
app.include_router(router, prefix="/v1")

# Optional eager warmups, controlled by settings.
_settings_at_import = get_settings()
if _settings_at_import.embedding.preload:
    # Lazy import to avoid pulling sentence-transformers at module-import time
    # in environments that don't need it (tests, indexer-only runs).
    from .embedding import get_encoder

    get_encoder(_settings_at_import)


_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def _parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="eunomia-rag")
    parser.add_argument(
        "--config", type=str, default=None,
        help="YAML config path (default: config/eunomia-rag.yaml or $EUNOMIA_RAG_CONFIG).",
    )
    parser.add_argument(
        "--verbose", type=str, choices=list(_LOG_LEVELS), default=None,
        help="Log level override.",
    )
    return parser.parse_args()


def _apply_cli_to_env(args: argparse.Namespace) -> None:
    if args.config:
        os.environ["EUNOMIA_RAG_CONFIG"] = args.config
    if args.verbose is not None:
        os.environ["EUNOMIA_RAG_LOGGING__LEVEL"] = args.verbose


def main() -> None:
    args = _parse_cli()
    _apply_cli_to_env(args)
    reset_settings_cache()
    settings = get_settings()
    configure_logging(settings)

    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
    )


if __name__ == "__main__":
    main()
