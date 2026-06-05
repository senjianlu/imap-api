import asyncio
from pathlib import Path

import aiosqlite

from imap_api import config
from imap_api.storage.models import CREATE_EMAILS, CREATE_ATTACHMENTS, CREATE_SYNC_STATE, INDEXES

_db: aiosqlite.Connection | None = None
write_lock = asyncio.Lock()


async def init_db() -> None:
    global _db
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.execute(CREATE_EMAILS)
    await _db.execute(CREATE_ATTACHMENTS)
    await _db.execute(CREATE_SYNC_STATE)
    for idx in INDEXES:
        await _db.execute(idx)
    await _db.commit()


async def close_db() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


def get_db() -> aiosqlite.Connection:
    assert _db is not None, "Database not initialised"
    return _db
