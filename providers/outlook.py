"""
providers/outlook.py — Microsoft Graph API email provider.

Auth: Bearer token (OAuth 2.0 / MSAL delegated access token).
Scopes required: Mail.Read, Mail.Send (read-only is sufficient for fetching).

Flow:
  1. Query /me/messages with OData $filter for sender and date range.
  2. Page through @odata.nextLink until exhausted.
  3. Parse & normalise into EmailMessage objects.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import httpx

from config import get_settings
from models import EmailMessage
from providers.base import BaseEmailProvider
from utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

_PAGE_SIZE = 50   # Graph API $top limit (max 1000, keep lower for memory)


def _escape_odata_string(value: str) -> str:
    """Escape single quotes for OData string literals."""
    return value.replace("'", "''")


def _message_has_recipient(item: dict, sender_email: str) -> bool:
    """Return True when sender_email appears in toRecipients."""
    target = (sender_email or "").strip().lower()
    for recipient in item.get("toRecipients", []):
        addr = recipient.get("emailAddress", {}).get("address", "")
        if addr.strip().lower() == target:
            return True
    return False

def _strip_quoted_reply(text: str) -> str:
    import re
    text = re.sub(r"\n>.*", "", text, flags=re.DOTALL)
    text = re.sub(r"On .+wrote:.*", "", text, flags=re.DOTALL)
    return text.strip()


def _parse_graph_datetime(dt_str: str) -> int:
    """Convert Graph API ISO 8601 datetime string → Unix epoch (seconds)."""
    try:
        # Graph returns UTC without trailing Z or with it
        dt_str = dt_str.rstrip("Z")
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return int(time.time())


def _extract_body_graph(body: dict) -> str:
    """Extract plain text from a Graph message body object."""
    content_type = body.get("contentType", "text")
    content      = body.get("content", "")
    if content_type == "html":
        # Lightweight HTML stripping — no dependency required
        import re
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"&nbsp;", " ", content)
        content = re.sub(r"\s{2,}", " ", content).strip()
    return content.strip()


class OutlookProvider(BaseEmailProvider):
    """Concrete Microsoft Graph implementation of BaseEmailProvider."""

    def __init__(self, token: str, user_id: str) -> None:
        super().__init__(token, user_id)
        self._base    = settings.graph_api_base
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
        # Graph examples use a UTC literal with trailing Z in OData filters.
        since_iso = (
            datetime
            .fromtimestamp(since_ts, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

        received = await self._fetch_messages(sender_email, since_iso, folder="inbox")
        sent     = await self._fetch_messages(sender_email, since_iso, folder="sentitems")

        all_messages = received + sent
        logger.info(
            f"Outlook: fetched {len(received)} received + {len(sent)} sent "
            f"messages for {sender_email}"
        )
        return all_messages

    async def fetch_thread_context(self, thread_id: str) -> list[dict]:
        """Fetch the last 3 messages as structured dicts."""
        if not thread_id:
            return []

        url = f"{self._base}/me/messages"
        params = {
            "$filter": f"conversationId eq '{_escape_odata_string(thread_id)}'",
            "$select": "id,subject,body,receivedDateTime,from,toRecipients",
            "$orderby": "receivedDateTime desc",
            "$top": 3,
        }

        messages = []
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers, params=params)
            if resp.status_code != 200:
                logger.warning(
                    "Outlook: failed to fetch thread %s [status=%s, detail=%s]",
                    thread_id,
                    resp.status_code,
                    (resp.text or "")[:300],
                )
                return []

            data = resp.json()
            for item in data.get("value", []):
                body = _strip_quoted_reply(_extract_body_graph(item.get("body", {})))
                from_dict = item.get("from", {}).get("emailAddress", {})
                from_addr = f"{from_dict.get('name', '')} <{from_dict.get('address', '')}>".strip()
                messages.append({
                    "from": from_addr,
                    "date": item.get("receivedDateTime", ""),
                    "body": body,
                })

        messages.reverse()  # chronological order
        return messages


    # ── Private helpers ───────────────────────────────────────────────────

    async def _fetch_messages(
        self,
        sender_email: str,
        since_iso: str,
        folder: str,
    ) -> List[EmailMessage]:
        """
        Fetch messages from a specific folder.
        - inbox   → filter by from address
        - sentitems → filter by date (recipient filtering is done client-side)
        """
        sender_email_escaped = _escape_odata_string(sender_email)

        if folder == "inbox":
            odata_filter = (
                f"from/emailAddress/address eq '{sender_email_escaped}' "
                f"and receivedDateTime ge {since_iso}"
            )
        else:
            # Graph can reject toRecipients/any(...) in this endpoint on some tenants.
            # Keep server-side date filtering and apply recipient filtering locally.
            odata_filter = f"receivedDateTime ge {since_iso}"

        select_fields = "id,subject,body,receivedDateTime,from,toRecipients"
        url = f"{self._base}/me/mailFolders/{folder}/messages"
        params = {
            "$filter": odata_filter,
            "$select": select_fields,
            "$top": _PAGE_SIZE,
        }

        messages: List[EmailMessage] = []

        async with httpx.AsyncClient(timeout=30) as client:
            while url:
                if params is not None:
                    resp = await client.get(url, headers=self._headers, params=params)
                    params = None
                else:
                    # @odata.nextLink already contains encoded query params.
                    resp = await client.get(url, headers=self._headers)
                resp.raise_for_status()
                data = resp.json()

                for item in data.get("value", []):
                    if folder == "sentitems" and not _message_has_recipient(item, sender_email):
                        continue

                    msg = self._parse_message(item, sender_email, folder)
                    if msg:
                        messages.append(msg)

                url = data.get("@odata.nextLink")  # None when exhausted

        return messages

    def _parse_message(
        self,
        item: dict,
        sender_email: str,
        folder: str,
    ) -> Optional[EmailMessage]:
        try:
            subject   = item.get("subject") or "(no subject)"
            body      = _extract_body_graph(item.get("body", {}))
            timestamp = _parse_graph_datetime(item.get("receivedDateTime", ""))
            is_incoming = folder == "inbox"

            return EmailMessage(
                message_id  = item["id"],
                subject     = subject,
                body        = body,
                timestamp   = timestamp,
                is_incoming = is_incoming,
            )
        except Exception as exc:
            logger.warning(f"Outlook: failed to parse message: {exc}")
            return None
