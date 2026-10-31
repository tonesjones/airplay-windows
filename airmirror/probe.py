"""Find a TV's AirPlay port by asking it directly, for when it stops announcing itself.

After a session ends, the Samsung UNU710D can stay silent over mDNS for minutes while it still
answers on its AirPlay port. That port changes on every TV reboot, so scan the whole range.
"""

from __future__ import annotations

import asyncio

INFO_REQUEST = b"GET /info RTSP/1.0\r\nCSeq: 1\r\n\r\n"


async def _is_airplay(ip: str, port: int, timeout: float) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, TimeoutError):
        return False
    try:
        writer.write(INFO_REQUEST)
        await writer.drain()
        head = await asyncio.wait_for(reader.read(512), 1.5)
        return b"AirTunes" in head
    except (OSError, TimeoutError):
        return False
    finally:
        writer.close()


async def _scan(ip: str, ports: range, batch: int, timeout: float) -> int | None:
    limit = asyncio.Semaphore(batch)

    async def probe(port: int) -> int | None:
        async with limit:
            return port if await _is_airplay(ip, port, timeout) else None

    tasks = [asyncio.create_task(probe(p)) for p in ports]
    try:
        for next_done in asyncio.as_completed(tasks):
            if port := await next_done:
                return port
        return None
    finally:
        for task in tasks:
            task.cancel()


def find_port(
    ip: str, ports: range = range(1024, 65536), batch: int = 1000, timeout: float = 0.5
) -> int | None:
    """Return a port on ip that answers like an AirPlay receiver, stopping at the first, or None."""
    return asyncio.run(_scan(ip, ports, batch, timeout))
