"""
providers/gmail.py — Gmail REST API email provider.

Uses the Gmail v1 REST API (no google-auth library required).
Auth: Bearer token passed directly (OAuth 2.0 access token).

Flow:
  1. Search messages matching  from:<sender> OR to:<sender>  after:<unix_ts>
  2. Batch-fetch full message details (200 per page max)
  3. Parse & normalise into EmailMessage objects
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import List, Optional

import httpx

from config import get_settings
from models import EmailMessage
from providers.base import BaseEmailProvider
from utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

_MAX_RESULTS_PER_PAGE = 100   # Gmail API max
_BATCH_CONCURRENCY   = 10     # simultaneous detail fetches


def _b64_decode(data: str) -> str:
    """URL-safe base64 decode used by the Gmail API."""
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")

def _strip_quoted_reply(text: str) -> str:
    # Remove everything after "On ... wrote:" pattern
    text = re.sub(r"\n>.*", "", text, flags=re.DOTALL)  # strip "> quoted" lines
    text = re.sub(r"On .+wrote:.*", "", text, flags=re.DOTALL)  # strip "On Tue... wrote:"
    return text.strip()


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text from a message payload."""
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return _b64_decode(data) if data else ""

    if mime.startswith("multipart/"):
        parts = payload.get("parts", [])
        # Prefer text/plain over text/html parts
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                return _b64_decode(data) if data else ""
        # Fall back to first part
        for part in parts:
            result = _extract_body(part)
            if result:
                return result

    return ""


def _get_header(headers: list, name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


class GmailProvider(BaseEmailProvider):
    """Concrete Gmail implementation of BaseEmailProvider."""

    def __init__(self, token: str, user_id: str) -> None:
        super().__init__(token, user_id)
        self._base = settings.gmail_api_base
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json",
        }

    # ── Public API ────────────────────────────────────────────────────────

    async def fetch_history(
        self,
        sender_email: str,
        since_ts: int,
    ) -> List[EmailMessage]:
        """Fetch all messages to/from sender_email since since_ts."""
        message_ids = await self._list_message_ids(sender_email, since_ts)
        if not message_ids:
            logger.info(f"Gmail: no messages found for {sender_email}")
            return []

        logger.info(f"Gmail: fetching details for {len(message_ids)} messages")
        messages = await self._fetch_details_concurrent(message_ids, sender_email)
        return messages

    async def fetch_thread_context(self, thread_id: str) -> list[dict]:
        if not thread_id:
            return []

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base}/users/me/threads/{thread_id}",
                headers=self._headers,
                params={"userId": "me", "format": "full"},
            )
            if resp.status_code != 200:
                logger.warning(f"Gmail: failed to fetch thread {thread_id}")
                return []
            data = resp.json()

        messages = data.get("messages", [])
        result = []

        for msg in messages[-3:]:
            headers = msg.get("payload", {}).get("headers", [])
            result.append({
                "from": _get_header(headers, "From"),
                "date": _get_header(headers, "Date"),
                "body": _strip_quoted_reply(_extract_body(msg.get("payload", {}))),
            })

        return result

    # ── Private helpers ───────────────────────────────────────────────────

    async def _list_message_ids(
        self, sender_email: str, since_ts: int
    ) -> List[str]:
        """Page through messages/list and collect all message IDs."""
        query = f"(from:{sender_email} OR to:{sender_email}) after:{since_ts}"
        ids: List[str] = []
        page_token: Optional[str] = None

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params: dict = {
                    "userId":     "me",
                    "q":          query,
                    "maxResults": _MAX_RESULTS_PER_PAGE,
                }
                if page_token:
                    params["pageToken"] = page_token

                resp = await client.get(
                    f"{self._base}/users/me/messages",
                    headers=self._headers,
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

                for msg in data.get("messages", []):
                    ids.append(msg["id"])

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

        return ids

    async def _fetch_details_concurrent(
        self, message_ids: List[str], sender_email: str
    ) -> List[EmailMessage]:
        """Fetch message details with bounded concurrency."""
        sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

        async def _fetch_one(mid: str) -> Optional[EmailMessage]:
            async with sem:
                try:
                    return await self._fetch_message(mid, sender_email)
                except Exception as exc:
                    logger.warning(f"Gmail: failed to fetch message {mid}: {exc}")
                    return None

        results = await asyncio.gather(*[_fetch_one(mid) for mid in message_ids])
        return [r for r in results if r is not None]

    async def _fetch_message(
        self, message_id: str, sender_email: str
    ) -> Optional[EmailMessage]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base}/users/me/messages/{message_id}",
                headers=self._headers,
                params={"userId": "me", "format": "full"},
            )
            resp.raise_for_status()
            data = resp.json()

        headers = data.get("payload", {}).get("headers", [])
        subject    = _get_header(headers, "Subject") or "(no subject)"
        from_addr  = _get_header(headers, "From").lower()
        date_str   = _get_header(headers, "Date")
        body       = _extract_body(data.get("payload", {}))

        # internalDate is milliseconds since epoch
        internal_date = data.get("internalDate")
        if internal_date:
            timestamp = int(internal_date) // 1000
        else:
            timestamp = int(time.time())

        is_incoming = sender_email in from_addr

        return EmailMessage(
            message_id  = message_id,
            subject     = subject,
            body        = body,
            timestamp   = timestamp,
            is_incoming = is_incoming,
        )
