import email as email_lib
import email.policy
import json
from datetime import timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    result = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(chunk)
    return "".join(result)


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def parse_message(
    raw: bytes,
    fetch_body: bool,
    store_attachments: bool,
    max_fetch_size: int,
) -> tuple[dict, list[dict]]:
    """Parse RFC822 bytes. Returns (email_fields dict, attachments list)."""
    truncated = len(raw) > max_fetch_size

    msg = email_lib.message_from_bytes(raw, policy=email_lib.policy.compat32)

    subject = _decode_header(msg.get("Subject"))
    from_addr = msg.get("From", "")
    to_addrs = json.dumps([a.strip() for a in (msg.get("To") or "").split(",") if a.strip()])
    cc_addrs = json.dumps([a.strip() for a in (msg.get("Cc") or "").split(",") if a.strip()])
    message_id = (msg.get("Message-ID") or "").strip()
    sent_date = _parse_date(msg.get("Date"))

    body_text = ""
    body_html = ""
    attachments: list[dict] = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            filename = _decode_header(part.get_filename() or "")

            if filename or "attachment" in disp:
                payload = part.get_payload(decode=True) or b""
                attachments.append({
                    "filename": filename or "attachment",
                    "content_type": ct,
                    "size": len(payload),
                    "content": payload if store_attachments else None,
                })
            elif ct == "text/plain" and fetch_body and not body_text:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                body_text = payload.decode(charset, errors="replace")
            elif ct == "text/html" and fetch_body and not body_html:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                body_html = payload.decode(charset, errors="replace")
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        if ct == "text/plain" and fetch_body:
            body_text = payload.decode(charset, errors="replace")
        elif ct == "text/html" and fetch_body:
            body_html = payload.decode(charset, errors="replace")

    fields = {
        "message_id": message_id,
        "from_addr": from_addr,
        "to_addr": to_addrs,
        "cc_addr": cc_addrs,
        "subject": subject,
        "sent_date": sent_date,
        "body_text": body_text if fetch_body else None,
        "body_html": body_html if fetch_body else None,
        "has_attachments": 1 if attachments else 0,
        "truncated": 1 if truncated else 0,
    }

    return fields, attachments
