"""
models.py — Shared Pydantic data models.
"""

from pydantic import BaseModel, EmailStr, field_validator
from typing import Any, Dict, List, Optional


class ContextRequest(BaseModel):
    user_id:      str
    sender_email: str
    token:        str          # Gmail or Microsoft Graph OAuth token
    body:         str
    subject:      Optional[str] = None
    timezone:     str
    is_gmail:     bool = True
    threadId:     Optional[str] = None

    @field_validator("body")
    @classmethod
    def body_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Email body must not be empty.")
        return v.strip()

    @field_validator("sender_email")
    @classmethod
    def sender_email_lowercase(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("threadId")
    @classmethod
    def normalize_thread_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        return cleaned or None


class EmailMessage(BaseModel):
    """Normalised email message from any provider."""
    message_id:  str
    subject:     str
    body:        str
    timestamp:   int           # Unix epoch (seconds)
    is_incoming: bool          # True if received, False if sent


class ContextResponse(BaseModel):
    user_id:           str
    sender_email:      str
    retrieved_history: List[Dict[str, Any]]  # Raw Pinecone snippet metadata
    vectors_matched:   int                   # How many Pinecone results contributed
    history_synced:    int                   # How many new emails were upserted this call
    thread_context: Optional[list[dict]] = None
    intent: Optional[str] = None
    keywords: Optional[List[str]] = None
    mentionedDates: Optional[List[Dict[str, str]]] = None
