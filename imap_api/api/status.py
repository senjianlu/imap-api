from fastapi import APIRouter, Depends

from imap_api import config
from imap_api.security.auth import verify_token
from imap_api.storage import db

router = APIRouter()

# Injected by main.py at startup
worker_states: dict[str, dict] = {}


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/status")
async def status(_: None = Depends(verify_token)):
    conn = db.get_db()
    result: dict[str, dict] = {}

    for folder in config.IMAP_FOLDERS:
        state = worker_states.get(folder, {})

        async with conn.execute(
            "SELECT COUNT(*) AS total FROM emails WHERE folder=?", (folder,)
        ) as cur:
            row = await cur.fetchone()
            total = row["total"] if row else 0

        async with conn.execute(
            "SELECT uidvalidity, last_seen_uid, last_sync_at FROM sync_state WHERE folder=?",
            (folder,),
        ) as cur:
            sync = await cur.fetchone()

        result[folder] = {
            "connection": state.get("connection", "disconnected"),
            "uidvalidity": sync["uidvalidity"] if sync else None,
            "last_seen_uid": sync["last_seen_uid"] if sync else None,
            "last_idle_at": state.get("last_idle_at"),
            "last_sync_at": sync["last_sync_at"] if sync else None,
            "last_error": state.get("last_error"),
            "total_emails": total,
        }

    return {"folders": result}
