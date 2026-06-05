import asyncio
import logging

from imap_api import config
from imap_api.storage import db

logger = logging.getLogger(__name__)

_REAP_INTERVAL = 3600  # every hour


async def reaper_loop() -> None:
    if config.IMAP_MAIL_RETENTION_DAYS == 0:
        return

    while True:
        await asyncio.sleep(_REAP_INTERVAL)
        try:
            await _purge()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Retention purge error: %s", exc)


async def _purge() -> None:
    conn = db.get_db()
    async with db.write_lock:
        cur = await conn.execute(
            """DELETE FROM emails
               WHERE COALESCE(internal_date, synced_at) < datetime('now', ? || ' days')""",
            (f"-{config.IMAP_MAIL_RETENTION_DAYS}",),
        )
        deleted = cur.rowcount
        await conn.commit()

    if deleted:
        logger.info("Retention purge: removed %d email(s)", deleted)
