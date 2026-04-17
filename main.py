"""
main.py — FastAPI application entrypoint.

Endpoints:
  POST /context   → run the full semantic context pipeline
  GET  /health    → liveness check
"""

from __future__ import annotations

import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from config import get_settings
from models import ContextRequest, ContextResponse
from services.context_engine import ContextEngine
from utils.logger import get_logger

settings = get_settings()
logger   = get_logger("main", level=settings.log_level)

# ── Lifespan (startup / shutdown) ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"NeatMail Context API starting [env={settings.app_env}]")
    yield
    logger.info("NeatMail Context API shutting down")


# ── App factory ──────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "NeatMail Context API",
    description = "Semantic email context retrieval using Pinecone + GPT.",
    version     = "1.0.0",
    lifespan    = lifespan,
    docs_url    = "/docs" if settings.app_env != "production" else None,
    redoc_url   = None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["https://dashboard.neatmail.app"],
    allow_methods  = ["POST", "GET"],
    allow_headers  = ["*"],
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

def verify_api_key(api_key: str = Depends(api_key_header)):
    if api_key != settings.dashboard_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    return api_key

# Singleton engine — shared across all requests
_engine = ContextEngine()


# ── Global exception handler ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
        content     = {"detail": "An internal error occurred. Please try again."},
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"])
async def health():
    """Liveness probe."""
    return {"status": "ok", "env": settings.app_env}


@app.post(
    "/context",
    response_model = ContextResponse,
    status_code    = status.HTTP_200_OK,
    tags           = ["Context"],
    summary        = "Get semantic context for an incoming email",
    dependencies   = [Depends(verify_api_key)],
)
async def get_context(req: ContextRequest) -> ContextResponse:
    """
    Full pipeline:
    1. Fetch 60-day email history from Gmail or Outlook.
    2. Sync new messages into Pinecone.
    3. Semantic search for the top-5 most relevant past interactions.
    4. LLM synthesis into a natural language context summary.
    """
    try:
        result = await _engine.process(req)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error(f"Upstream API error: {exc}")
        raise HTTPException(
            status_code = status.HTTP_502_BAD_GATEWAY,
            detail      = "Failed to reach email provider. Check your token and retry.",
        )
