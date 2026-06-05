import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator

from imap_api import config
from imap_api.core.mime import parse_message
from imap_api.storage import db

logger = logging.getLogger(__name__)

# aioimaplib only allows UID COPY/FETCH/EXPUNGE/STORE — UID SEARCH is rejected.
# We use UID FETCH ranges to discover new UIDs (incremental), and regular SEARCH
# + plain FETCH (UID) to discover UIDs for the initial date-bounded sync.


# ── Low-level helpers ────────────────────────────────────────────────────────

def extract_uidvalidity(lines: list) -> int | None:
    for line in lines:
        s = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        m = re.search(r"UIDVALIDITY\s+(\d+)", s)
        if m:
            return int(m.group(1))
    return None


def _parse_numbers(data: bytes | str) -> list[int]:
    s = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    result = []
    for token in s.strip().split():
        try:
            result.append(int(token))
        except ValueError:
            pass
    return sorted(result)


def _extract_uids(lines: list) -> list[int]:
    """Pull every UID <n> occurrence out of a FETCH response."""
    uids: set[int] = set()
    for line in lines:
        s = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        for m in re.finditer(r"\bUID (\d+)", s, re.IGNORECASE):
            uids.add(int(m.group(1)))
    return sorted(uids)


def _batched(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def parse_flags(raw: str) -> list[str]:
    return [f.strip() for f in raw.strip().split() if f.strip() not in ("(", ")")]


def parse_imap_date(date_str: str) -> str | None:
    if not date_str:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S %z",):
        try:
            dt = datetime.strptime(date_str.strip('"'), fmt)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return None


def parse_fetch_response(lines: list) -> tuple[bytes | None, str, str, int]:
    """Extract (raw_email, internaldate, flags, size) from a UID FETCH response.

    aioimaplib puts the IMAP metadata header as one bytes item and the
    RFC822 literal body as the NEXT bytes item.  The header ends with
    {N} where N is the literal size.  We detect that sentinel so we do
    not mistake the metadata line itself for the email body.
    """
    raw = None
    internaldate = ""
    flags = ""
    size = 0
    next_is_body = False

    for line in lines:
        s = (
            line.decode("utf-8", errors="replace")
            if isinstance(line, (bytes, bytearray))
            else str(line)
        )

        if next_is_body:
            raw = bytes(line) if isinstance(line, (bytes, bytearray)) else s.encode()
            next_is_body = False
            continue

        # Metadata line ends with {N} — the next item is the literal body
        if re.search(r"\{\d+\}\s*$", s):
            next_is_body = True

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


# ── UID discovery (no UID SEARCH) ───────────────────────────────────────────

async def _uid_range_fetch(client, uid_range: str) -> list[int]:
    """UID FETCH <range> (UID) — valid aioimaplib UID command, returns UID list."""
    resp = await client.uid("fetch", uid_range, "(UID)")
    if resp.result != "OK":
        return []
    return _extract_uids(resp.lines)


async def _seq_to_uids(client, seq_nums: list[int]) -> list[int]:
    """Regular FETCH <seqs> (UID) — converts sequence numbers to UIDs."""
    uids: list[int] = []
    for batch in _batched(seq_nums, 100):
        seq_range = ",".join(str(s) for s in batch)
        resp = await client.fetch(seq_range, "(UID)")
        if resp.result == "OK":
            uids.extend(_extract_uids(resp.lines))
    return sorted(set(uids))


# ── Sync state persistence ───────────────────────────────────────────────────

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
    """Return last_seen_uid to use for next sync (0 = start fresh)."""
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


# ── Public entry point ───────────────────────────────────────────────────────

async def incremental_sync(client, folder: str, uidvalidity: int, last_seen_uid: int) -> int:
    """Fetch and store all emails newer than last_seen_uid. Returns new last_seen_uid."""
    if last_seen_uid == 0:
        return await _initial_sync(client, folder, uidvalidity)

    # UID FETCH range — discovers any UIDs above last_seen_uid without UID SEARCH
    uids = await _uid_range_fetch(client, f"{last_seen_uid + 1}:*")
    if not uids:
        await save_sync_state(folder, uidvalidity, last_seen_uid)
        return last_seen_uid

    logger.info("Syncing %d new email(s) in %s", len(uids), folder)
    new_max = last_seen_uid
    for uid in uids:
        try:
            if await _fetch_and_store(client, folder, uidvalidity, uid):
                new_max = max(new_max, uid)
        except Exception as exc:
            logger.error("Failed to fetch UID %d in %s: %s", uid, folder, exc)

    await save_sync_state(folder, uidvalidity, new_max)
    return new_max


async def _initial_sync(client, folder: str, uidvalidity: int) -> int:
    if config.INITIAL_SYNC_DAYS == 0:
        # Use 1:* range — Zoho (and some other servers) return nothing for bare "*"
        uids = await _uid_range_fetch(client, "1:*")
        max_uid = max(uids) if uids else 0
        await save_sync_state(folder, uidvalidity, max_uid)
        logger.info("INITIAL_SYNC_DAYS=0: baseline UID %d for %s", max_uid, folder)
        return max_uid

    since = datetime.now(timezone.utc) - timedelta(days=config.INITIAL_SYNC_DAYS)
    date_str = since.strftime("%d-%b-%Y")

    # Regular SEARCH returns sequence numbers (aioimaplib does not block this)
    resp = await client.search("SINCE", date_str)
    if resp.result != "OK":
        logger.error("SEARCH SINCE failed for %s", folder)
        return 0

    seq_nums = _parse_numbers(resp.lines[0] if resp.lines else b"")
    if not seq_nums:
        await save_sync_state(folder, uidvalidity, 0)
        return 0

    # Convert sequence numbers → UIDs via plain FETCH (UID)
    uids = await _seq_to_uids(client, seq_nums)
    if not uids:
        await save_sync_state(folder, uidvalidity, 0)
        return 0

    logger.info(
        "Initial sync: %d email(s) in last %d days for %s",
        len(uids), config.INITIAL_SYNC_DAYS, folder,
    )
    new_max = 0
    for uid in uids:
        try:
            if await _fetch_and_store(client, folder, uidvalidity, uid):
                new_max = max(new_max, uid)
        except Exception as exc:
            logger.error("Failed to fetch UID %d in %s: %s", uid, folder, exc)

    await save_sync_state(folder, uidvalidity, new_max)
    return new_max


# ── Per-message fetch + store ────────────────────────────────────────────────

async def _fetch_and_store(client, folder: str, uidvalidity: int, uid: int) -> bool:
    """Fetch one email by UID and insert into DB. Returns True if stored."""
    resp = await client.uid(
        "fetch", str(uid),
        "(RFC822 INTERNALDATE FLAGS RFC822.SIZE)",
    )
    if resp.result != "OK":
        return False

    raw, internaldate_str, flags_str, size = parse_fetch_response(resp.lines)
    if raw is None:
        return False

    if len(raw) > config.MAX_FETCH_SIZE:
        # Too large — fall back to headers only
        resp2 = await client.uid(
            "fetch", str(uid),
            "(BODY[HEADER] INTERNALDATE FLAGS RFC822.SIZE)",
        )
        raw2, internaldate_str, flags_str, size = parse_fetch_response(resp2.lines)
        if raw2 is None:
            return False
        fields, attachments = parse_message(raw2, False, False, config.MAX_FETCH_SIZE)
        fields["truncated"] = 1
    else:
        fields, attachments = parse_message(
            raw, config.FETCH_BODY, config.STORE_ATTACHMENTS, config.MAX_FETCH_SIZE,
        )

    internal_date = parse_imap_date(internaldate_str)
    flags_json = json.dumps(parse_flags(flags_str))

    conn = db.get_db()
    async with db.write_lock:
        # Cross-UIDVALIDITY dedup via Message-ID
        if fields["message_id"]:
            async with conn.execute(
                "SELECT id FROM emails WHERE message_id=? AND folder=?",
                (fields["message_id"], folder),
            ) as cur:
                if await cur.fetchone():
                    return False

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

    return bool(cur.rowcount)
