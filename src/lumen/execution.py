"""Immutable constraints attached to one Host-admitted background turn."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackgroundExecution:
    request_limit: int

    def __post_init__(self) -> None:
        if not 1 <= self.request_limit <= 50:
            raise ValueError("A background run requires a finite request limit")
