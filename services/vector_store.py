"""
services/vector_store.py — Pinecone vector store service.

Responsibilities:
  - Upsert new email chunks (with rich metadata for filtering).
  - Query by semantic similarity with strict metadata filters.
  - Check which message IDs are already indexed to avoid re-embedding.

Vector ID format:  {user_id}#{sender_email}#{message_id}#{chunk_idx}
  → deterministic, collision-free, idempotent upserts.

Metadata schema:
    user_id       : str    (for multi-tenancy isolation)
    sender_email  : str    (for per-sender filtering)
    message_id    : str
    chunk_idx     : int
    timestamp     : int    (Unix epoch — enables range filtering)
    subject       : str
    is_incoming   : bool
    text          : str    (chunk text stored for retrieval)
"""

from __future__ import annotations

import hashlib
from typing import List, Dict, Any, Set

from pinecone import Pinecone, ServerlessSpec

from config import get_settings
from utils.logger import get_logger

logger   = get_logger(__name__)
settings = get_settings()


def _make_vector_id(user_id: str, sender_email: str, message_id: str, chunk_idx: int) -> str:
    """Build a deterministic, URL-safe vector ID."""
    raw = f"{user_id}#{sender_email}#{message_id}#{chunk_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:48]


class VectorStoreService:
    """Async-compatible Pinecone client wrapper (Pinecone SDK is sync)."""

    def __init__(self) -> None:
        pc = Pinecone(api_key=settings.pinecone_api_key)
        self._index = self._get_or_create_index(pc)

    # ── Index bootstrap ───────────────────────────────────────────────────

    @staticmethod
    def _get_or_create_index(pc: Pinecone):
        """Create the index if it doesn't exist, return the Index object."""
        existing = [idx.name for idx in pc.list_indexes()]
        if settings.pinecone_index_name not in existing:
            logger.info(f"Creating Pinecone index '{settings.pinecone_index_name}'")
            pc.create_index(
                name      = settings.pinecone_index_name,
                dimension = settings.openai_embedding_dimensions,
                metric    = "cosine",
                spec      = ServerlessSpec(
                    cloud  = "aws",
                    region = settings.pinecone_environment,
                ),
            )
        return pc.Index(settings.pinecone_index_name)

    # ── Write path ────────────────────────────────────────────────────────

    def upsert_chunks(
        self,
        user_id:      str,
        sender_email: str,
        message_id:   str,
        subject:      str,
        timestamp:    int,
        is_incoming:  bool,
        chunks:       List[str],
        embeddings:   List[List[float]],
    ) -> int:
        """
        Upsert all (chunk, embedding) pairs for a single email.
        Returns the number of vectors upserted.
        """
        vectors = []
        for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            vid = _make_vector_id(user_id, sender_email, message_id, idx)
            vectors.append({
                "id":     vid,
                "values": emb,
                "metadata": {
                    "user_id":      user_id,
                    "sender_email": sender_email,
                    "message_id":   message_id,
                    "chunk_idx":    idx,
                    "timestamp":    timestamp,
                    "subject":      subject[:200],   # Pinecone metadata string limit
                    "is_incoming":  is_incoming,
                    "text":         chunk[:1000],    # store truncated chunk for retrieval
                },
            })

        if vectors:
            self._index.upsert(vectors=vectors, namespace=user_id)
            logger.info(
                f"VectorStore: upserted {len(vectors)} vectors "
                f"[user={user_id}, sender={sender_email}]"
            )

        return len(vectors)

    # ── Read path ─────────────────────────────────────────────────────────

    def get_indexed_message_ids(
        self,
        user_id:      str,
        sender_email: str,
        since_ts:     int,
    ) -> Set[str]:
        """
        Return the set of message_ids already indexed for this user/sender
        within the time window.  Uses a dummy query with a zero vector.
        """
        zero_vec = [0.0] * settings.openai_embedding_dimensions

        response = self._index.query(
            vector          = zero_vec,
            top_k           = 10_000,  # fetch as many as Pinecone allows
            include_metadata= True,
            namespace       = user_id,
            filter          = {
                "user_id":      {"$eq": user_id},
                "sender_email": {"$eq": sender_email},
                "timestamp":    {"$gte": since_ts},
            },
        )

        return {
            match["metadata"]["message_id"]
            for match in response.get("matches", [])
            if "message_id" in match.get("metadata", {})
        }

    def query(
        self,
        user_id:      str,
        sender_email: str,
        since_ts:     int,
        query_vector: List[float],
        top_k:        int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search within the user+sender scope.
        Returns a list of metadata dicts for the top_k matches.
        """
        response = self._index.query(
            vector          = query_vector,
            top_k           = top_k,
            include_metadata= True,
            namespace       = user_id,
            filter          = {
                "user_id":      {"$eq": user_id},
                "sender_email": {"$eq": sender_email},
                "timestamp":    {"$gte": since_ts},
            },
        )

        matches = response.get("matches", [])
        logger.info(
            f"VectorStore: query returned {len(matches)} matches "
            f"[user={user_id}, sender={sender_email}]"
        )
        return [m["metadata"] for m in matches if m.get("metadata")]
