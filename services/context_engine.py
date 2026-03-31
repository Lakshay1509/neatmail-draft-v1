"""
services/context_engine.py — Core orchestration service.

Execution order:
  1. Determine provider (Gmail / Outlook) via factory.
  2. Fetch email history for the sender (last N days).
  3. Diff against already-indexed Pinecone vectors → only embed new messages.
  4. Chunk + embed new messages → upsert into Pinecone.
  5. Embed the incoming email body+subject → semantic query Pinecone (top-k).
  6. Return raw Pinecone snippet metadata as retrieved_history (no LLM).
"""

from __future__ import annotations

import asyncio
import time
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from openai import AsyncOpenAI


from config import get_settings
from models import ContextRequest, ContextResponse, EmailMessage
from providers import get_provider
from services.embedder import EmbedderService
from services.vector_store import VectorStoreService
from utils.chunker import chunk_text
from utils.logger import get_logger

logger   = get_logger(__name__)
settings = get_settings()

# ── Singleton services (reused across requests) ─────────────────────────────
_embedder     = EmbedderService()
_vector_store = VectorStoreService()
_openai       = AsyncOpenAI(api_key=settings.azure_api_key, base_url=settings.azure_endpoint)



# ── Main orchestration ───────────────────────────────────────────────────────

class ContextEngine:
    """Fully async orchestration of the semantic context pipeline."""

    async def process(self, req: ContextRequest) -> ContextResponse:
        t0 = time.perf_counter()
        since_ts = self._since_timestamp(settings.history_days)

        logger.info(
            f"ContextEngine: start [user={req.user_id}, "
            f"sender={req.sender_email}, gmail={req.is_gmail}]"
        )

        # ── Step 1: Fetch email history ──────────────────────────────────
        provider = get_provider(
            is_gmail = req.is_gmail,
            token    = req.token,
            user_id  = req.user_id,
        )
        
        history_task = provider.fetch_history(
            sender_email = req.sender_email,
            since_ts     = since_ts,
        )

        if req.threadId:
            thread_context_task = provider.fetch_thread_context(req.threadId)
            history, thread_context = await asyncio.gather(history_task, thread_context_task)
        else:
            history = await history_task
            thread_context = None

        metadata_task = self._extract_metadata(req.body, req.timezone)

        # ── Step 2: Sync new messages to Pinecone ───────────────────────
        history_synced = await self._sync_history(
            history      = history,
            user_id      = req.user_id,
            sender_email = req.sender_email,
            since_ts     = since_ts,
        )

        # ── Step 3: Embed the incoming email ────────────────────────────
        incoming_text  = f"Subject: {req.subject or ''}\n\n{req.body}"
        query_vector   = await _embedder.embed_one(incoming_text)

        # ── Step 4: Semantic query ───────────────────────────────────────
        matches = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _vector_store.query(
                user_id      = req.user_id,
                sender_email = req.sender_email,
                since_ts     = since_ts,
                query_vector = query_vector,
                top_k        = settings.max_context_vectors,
            ),
        )

        # ── Step 5: Build retrieved_history from raw matches ────────────────
        retrieved_history = self._build_retrieved_history(matches)

        # ── Wait for metadata extraction ──────────────────────────────────
        metadata = await metadata_task
        

        elapsed = time.perf_counter() - t0
        logger.info(
            f"ContextEngine: done in {elapsed:.2f}s "
            f"[synced={history_synced}, matched={len(matches)}]"
        )

        return ContextResponse(
            user_id           = req.user_id,
            sender_email      = req.sender_email,
            retrieved_history = retrieved_history,
            vectors_matched   = len(matches),
            history_synced    = history_synced,
            thread_context    = thread_context,
            intent            = metadata.get("intent"),
            keywords          = metadata.get("keywords", []),
            mentionedDates    = metadata.get("mentionedDates", []),
        )

    async def _extract_metadata(self, body: str, timezone_str: str) -> Dict[str, Any]:
        prompt = f"""Extract metadata from the following email body.
Output strictly JSON conforming to this schema (no other keys):
{{
  "intent": "<one of: [scheduling_request, task_assignment, question, follow_up, general] — from email body only>",
  "keywords": ["<up to 3 keywords from email body, or empty array>"],
  "mentionedDates": [
    {{"raw": "<exact text>", "iso": "<ISO-8601 with {timezone_str} offset>"}}
  ]
}}
Email Body:
{body}
"""
        try:
            print("LLM: calling chat completion for metadata extraction")
            logger.info("LLM: calling chat completion for metadata extraction")
            resp = await _openai.chat.completions.create(
                model=settings.openai_chat_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:
            logger.error(f"Failed to extract metadata: {e}")
            return {}

    # ── Private: sync history ────────────────────────────────────────────

    async def _sync_history(
        self,
        history:      List[EmailMessage],
        user_id:      str,
        sender_email: str,
        since_ts:     int,
    ) -> int:
        """Embed & upsert only messages not already indexed. Returns count upserted."""
        if not history:
            return 0

        # Run Pinecone lookup in a thread (SDK is synchronous)
        indexed_ids: set = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _vector_store.get_indexed_message_ids(user_id, sender_email, since_ts),
        )

        new_messages = [m for m in history if m.message_id not in indexed_ids]
        logger.info(
            f"Sync: {len(history)} total, {len(indexed_ids)} indexed, "
            f"{len(new_messages)} new to embed"
        )

        if not new_messages:
            return 0

        total_upserted = 0
        # Process messages one at a time to avoid OOM on large histories
        for msg in new_messages:
            chunks = chunk_text(msg.body, max_tokens=settings.chunk_max_tokens)
            if not chunks:
                continue

            embeddings = await _embedder.embed_many(chunks)

            upserted = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda m=msg, c=chunks, e=embeddings: _vector_store.upsert_chunks(
                    user_id      = user_id,
                    sender_email = sender_email,
                    message_id   = m.message_id,
                    subject      = m.subject,
                    timestamp    = m.timestamp,
                    is_incoming  = m.is_incoming,
                    chunks       = c,
                    embeddings   = e,
                ),
            )
            total_upserted += upserted

        return total_upserted

    # ── Private: build retrieved_history ────────────────────────────────

    @staticmethod
    def _build_retrieved_history(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert raw Pinecone match metadata into the retrieved_history shape."""
        sorted_matches = sorted(matches, key=lambda s: s.get("timestamp", 0), reverse=True)
        history = []
        for m in sorted_matches:
            ts   = m.get("timestamp", 0)
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d") if ts else "?"
            sender = m.get("sender_email", "unknown")
            history.append({
                "from": sender,
                "date": date,
                "body": m.get("text", "").strip(),
            })
        return history

    # ── Utility ──────────────────────────────────────────────────────────

    @staticmethod
    def _since_timestamp(days: int) -> int:
        """Return Unix epoch timestamp for *days* ago from now."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return int(cutoff.timestamp())
