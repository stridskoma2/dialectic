"""Composition root kept outside the CLI transport adapter."""

from __future__ import annotations

from .service import DialecticService
from .store import RunStore


def build_service() -> DialecticService:
    return DialecticService(RunStore())
