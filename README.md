# NeatMail — Semantic Email Context API

Provides natural-language context for any incoming email by querying a vector store of the sender's 60-day interaction history.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         FastAPI  /context                    │
└──────────────────────────┬───────────────────────────────────┘
                           │  ContextRequest
                           ▼
                    ContextEngine (services/context_engine.py)
                    ┌──────┴──────┐
                    │             │
            GmailProvider   OutlookProvider   (providers/)
                    │             │
                    └──────┬──────┘
                           │  List[EmailMessage]  (60 days)
                           ▼
                    EmbedderService              (services/embedder.py)
                    text-embedding-3-small
                           │  vectors
                           ▼
                    VectorStoreService           (services/vector_store.py)
                    Pinecone  (upsert + query)
                           │  top-5 snippets
                           ▼
                    OpenAI gpt-5-mini
                           │  context_summary : str
                           ▼
                    ContextResponse  → caller
```

## File Structure

```
neatmail-draft-v1/
├── main.py                    # FastAPI app & routes
├── config.py                  # Pydantic-Settings (env vars)
├── models.py                  # Shared Pydantic models
├── requirements.txt
├── .env.example               # Copy → .env and fill credentials
├── providers/
│   ├── __init__.py            # Provider factory
│   ├── base.py                # Abstract BaseEmailProvider
│   ├── gmail.py               # Google Gmail REST API
│   └── outlook.py             # Microsoft Graph API
├── services/
│   ├── __init__.py
│   ├── embedder.py            # OpenAI embedding (with retry)
│   ├── vector_store.py        # Pinecone upsert / query
│   └── context_engine.py     # Full pipeline orchestration
└── utils/
    ├── __init__.py
    ├── chunker.py             # Token-aware text chunker
    └── logger.py             # Structured JSON logger
```

## Quick Start

### 1. Clone & set up environment

```bash
cd neatmail-draft-v1
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env and fill in:
#   OPENAI_API_KEY
#   PINECONE_API_KEY
#   PINECONE_INDEX_NAME
#   PINECONE_ENVIRONMENT
```

### 3. Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Set `APP_ENV=development` to enable `/docs` (Swagger UI).

---

## API

### `POST /context`

**Request body:**
```json
{
  "user_id":      "user-123",
  "sender_email": "alice@example.com",
  "token":        "<Gmail or Graph OAuth token>",
  "body":         "Hi, following up on our meeting...",
  "subject":      "Follow-up",
  "timezone":     "Asia/Kolkata",
  "is_gmail":     true
}
```

**Response:**
```json
{
  "user_id":         "user-123",
  "sender_email":    "alice@example.com",
  "context_summary": "Alice and the user previously discussed a Q1 roadmap on 2026-03-10 and agreed on a March 28 deadline. This follow-up revisits the same project thread.",
  "vectors_matched": 5,
  "history_synced":  12
}
```

### `GET /health`
```json
{ "status": "ok", "env": "production" }
```

---

## Pinecone Vector Schema

| Field          | Type   | Description                                 |
|----------------|--------|---------------------------------------------|
| `user_id`      | string | Multi-tenancy isolation key                 |
| `sender_email` | string | Email address used for filtering            |
| `message_id`   | string | Provider message ID                         |
| `chunk_idx`    | int    | Position of chunk within the message        |
| `timestamp`    | int    | Unix epoch — supports `$gte` range filter   |
| `subject`      | string | Email subject line                          |
| `is_incoming`  | bool   | `true` = received, `false` = sent           |
| `text`         | string | Stored chunk text (truncated to 1000 chars) |

Vector namespace = `user_id` for complete tenant isolation.

---

## Required OAuth Scopes

| Provider    | Scopes                                      |
|-------------|---------------------------------------------|
| Gmail       | `https://www.googleapis.com/auth/gmail.readonly` |
| Outlook     | `Mail.Read`                                 |
