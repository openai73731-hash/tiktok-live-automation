"""Wait for a finite, owned transition despite repeated caller cancellation."""
import asyncio


async def finish_owned(task):
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve failures even when the caller was cancelled.
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
