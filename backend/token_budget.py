"""Exact token accounting for task-time skill capsules.

The serving model's tokenizer is deliberately configured by path.  The
resolver must not download a tokenizer or silently fall back to a character
estimate on the internet path: if exact accounting is unavailable, the
internet candidate remains discovery-only.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Any, Protocol


class TokenCounter(Protocol):
    """Minimal tokenizer boundary used by the capsule compiler/guard."""

    tokenizer_id: str

    def count(self, text: str) -> int:
        ...


class TokenizerUnavailable(RuntimeError):
    """Raised when exact serving-model tokenization is not configured."""


@dataclass(frozen=True)
class FixedTokenCounter:
    """Deterministic test counter; never used by the production default."""

    tokenizer_id: str = "fixture-tokenizer-v1"
    tokens_per_word: int = 1

    def count(self, text: str) -> int:
        words = [part for part in str(text or "").split() if part]
        return len(words) * max(1, int(self.tokens_per_word))


class HuggingFaceTokenCounter:
    """Count with a local `tokenizers` JSON for the configured serving model."""

    def __init__(self, path: str, *, tokenizer_id: str | None = None) -> None:
        tokenizer_path = Path(path).expanduser()
        if not tokenizer_path.is_file():
            raise TokenizerUnavailable(f"serving tokenizer file not found: {tokenizer_path}")
        try:
            from tokenizers import Tokenizer

            self._tokenizer: Any = Tokenizer.from_file(str(tokenizer_path))
        except Exception as exc:  # pragma: no cover - dependency/runtime specific
            raise TokenizerUnavailable(f"serving tokenizer could not load: {type(exc).__name__}") from exc
        digest = hashlib.sha256(tokenizer_path.read_bytes()).hexdigest()[:16]
        self.tokenizer_id = f"{tokenizer_id or tokenizer_path.name}:{digest}"

    def count(self, text: str) -> int:
        try:
            return len(self._tokenizer.encode(str(text or "")).ids)
        except Exception as exc:  # pragma: no cover - dependency/runtime specific
            raise TokenizerUnavailable(f"serving tokenizer failed: {type(exc).__name__}") from exc


_DEFAULT_COUNTER: TokenCounter | None = None
_DEFAULT_COUNTER_PATH = ""


def default_token_counter() -> TokenCounter | None:
    """Load the configured serving tokenizer without network access.

    A path change resets the process-local instance so tests and a rotated
    serving model cannot reuse counts from the previous tokenizer.
    """

    global _DEFAULT_COUNTER, _DEFAULT_COUNTER_PATH
    path = os.getenv("AUTOSKILL_SERVING_TOKENIZER_PATH", "").strip()
    if not path:
        return None
    if _DEFAULT_COUNTER is not None and _DEFAULT_COUNTER_PATH == path:
        return _DEFAULT_COUNTER
    try:
        _DEFAULT_COUNTER = HuggingFaceTokenCounter(
            path,
            tokenizer_id=os.getenv("AUTOSKILL_SERVING_TOKENIZER_ID", "").strip() or None,
        )
        _DEFAULT_COUNTER_PATH = path
    except TokenizerUnavailable:
        _DEFAULT_COUNTER = None
        _DEFAULT_COUNTER_PATH = path
    return _DEFAULT_COUNTER


__all__ = [
    "FixedTokenCounter",
    "HuggingFaceTokenCounter",
    "TokenCounter",
    "TokenizerUnavailable",
    "default_token_counter",
]
