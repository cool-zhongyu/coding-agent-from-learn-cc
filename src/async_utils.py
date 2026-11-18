import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor


_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cc-agent")


def _shutdown_executor() -> None:
    _EXECUTOR.shutdown(wait=True)


atexit.register(_shutdown_executor)


async def run_in_thread(func, *args, **kwargs):
    """Run a blocking callable off the event loop.

    This intentionally avoids ``asyncio.to_thread`` in this environment.
    Python 3.12's default executor can stall during ``asyncio.run`` teardown
    after a completed future, so we use one explicit executor and shut it down
    at process exit instead.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _EXECUTOR,
        lambda: func(*args, **kwargs),
    )
