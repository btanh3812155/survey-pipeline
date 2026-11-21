import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import main
from core.embedding_executor import InferenceBusyError, run_inference
from services import image_objects, telegram_polling


_TIMEOUT = 10


@pytest.mark.asyncio
async def test_health_progress_bounded_admission_and_lifespan_shutdown(monkeypatch):
    monkeypatch.setattr(main.settings, "ENVIRONMENT", "development")
    redis = SimpleNamespace(ping=AsyncMock(), aclose=AsyncMock())
    pool = SimpleNamespace(open=AsyncMock(side_effect=ConnectionError), close=AsyncMock())
    monkeypatch.setattr(main.aioredis, "from_url", lambda *a, **kw: redis)
    monkeypatch.setattr(main, "AsyncConnectionPool", lambda **kw: pool)
    monkeypatch.setattr(main, "seed_media_storage", lambda: None)
    monkeypatch.setattr(image_objects, "load_registry", lambda **kw: {"objects": [], "engines": []})
    monkeypatch.setattr(telegram_polling, "configure_bot_menus", AsyncMock())

    ready, stop, closing, queued = (asyncio.Event() for _ in range(4))
    entered = [asyncio.Event(), asyncio.Event()]
    release = [threading.Event(), threading.Event()]
    workers = []
    lock = threading.Lock()
    active = peak = 0
    loop = asyncio.get_running_loop()

    def slow_inference(index, *, result):
        nonlocal active, peak
        with lock:
            workers.append(threading.current_thread())
            active += 1
            peak = max(active, peak)
        try:
            loop.call_soon_threadsafe(entered[index].set)
            assert release[index].wait(_TIMEOUT), "inference test did not release worker"
            return result
        finally:
            with lock:
                active -= 1

    async def serve():
        async with main.lifespan(main.app):
            ready.set()
            await stop.wait()
            closing.set()

    async def submit_second():
        # Admission happens before the next suspension, so the observer of this
        # event sees the waiting slot already reserved.
        queued.set()
        return await run_inference(slow_inference, 1, result="second")

    tasks = [server := asyncio.create_task(serve())]
    try:
        await asyncio.wait_for(ready.wait(), _TIMEOUT)
        tasks.append(first := asyncio.create_task(run_inference(slow_inference, 0, result="first")))
        await asyncio.wait_for(entered[0].wait(), _TIMEOUT)
        tasks.append(second := asyncio.create_task(submit_second()))
        await asyncio.wait_for(queued.wait(), _TIMEOUT)

        with pytest.raises(InferenceBusyError):
            await asyncio.wait_for(run_inference(lambda: None), _TIMEOUT)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            health = await asyncio.wait_for(client.get("/health"), _TIMEOUT)
        assert health.status_code == 200
        assert health.json()["status"] == "healthy"
        assert not first.done() and not entered[1].is_set()
        assert workers[0] is not threading.current_thread()

        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        with pytest.raises(InferenceBusyError):
            await asyncio.wait_for(run_inference(lambda: None), _TIMEOUT)

        stop.set()
        await asyncio.wait_for(closing.wait(), _TIMEOUT)
        assert not server.done()
        release[0].set()
        assert await asyncio.wait_for(first, _TIMEOUT) == "first"
        await asyncio.wait_for(entered[1].wait(), _TIMEOUT)
        with pytest.raises(InferenceBusyError, match="unavailable"):
            await asyncio.wait_for(run_inference(lambda: None), _TIMEOUT)
        assert not server.done()  # Shutdown is waiting for accepted work, not just its awaiters.

        release[1].set()
        await asyncio.wait_for(server, _TIMEOUT)
        assert peak == 1
        assert len(workers) == 2 and workers[0] is workers[1]
        assert not workers[0].is_alive()
        with pytest.raises(InferenceBusyError, match="unavailable"):
            await run_inference(lambda: None)
    finally:
        stop.set()
        for gate in release:
            gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), _TIMEOUT)
