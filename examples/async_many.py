"""Create several addresses concurrently, list them, then delete them (asyncio)."""

from __future__ import annotations

import asyncio

import tmailor


async def main(n: int = 3) -> None:
    async with tmailor.AsyncClient() as client:
        boxes = await asyncio.gather(*(client.mailboxes.create() for _ in range(n)))
        for box in boxes:
            print(box.email)
        try:
            counts = await asyncio.gather(*(b.info() for b in boxes))
            print([b.message_count for b in counts])
        finally:
            await asyncio.gather(*(b.delete() for b in boxes), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
