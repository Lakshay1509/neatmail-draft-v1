"""
services/embedder.py — OpenAI embedding service.

Provides a single async function to embed one or many texts using
text-embedding-3-small (1536 dimensions).

Retry logic: exponential backoff on transient 5xx / rate-limit errors.
"""

from __future__ import annotations

import asyncio
from typing import List

from openai import AsyncOpenAI, RateLimitError, APIStatusError

from config import get_settings
from utils.logger import get_logger

logger   = get_logger(__name__)
settings = get_settings()

_MAX_RETRIES = 3
_BACKOFF_BASE = 2  # seconds



class EmbedderService:
    """Thin async wrapper around the OpenAI Embeddings endpoint."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(base_url=settings.azure_endpoint, api_key=settings.azure_api_key )

        self._model  = settings.openai_embedding_model
        self._dims   = settings.openai_embedding_dimensions

    async def embed_one(self, text: str) -> List[float]:
        """Embed a single text string. Returns a float vector."""
        results = await self.embed_many([text])
        return results[0]

    async def embed_many(self, texts: List[str]) -> List[List[float]]:
        """
        Embed a batch of texts in a single API call.
        Falls back on rate-limit with exponential backoff.
        """
        if not texts:
            return []

        # OpenAI recommends replacing newlines for embedding quality
        clean = [t.replace("\n", " ").strip() for t in texts]

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await self._client.embeddings.create(
                    model      = self._model,
                    input      = clean,
                    dimensions = self._dims,
                )
                return [item.embedding for item in response.data]

            except RateLimitError as exc:
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    f"Embedder: rate-limited (attempt {attempt}/{_MAX_RETRIES}), "
                    f"retrying in {wait}s — {exc}"
                )
                await asyncio.sleep(wait)

            except APIStatusError as exc:
                if exc.status_code >= 500:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning(
                        f"Embedder: server error {exc.status_code} "
                        f"(attempt {attempt}/{_MAX_RETRIES}), retrying in {wait}s"
                    )
                    await asyncio.sleep(wait)
                else:
                    raise

        raise RuntimeError(
            f"Embedder: failed after {_MAX_RETRIES} retries for {len(texts)} texts."
        )
