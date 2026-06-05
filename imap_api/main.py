import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from imap_api import config
from imap_api.storage import db
from imap_api.core.idle_worker import idle_worker
from imap_api.core.reaper import reaper_loop
from imap_api.api import emails as emails_api
from imap_api.api import status as status_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

worker_states: dict[str, dict] = {
    folder: {
        "connection": "disconnected",
        "last_idle_at": None,
        "last_sync_at": None,
        "last_error": None,
    }
    for folder in config.IMAP_FOLDERS
}

_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()

    status_api.worker_states = worker_states

    for folder in config.IMAP_FOLDERS:
        _tasks.append(
            asyncio.create_task(
                idle_worker(folder, worker_states[folder]),
                name=f"idle_worker:{folder}",
            )
        )

    _tasks.append(asyncio.create_task(reaper_loop(), name="reaper"))

    yield

    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    await db.close_db()


app = FastAPI(title="imap-api", version="0.1.0", lifespan=lifespan)

app.include_router(status_api.router)
app.include_router(emails_api.router)


if __name__ == "__main__":
    uvicorn.run(
        "imap_api.main:app",
        host=config.API_HOST,
        port=config.API_PORT,
        log_level="info",
    )
