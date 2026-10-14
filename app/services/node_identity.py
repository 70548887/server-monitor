"""Coordinate node identity changes with async producers of node data."""

import asyncio
import threading
from functools import wraps
from weakref import WeakKeyDictionary


_loop_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_loop_locks_guard = threading.Lock()


def node_mutation_lock() -> asyncio.Lock:
    """Return the shared mutation lock for the current running event loop.

    All node writes and heartbeat publication use this lock so a rename cannot
    leave new records under the old name. Separate loops need separate locks,
    including the short-lived event loops used by tests.
    """
    loop = asyncio.get_running_loop()
    with _loop_locks_guard:
        lock = _loop_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _loop_locks[loop] = lock
        return lock


def serialize_node_mutation(func):
    """Serialize an async node mutation against heartbeat publication."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        async with node_mutation_lock():
            return await func(*args, **kwargs)
    return wrapper
