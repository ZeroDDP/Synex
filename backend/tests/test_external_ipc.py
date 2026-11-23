import asyncio

import pytest

from app.external.ipc import LIMIT, Channel


async def test_oversized_header_rejected_before_reading_body():
    reader = asyncio.StreamReader()
    reader.feed_data((LIMIT + 1).to_bytes(4, "big"))
    with pytest.raises(ValueError, match="frame size"):
        await Channel(reader, None, None, None).read()
