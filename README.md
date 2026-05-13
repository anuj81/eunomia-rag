# eunomia-rag

> **Catalog-aware relevance ranking for the Eunomia middleware.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

Standalone FastAPI service. Given a natural-language query and the user's already-authorized view list, returns the top-K most relevant views for the LLM prompt. Crucially:

**RAG ranks; OpenMetadata authorizes.** The middleware supplies the allowed-view list — the RAG service filters its Qdrant index by that list as a server-side payload condition, so it cannot return a view the user is not entitled to even if the index happens to contain it.

```
   (eunomia-middleware)
        │
        │  POST /v1/retrieve { query, allowed_views[], k }
        ▼
   ┌────────────────────────────────────────────────────────┐
   │   eunomia-rag (this repo)                              │
   │                                                        │
   │   1. Encode query with sentence-transformers           │
   │   2. Qdrant query_points:                              │
   │        vector  = encoded query                         │
   │        filter  = payload.name ∈ allowed_views          │
   │   3. Return top-K with payload + score                 │
   └────────────────────────────────────────────────────────┘
```

The indexer (cron + admin-trigger) pulls the full OpenMetadata catalog (using a Keycloak service-account token), synthesizes per-view docs, and upserts to Qdrant with stable point IDs.

---

## Stack

- **FastAPI** for the HTTP surface
- **Qdrant** as the vector store (self-hosted via docker-compose; payload index on `name` for allow-list filtering)
- **`sentence-transformers/all-MiniLM-L6-v2`** for local embeddings — 384-d, unit-normalized → cosine-friendly, ≈80MB model, no API key required
- **Keycloak** service-account (`client_credentials` grant) for OM auth at indexing time

---

## Repository layout

```
eunomia-rag/
├── config/
│   └── eunomia-rag.yaml         single source of truth (no secrets)
├── docker-compose.yml           Qdrant
├── logs/                         (gitignored)
├── src/
│   ├── api/
│   │   ├── auth.py              Bearer RAG_API_KEY verification
│   │   └── routes.py            /v1/{healthz,retrieve,index/refresh}
│   ├── config/settings.py        pydantic-settings + YAML loader
│   ├── embedding/encoder.py      sentence-transformers wrapper
│   ├── store/qdrant_client.py    Qdrant CRUD + query_points filtering
│   ├── indexer/
│   │   ├── om_source.py         Keycloak-authed OM /tables pull
│   │   ├── doc_synthesis.py     OM table → (payload, embedded text)
│   │   ├── pipeline.py          pull → synth → embed → upsert
│   │   └── __main__.py          `python -m src.indexer` for cron
│   ├── logging_setup.py
│   └── main.py
└── start-rag.sh
```

Companion repos: [`eunomia-middleware`](https://github.com/anuj81/eunomia-middleware), [`eunomia-cli`](https://github.com/anuj81/eunomia-cli), `eunomia-infrastructure`.

---

## Quickstart

```bash
# 1. Bring up Qdrant
docker-compose up -d qdrant

# 2. Python deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Secrets
cp .env.example .env
# edit .env — set KEYCLOAK_RAG_INDEXER_SECRET + RAG_API_KEY

# 4. Bootstrap the index (requires OpenMetadata up; see eunomia-infrastructure/)
python -m src.indexer --reset

# 5. Start the service
./start-rag.sh                       # → http://localhost:9000
# or: ./start-rag.sh --verbose DEBUG
```

---

## API

### `GET /v1/healthz`

```json
{ "status": "ok", "qdrant": "reachable", "collection": "eunomia_views" }
```

### `POST /v1/retrieve`

```json
POST /v1/retrieve
Authorization: Bearer <RAG_API_KEY>
{
  "query": "what's our daily revenue last week",
  "allowed_views": [
    "finance_daily_revenue_view",
    "finance_customer_payment_history_view"
  ],
  "k": 8
}
```

Response:
```json
{
  "results": [
    {
      "name": "finance_daily_revenue_view",
      "domain": "Finance",
      "description": "Aggregated daily revenue rollup. No PII.",
      "columns": [
        {"name": "day", "description": "UTC date of revenue rollup."},
        {"name": "gross_revenue", "description": "Sum of order totals."}
      ],
      "owner_team": "finance-team",
      "score": 0.83
    }
  ]
}
```

The `allowed_views` filter is applied **server-side** as a Qdrant payload filter on `payload.name`. The service does not consult user identity — that's the middleware's job; this service only ranks within whatever allow-list the middleware passes in.

### `POST /v1/index/refresh` *(admin)*

```bash
curl -X POST http://localhost:9000/v1/index/refresh \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"reset": false}'
```

Re-runs the full indexer pipeline in-process. With `"reset": true` the Qdrant collection is dropped and recreated; otherwise it's an idempotent upsert (stable point IDs).

---

## Sync model

Two equivalent paths to keep the index fresh:

```cron
# Pull from OpenMetadata every 30 minutes (idempotent upsert).
*/30 * * * *  cd /path/to/eunomia-rag && ./venv/bin/python -m src.indexer >> logs/cron.log 2>&1

# Nightly full rebuild at 03:15.
15 3 * * *    cd /path/to/eunomia-rag && ./venv/bin/python -m src.indexer --reset >> logs/cron.log 2>&1
```

The admin REST endpoint (`POST /v1/index/refresh`) is functionally the same — it runs the indexer in-process. Useful when you've just changed tag policies in OM and want the change reflected immediately.

Real-time OM webhook → RAG push sync is deferred to a later phase.

---

## Auth at indexing time (Phase D)

The indexer authenticates to OpenMetadata via a Keycloak service account:

```
indexer
  │  POST <keycloak>/realms/eunomia/protocol/openid-connect/token
  │       grant_type=client_credentials
  │       client_id=eunomia-rag-indexer
  │       client_secret=$KEYCLOAK_RAG_INDEXER_SECRET
  ▼
Keycloak  ← service account user `service-account-eunomia-rag-indexer`
  │  holds realm role `eunomia-om-admin` + carries preferred_username
  │  and email claims (set via realm hardcoded-claim mappers)
  ▼
indexer → OM (Bearer <token>)  ← service account is in OM adminPrincipals
```

A legacy `OPENMETADATA_PASSWORD` basic-auth path is kept as a fallback for when OM is in `basic` or `multi` mode.

---

## Configuration

`config/eunomia-rag.yaml`:

| Section | Key fields |
|---|---|
| `logging` | `level`, `log_dir`, `console`, `file`, `rotation` |
| `server` | `host`, `port`, `reload` |
| `openmetadata` | `url`, `database_fqn`, `username` (legacy `password` env-only) |
| `keycloak` | `issuer`, `client_id`, `refresh_window_seconds` (`client_secret` env-only) |
| `qdrant` | `url`, `collection`, `vector_size`, `distance` |
| `embedding` | `model`, `preload` |
| `auth` | `require_auth`, (`api_key` env-only) |

Precedence: CLI flags → `EUNOMIA_RAG_<SECTION>__<FIELD>` env → YAML → defaults. Secrets in YAML raise an error at startup.

---

## Development

Manual local exercise (no Keycloak / no OM required for the API itself if you've already populated Qdrant):

```bash
# A. Bring up Qdrant + populate via the indexer once
docker-compose up -d qdrant
python -m src.indexer --reset

# B. Start the service
./start-rag.sh

# C. Probe
curl http://localhost:9000/v1/healthz
curl -X POST http://localhost:9000/v1/retrieve \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"daily revenue","allowed_views":["finance_daily_revenue_view"],"k":3}'
```

### Tests

There are no Python unit tests in this repo (the end-to-end harness lives in `eunomia-infrastructure/verify_phase_d.py`, which covers the RAG service via real HTTP). The components are intentionally thin so most coverage rides on the live verification.

---

## License

[Apache 2.0](LICENSE)
