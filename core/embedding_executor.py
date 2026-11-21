"""Single-worker, bounded execution for synchronous image inference."""

import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, ParamSpec, TypeVar


_P = ParamSpec("_P")
_T = TypeVar("_T")


class InferenceBusyError(RuntimeError):
    """Inference has no admission slot or the application is not serving it."""


class _EmbeddingExecutor:
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedding")
        self._pending: set[asyncio.Future[Any]] = set()
        self._closed = False

    async def run(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        if self._closed or asyncio.get_running_loop() is not self._loop:
            raise InferenceBusyError("Inference executor is unavailable")
        # Checking and reserving a slot happen on the owner loop without an await.
        if len(self._pending) >= 2:
            raise InferenceBusyError("Inference worker and waiting slot are occupied")
        future = self._loop.run_in_executor(self._pool, partial(fn, *args, **kwargs))
        self._pending.add(future)
        future.add_done_callback(self._finished)
        # A cancelled request does not free a slot while its job still occupies it.
        return await asyncio.shield(future)

    def _finished(self, future: asyncio.Future[Any]) -> None:
        self._pending.remove(future)
        if not future.cancelled():
            future.exception()  # Observe failures even if the request was cancelled.

    async def close(self) -> None:
        self._closed = True
        drained = asyncio.gather(*self._pending, return_exceptions=True)
        cancellation = None
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError as exc:
                cancellation = exc
        # The accepted jobs have finished; shutdown only has to join the idle worker.
        self._pool.shutdown()
        if cancellation is not None:
            raise cancellation


_executor: _EmbeddingExecutor | None = None


def get_embedding_executor() -> _EmbeddingExecutor:
    if _executor is None:
        raise InferenceBusyError("Inference executor is unavailable")
    return _executor


async def run_inference(fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Run synchronous inference with immutable inputs or worker-owned snapshots.

    Pass normalized image bytes or a private PIL copy, not a request-owned image
    or mutable cache. Accepted work can outlive the awaiting request.
    """
    return await get_embedding_executor().run(fn, *args, **kwargs)


@asynccontextmanager
async def embedding_executor_lifespan() -> AsyncIterator[_EmbeddingExecutor]:
    global _executor
    if _executor is not None:
        raise RuntimeError("Inference executor lifespan is already active")
    executor = _executor = _EmbeddingExecutor()
    try:
        yield executor
    finally:
        try:
            await executor.close()
        finally:
            _executor = None
