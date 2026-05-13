#!/usr/bin/env bash
# Launch the eunomia-rag service.
#
# Secrets are loaded from .env (gitignored). CLI flags override .env / YAML:
#
#   ./start-rag.sh --verbose DEBUG
#   ./start-rag.sh --config config/eunomia-rag.local.yaml
#
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
exec python -m src.main "$@"
