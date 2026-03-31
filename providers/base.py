"""
providers/base.py — Abstract base class for email providers.

Every concrete provider (Gmail, Outlook) must implement this interface
so the rest of the codebase stays provider-agnostic.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List
from models import EmailMessage


class BaseEmailProvider(ABC):
    """Fetch normalised email history for a given sender."""

    def __init__(self, token: str, user_id: str) -> None:
        self.token   = token
        self.user_id = user_id

    @abstractmethod
    async def fetch_history(
        self,
        sender_email: str,
        since_ts: int,          # Unix epoch (seconds)
    ) -> List[EmailMessage]:
        """Return all messages (sent & received) with *sender_email* since *since_ts*."""
        ...

    @abstractmethod
    async def fetch_thread_context(self, thread_id: str) -> str:
        """Fetch the thread text by thread ID/conversation ID."""
        ...
