import json
import logging
import re
from datetime import datetime, timedelta, timezone

from imap_api import config
from imap_api.core.mime import parse_message
from imap_api.storage import db

logger = logging.getLogger(__name__)


def extract_uidvalidity(lines: list) -> int | None:
    for line in lines:
        s = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        m = re.search(r"UIDVALIDITY\s+(\d+)", s)
        if m:
            return int(m.group(1))
    return None


def parse_uid_list(data: bytes | str) -> list[int]:
    s = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    result = []
    for token in s.strip().split():
        try:
            result.append(int(token))
        except ValueError:
            pass
    return sorted(result)


def parse_flags(raw: str) -> list[str]:
    return [f.strip() for f in raw.strip().split() if f.strip() and f.strip() not in ("(", ")")]


def parse_imap_date(date_str: str) -> str | None:
    if not date_str:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S %z", "%d-%b-%Y %H:%M:%S +0000"):
        try:
            dt = datetime.strptime(date_str.strip('"'), fmt)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return None


def parse_fetch_response(lines: list) -> tuple[bytes | None, str, str, int]:
    """Extract (raw_email, internaldate, flags, size) from a UID FETCH response."""
    raw = None
    internaldate = ""
    flags = ""
    size = 0

    for line in lines:
        if isinstance(line, (bytes, bytearray)):
            b = bytes(line)
            if len(b) > 20 and raw is None:
                raw = b
        else:
            s = str(line)
            m = re.search(r'INTERNALDATE "([^"]+)"', s, re.IGNORECASE)
            if m:
                internaldate = m.group(1)
            m = re.search(r'FLAGS \(([^)]*)\)', s, re.IGNORECASE)
            if m:
                flags = m.group(1)
            m = re.search(r'RFC822\.SIZE (\d+)', s, re.IGNORECASE)
            if m:
                size = int(m.group(1))

    return raw, internaldate, flags, size


async def get_sync_state(folder: str) -> dict | None:
    conn = db.get_db()
    async with conn.execute(
        "SELECT folder, uidvalidity, last_seen_uid, last_sync_at FROM sync_state WHERE folder=?",
        (folder,),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def save_sync_state(folder: str, uidvalidity: int, last_seen_uid: int) -> None:
    conn = db.get_db()
    async with db.write_lock:
        await conn.execute(
            """INSERT INTO sync_state(folder, uidvalidity, last_seen_uid, last_sync_at)
               VALUES(?, ?, ?, datetime('now'))
               ON CONFLICT(folder) DO UPDATE SET
                 uidvalidity   = excluded.uidvalidity,
                 last_seen_uid = excluded.last_seen_uid,
                 last_sync_at  = excluded.last_sync_at""",
            (folder, uidvalidity, last_seen_uid),
        )
        await conn.commit()


async def reconcile_uidvalidity(folder: str, server_uidvalidity: int) -> int:
    """Return last_seen_uid to use for the next incremental sync (0 means start fresh)."""
    state = await get_sync_state(folder)

    if state is None:
        return 0

    if state["uidvalidity"] == server_uidvalidity:
        return state["last_seen_uid"] or 0

    logger.warning(
        "UIDVALIDITY changed for %s (%s → %s); resetting sync state",
        folder, state["uidvalidity"], server_uidvalidity,
    )
    conn = db.get_db()
    async with db.write_lock:
        await conn.execute("DELETE FROM sync_state WHERE folder=?", (folder,))
        await conn.commit()
    return 0


async def incremental_sync(client, folder: str, uidvalidity: int, last_seen_uid: int) -> int:
    """Fetch and store all emails newer than last_seen_uid. Returns new last_seen_uid."""
    if last_seen_uid == 0:
        if config.INITIAL_SYNC_DAYS == 0:
            resp = await client.uid("search", "ALL")
            if resp.result == "OK" and resp.lines:
                uids = parse_uid_list(resp.lines[0])
                max_uid = max(uids) if uids else 0
            else:
                max_uid = 0
            await save_sync_state(folder, uidvalidity, max_uid)
            return max_uid

        since = (datetime.now(timezone.utc) - timedelta(days=config.INITIAL_SYNC_DAYS))
        date_str = since.strftime("%d-%b-%Y")
        resp = await client.uid("search", f"SINCE {date_str}")
    else:
        resp = await client.uid("search", f"UID {last_seen_uid + 1}:*")

    if resp.result != "OK":
        logger.error("UID SEARCH failed for %s: %s", folder, resp)
        return last_seen_uid

    raw_line = resp.lines[0] if resp.lines else b""
    uids = parse_uid_list(raw_line)

    if not uids:
        await save_sync_state(folder, uidvalidity, last_seen_uid)
        return last_seen_uid

    logger.info("Syncing %d email(s) in %s", len(uids), folder)
    new_max = last_seen_uid

    for uid in uids:
        try:
            stored_uid = await _fetch_and_store(client, folder, uidvalidity, uid)
            if stored_uid > new_max:
                new_max = stored_uid
        except Exception as exc:
            logger.error("Failed to fetch UID %d in %s: %s", uid, folder, exc)

    await save_sync_state(folder, uidvalidity, new_max)
    return new_max


async def _fetch_and_store(client, folder: str, uidvalidity: int, uid: int) -> int:
    resp = await client.uid(
        "fetch", str(uid),
        "(RFC822 INTERNALDATE FLAGS RFC822.SIZE)",
    )
    if resp.result != "OK":
        return uid

    raw, internaldate_str, flags_str, size = parse_fetch_response(resp.lines)

    if raw is None:
        return uid

    if len(raw) > config.MAX_FETCH_SIZE:
        resp2 = await client.uid("fetch", str(uid), "(BODY[HEADER] INTERNALDATE FLAGS RFC822.SIZE)")
        raw2, internaldate_str, flags_str, size = parse_fetch_response(resp2.lines)
        if raw2 is None:
            return uid
        fields, attachments = parse_message(raw2, False, False, config.MAX_FETCH_SIZE)
        fields["truncated"] = 1
    else:
        fields, attachments = parse_message(
            raw, config.FETCH_BODY, config.STORE_ATTACHMENTS, config.MAX_FETCH_SIZE
        )

    internal_date = parse_imap_date(internaldate_str)
    flags_json = json.dumps(parse_flags(flags_str))

    conn = db.get_db()
    async with db.write_lock:
        # Cross-UIDVALIDITY dedup by Message-ID
        if fields["message_id"]:
            async with conn.execute(
                "SELECT id FROM emails WHERE message_id=? AND folder=?",
                (fields["message_id"], folder),
            ) as cur:
                if await cur.fetchone():
                    return uid

        cur = await conn.execute(
            """INSERT OR IGNORE INTO emails
               (uidvalidity, uid, folder, message_id,
                from_addr, to_addr, cc_addr, subject,
                internal_date, sent_date, flags,
                body_text, body_html, has_attachments, size, truncated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uidvalidity, uid, folder, fields["message_id"],
                fields["from_addr"], fields["to_addr"], fields["cc_addr"], fields["subject"],
                internal_date, fields["sent_date"], flags_json,
                fields.get("body_text"), fields.get("body_html"),
                fields["has_attachments"], size, fields["truncated"],
            ),
        )
        email_id = cur.lastrowid

        if email_id and cur.rowcount and attachments:
            for att in attachments:
                await conn.execute(
                    """INSERT INTO attachments(email_id, filename, content_type, size, content)
                       VALUES(?,?,?,?,?)""",
                    (email_id, att["filename"], att["content_type"], att["size"], att["content"]),
                )

        await conn.commit()

    return uid
