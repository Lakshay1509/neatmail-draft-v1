# AGENTS.md — NeatMail Context API

## Run

```bash
cp .env.example .env   # fill credentials
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
Set `APP_ENV=development` to expose `/docs` (Swagger UI).  
Docker runs on port **3000** (`Dockerfile:19`), not 8000.

## Architecture

Single FastAPI service. Request path:

1. `POST /context` → `ContextEngine.process()` (`services/context_engine.py`)
2. Provider factory (`providers/__init__.py`) → `GmailProvider` or `OutlookProvider`
3. Fetch 60-day history → chunk → embed → Pinecone upsert (skips already-indexed)
4. Embed incoming email → Pinecone query (top-k) → return raw metadata (no LLM synthesis)

## Critical gotchas

- **Azure OpenAI is active, not direct OpenAI.** `EmbedderService` and `ContextEngine` instantiate `AsyncOpenAI` with `azure_endpoint` + `azure_api_key`. The `openai_api_key` config field is **unused** by the code.
- **Pinecone SDK is synchronous.** All Pinecone calls must be wrapped in `asyncio.get_event_loop().run_in_executor(None, ...)`.
- **OAuth tokens are passed per-request** in the `ContextRequest.token` field. No token refresh or storage in this service.
- **Pinecone vector IDs** are `sha256("{user_id}#{sender_email}#{message_id}#{chunk_idx}")[:48]`. Do not change this hash format — it breaks idempotent upserts.
- **No LLM synthesis in the response.** The `/context` endpoint returns raw `retrieved_history` (Pinecone metadata). LLM is only used for `_extract_metadata()` (intent, keywords, dates).
- **Outlook `fetch_thread_context`** falls back through three resolution paths: conversationId → message ID → internetMessageId. Graph tenants can reject `conversationId eq` + `$orderby` so the code sorts locally.

## Endpoints

| Method | Path       | Auth              |
|--------|-----------|-------------------|
| POST   | `/context`| `X-API-Key` header|
| GET    | `/health` | none              |

## Config

`config.py` reads `.env` via `pydantic_settings`. All values have defaults except `openai_api_key`, `pinecone_api_key`, `dashboard_api_key`, `azure_endpoint`, `azure_api_key`.
