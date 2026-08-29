"""Composition root kept outside the CLI transport adapter."""

from __future__ import annotations

import os

from .native_runtime import (
    NativeCodeExecutor,
    NativeCouncilExecutor,
    native_credentials,
)
from .service import DialecticService
from .store import RunStore


def build_service() -> DialecticService:
    environment = dict(os.environ)
    return DialecticService(
        RunStore(),
        credential_provider=lambda config, mode: native_credentials(
            config, mode, environment=environment
        ),
        code_executor=NativeCodeExecutor(source_environment=environment),
        council_executor=NativeCouncilExecutor(source_environment=environment),
    )
