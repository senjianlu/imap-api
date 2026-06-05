import asyncio
import logging
from datetime import datetime, timezone

import aioimaplib

from imap_api import config
from imap_api.core.sync import extract_uidvalidity, incremental_sync, reconcile_uidvalidity

logger = logging.getLogger(__name__)

_IDLE_INNER_TIMEOUT = 60  # seconds per wait_server_push call
_NOOP_TIMEOUT = 10


async def idle_worker(folder: str, state: dict) -> None:
    """Long-running task for a single folder. Restarts with exponential backoff on any error."""
    backoff = 1
    while True:
        try:
            await _run_connection(folder, state)
            backoff = 1
        except asyncio.CancelledError:
            logger.info("Worker cancelled: %s", folder)
            state["connection"] = "disconnected"
            raise
        except Exception as exc:
            state["connection"] = "disconnected"
            state["last_error"] = str(exc)
            logger.error("Worker error [%s]: %s — retry in %ds", folder, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _run_connection(folder: str, state: dict) -> None:
    state["connection"] = "connecting"

    if config.IMAP_SSL:
        client = aioimaplib.IMAP4_SSL(host=config.IMAP_HOST, port=config.IMAP_PORT)
    else:
        client = aioimaplib.IMAP4(host=config.IMAP_HOST, port=config.IMAP_PORT)

    try:
        await client.wait_hello_from_server()
        login_resp = await client.login(config.IMAP_USERNAME, config.IMAP_PASSWORD)
        if login_resp.result != "OK":
            raise RuntimeError(f"LOGIN failed: {login_resp.lines}")

        select_resp = await client.select(folder)
        if select_resp.result != "OK":
            raise RuntimeError(f"SELECT {folder!r} failed: {select_resp.lines}")

        uidvalidity = extract_uidvalidity(select_resp.lines)
        if uidvalidity is None:
            raise RuntimeError(f"Could not extract UIDVALIDITY for {folder!r}")

        last_seen_uid = await reconcile_uidvalidity(folder, uidvalidity)

        # Compensating sync: catch any mail that arrived during the reconnect window
        state["connection"] = "syncing"
        last_seen_uid = await incremental_sync(client, folder, uidvalidity, last_seen_uid)
        state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = None
        state["connection"] = "idle"

        loop = asyncio.get_event_loop()
        deadline = loop.time() + config.IDLE_RECONNECT_INTERVAL

        while loop.time() < deadline:
            remaining = deadline - loop.time()
            timeout = min(_IDLE_INNER_TIMEOUT, remaining)
            if timeout <= 0:
                break

            idle_task = await client.idle_start()
            try:
                pushes = await asyncio.wait_for(
                    client.wait_server_push(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                pushes = []
            finally:
                client.idle_done()
                try:
                    await asyncio.wait_for(idle_task, timeout=_NOOP_TIMEOUT)
                except (asyncio.TimeoutError, Exception):
                    pass

            state["last_idle_at"] = datetime.now(timezone.utc).isoformat()

            if _has_exists(pushes):
                state["connection"] = "syncing"
                last_seen_uid = await incremental_sync(client, folder, uidvalidity, last_seen_uid)
                state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
                state["connection"] = "idle"
            else:
                noop_resp = await client.noop()
                if noop_resp.result != "OK":
                    raise RuntimeError("NOOP failed — connection dead")

        logger.debug("Periodic reconnect for %s", folder)

    finally:
        try:
            await client.logout()
        except Exception:
            pass


def _has_exists(lines: list) -> bool:
    for line in lines:
        s = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        if "EXISTS" in s:
            return True
    return False
