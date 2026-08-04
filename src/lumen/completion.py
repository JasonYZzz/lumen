"""UI-independent completion value objects."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompletionSuggestion:
    """One displayed completion and the text inserted when it is selected."""

    label: str
    insert: str
    description: str | None = None


__all__ = ["CompletionSuggestion"]
