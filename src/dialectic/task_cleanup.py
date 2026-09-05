"""Cancellation and cleanup precedence for controller-owned tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable


async def cancel_and_wait(tasks: Iterable[asyncio.Future[object]]) -> None:
    """Drain owned work without hiding cleanup failure behind cancellation."""
    owned = tuple(tasks)
    for task in owned:
        if not task.done() and not (isinstance(task, asyncio.Task) and task.cancelling()):
            task.cancel()
    drained = asyncio.gather(*owned, return_exceptions=True)
    cancelled = False
    while not drained.done():
        try:
            await asyncio.shield(drained)
        except asyncio.CancelledError:
            # A second cancellation must not interrupt native process cleanup.
            cancelled = True
    for result in drained.result():
        # NativeTurnError and DialecticFailure share this failure-kind contract.
        # Matching it here avoids importing either higher-level exception module.
        if (
            isinstance(result, BaseException)
            and getattr(result, "kind", None) == "PROCESS_CLEANUP_FAILED"
        ):
            raise result
    if cancelled:
        raise asyncio.CancelledError
