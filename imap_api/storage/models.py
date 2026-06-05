CREATE_EMAILS = """
CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY,
    uidvalidity     INTEGER NOT NULL,
    uid             INTEGER NOT NULL,
    message_id      TEXT,
    folder          TEXT NOT NULL,
    from_addr       TEXT,
    to_addr         TEXT DEFAULT '[]',
    cc_addr         TEXT DEFAULT '[]',
    subject         TEXT,
    internal_date   TIMESTAMP,
    sent_date       TIMESTAMP,
    flags           TEXT DEFAULT '[]',
    body_text       TEXT,
    body_html       TEXT,
    has_attachments INTEGER DEFAULT 0,
    size            INTEGER,
    truncated       INTEGER DEFAULT 0,
    synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(folder, uidvalidity, uid)
)
"""

CREATE_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS attachments (
    id           INTEGER PRIMARY KEY,
    email_id     INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    filename     TEXT,
    content_type TEXT,
    size         INTEGER,
    content      BLOB
)
"""

CREATE_SYNC_STATE = """
CREATE TABLE IF NOT EXISTS sync_state (
    folder        TEXT PRIMARY KEY,
    uidvalidity   INTEGER,
    last_seen_uid INTEGER DEFAULT 0,
    last_sync_at  TIMESTAMP
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_emails_internal_date ON emails(internal_date)",
    "CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_emails_folder_date ON emails(folder, internal_date)",
]
