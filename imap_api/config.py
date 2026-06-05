import os

IMAP_HOST: str = os.environ["IMAP_HOST"]
IMAP_PORT: int = int(os.environ.get("IMAP_PORT", "993"))
IMAP_SSL: bool = os.environ.get("IMAP_SSL", "true").lower() == "true"
IMAP_USERNAME: str = os.environ["IMAP_USERNAME"]
IMAP_PASSWORD: str = os.environ["IMAP_PASSWORD"]
IMAP_FOLDERS: list[str] = [
    f.strip()
    for f in os.environ.get("IMAP_FOLDERS", "INBOX").split(",")
    if f.strip()
]
IMAP_API_TOKEN: str = os.environ.get("IMAP_API_TOKEN", "")
IDLE_RECONNECT_INTERVAL: int = int(os.environ.get("IDLE_RECONNECT_INTERVAL", "900"))
IMAP_MAIL_RETENTION_DAYS: int = int(os.environ.get("IMAP_MAIL_RETENTION_DAYS", "365"))
INITIAL_SYNC_DAYS: int = int(os.environ.get("INITIAL_SYNC_DAYS", "30"))
FETCH_BODY: bool = os.environ.get("FETCH_BODY", "true").lower() == "true"
STORE_ATTACHMENTS: bool = os.environ.get("STORE_ATTACHMENTS", "false").lower() == "true"
MAX_FETCH_SIZE: int = int(os.environ.get("MAX_FETCH_SIZE", "26214400"))

DB_PATH: str = "/data/imap-api.db"
API_HOST: str = "0.0.0.0"
API_PORT: int = 8000
