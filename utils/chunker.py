"""
utils/chunker.py — Split long email bodies into token-bounded chunks.

Uses a simple word-based approximation (≈ 0.75 words/token) to avoid
pulling in a heavy tokeniser dependency. For production workloads swap
the splitter to tiktoken if exact token counting is required.
"""

from __future__ import annotations
import re
from typing import List


_WORD_TO_TOKEN_RATIO = 0.75   # conservative estimate


def _approx_tokens(text: str) -> int:
    return int(len(text.split()) * _WORD_TO_TOKEN_RATIO)


def _clean(text: str) -> str:
    """Strip excessive whitespace and quoted-reply headers."""
    # Remove common reply-chain markers
    text = re.sub(r"(?m)^>.*$", "", text)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, max_tokens: int = 500) -> List[str]:
    """
    Split *text* into chunks that each fit within *max_tokens*.

    Strategy:
      1. Split on paragraph boundaries first (double newlines).
      2. If a paragraph still exceeds the limit, split on sentences.
      3. Append chunks greedily until the limit is reached.

    Returns a list of non-empty string chunks.
    """
    text = _clean(text)
    if not text:
        return []

    # Fast-path: entire text fits in one chunk
    if _approx_tokens(text) <= max_tokens:
        return [text]

    paragraphs: List[str] = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    current_parts: List[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = _approx_tokens(para)

        if para_tokens > max_tokens:
            # Split oversized paragraph into sentences
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sent in sentences:
                sent_tokens = _approx_tokens(sent)
                if current_tokens + sent_tokens > max_tokens and current_parts:
                    chunks.append(" ".join(current_parts))
                    current_parts = []
                    current_tokens = 0
                current_parts.append(sent)
                current_tokens += sent_tokens
        else:
            if current_tokens + para_tokens > max_tokens and current_parts:
                chunks.append("\n\n".join(current_parts))
                current_parts = []
                current_tokens = 0
            current_parts.append(para)
            current_tokens += para_tokens

    if current_parts:
        chunk = " ".join(current_parts) if len(current_parts) > 1 else current_parts[0]
        chunks.append(chunk)

    return [c for c in chunks if c.strip()]
