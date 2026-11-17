"""Bound expensive stages across jobs while allowing their pipelines to overlap."""

import asyncio
import weakref
from contextlib import asynccontextmanager

_LIMITERS = weakref.WeakKeyDictionary()
_PACERS = weakref.WeakKeyDictionary()


async def run_blocking(function, *args, **kwargs):
    """Keep a worker's slot and file leases until its native work has stopped."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def _service_state(name):
    loop = asyncio.get_running_loop()
    return _PACERS.setdefault(loop, {}).setdefault(name, {"lock": asyncio.Lock(), "next": 0, "blocked": 0})


async def wait_for_service(name, interval=.4):
    """Space requests globally and honor cooldowns set by any job."""
    state = _service_state(name)
    loop = asyncio.get_running_loop()
    async with state["lock"]:
        while True:
            delay = max(state["next"], state["blocked"]) - loop.time()
            if delay <= 0:
                state["next"] = loop.time() + interval
                return
            await asyncio.sleep(delay)


def defer_service(name, seconds):
    state = _service_state(name)
    state["blocked"] = max(state["blocked"], asyncio.get_running_loop().time() + seconds)


@asynccontextmanager
async def resource_slot(name, limit):
    loop = asyncio.get_running_loop()
    slots = _LIMITERS.setdefault(loop, {})
    semaphore = slots.setdefault((name, limit), asyncio.Semaphore(limit))
    async with semaphore:
        yield
