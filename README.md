# eunomia-rag

Retrieval service for the Eunomia middleware. Holds metadata for all
tables/views/columns + descriptions, and returns top-K relevant tables for a
given NLQ — **filtered by the user's allowed-views list** that the middleware
supplies. RAG ranks; OpenMetadata authorizes.

## Stack

- **FastAPI** — service layer
- **Qdrant** — vector store (self-hosted, Docker)
- **sentence-transformers** (`all-MiniLM-L6-v2`, 384-d) — local embeddings, no API calls

## Layout

```
eunomia-rag/
├── config/
│   └── eunomia-rag.yaml         # default config (no secrets)
├── docker-compose.yml           # Qdrant
├── logs/
├── requirements.txt
├── src/
│   ├── api/
│   │   ├── auth.py              # Bearer RAG_API_KEY
│   │   └── routes.py            # /v1/retrieve, /v1/index/refresh, /v1/healthz
│   ├── config/
│   │   └── settings.py          # pydantic-settings + YAML loader
│   ├── embedding/
│   │   └── encoder.py           # sentence-transformers wrapper
│   ├── store/
│   │   └── qdrant_client.py     # Qdrant wrapper
│   ├── indexer/
│   │   ├── om_source.py         # pull tables/cols from OpenMetadata REST
│   │   ├── doc_synthesis.py     # OM table → embeddable doc text
│   │   ├── pipeline.py          # pull + embed + upsert
│   │   └── __main__.py          # python -m src.indexer
│   ├── logging_setup.py
│   └── main.py                  # FastAPI entrypoint
└── start-rag.sh
```

## Quickstart

```bash
# 1. Start Qdrant
docker-compose up -d qdrant   # or: docker compose up -d qdrant on newer installs

# 2. Bring up Python deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Copy secrets template
cp .env.example .env
# edit .env — set OPENMETADATA_PASSWORD and RAG_API_KEY

# 4. Index from OpenMetadata (cron this — refresh endpoint also available)
python -m src.indexer

# 5. Start the service
./start-rag.sh                       # → http://localhost:9000
# or: ./start-rag.sh --verbose DEBUG
```

## API

```
GET  /v1/healthz                                 → liveness + qdrant reachability
POST /v1/retrieve                                → top-K ranked views
POST /v1/index/refresh    (admin)                → re-pull from OM and re-embed
```

### `/v1/retrieve`

```json
POST /v1/retrieve
Authorization: Bearer <RAG_API_KEY>
{
  "query": "what's our daily revenue last week",
  "allowed_views": ["finance_daily_revenue_view", "finance_customer_payment_history_view"],
  "k": 8
}
```

The `allowed_views` list is **mandatory**. Qdrant filters at query time on
`payload.name ∈ allowed_views`, so RAG can never return views the user is not
authorized to see — even if the index contains them.

## Sync model (Phase B)

1. **Manual / cron**: `python -m src.indexer` runs the full pull+embed+upsert pipeline.
2. **Admin endpoint**: `POST /v1/index/refresh` triggers the same pipeline in-process.

Real-time OpenMetadata webhook → RAG is deferred to a later phase.

### Sample crontab

```cron
# Re-index from OpenMetadata every 30 minutes (idempotent upsert).
*/30 * * * *  cd /Users/anuj/sandbox/open/eunomia-rag && ./venv/bin/python -m src.indexer >> logs/cron.log 2>&1

# Nightly full rebuild at 03:15 — drops the collection and re-creates it.
15 3 * * *    cd /Users/anuj/sandbox/open/eunomia-rag && ./venv/bin/python -m src.indexer --reset >> logs/cron.log 2>&1
```

### Admin-trigger refresh

```bash
curl -X POST http://localhost:9000/v1/index/refresh \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"reset": false}'
```

## Security boundary

This service is internal-only. It assumes the caller (the middleware) has
already authorized the user against OpenMetadata. The `allowed_views` filter
is the only authorization knob this service honors — it does not look at
user identity at all.
