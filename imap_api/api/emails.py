import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from imap_api.security.auth import verify_token
from imap_api.storage import db

router = APIRouter()


@router.get("/emails")
async def list_emails(
    folder: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    unseen: bool = False,
    since: str | None = None,
    search: str | None = None,
    _: None = Depends(verify_token),
):
    conn = db.get_db()

    conditions: list[str] = []
    params: list = []

    if folder:
        conditions.append("folder = ?")
        params.append(folder)
    if unseen:
        conditions.append("flags NOT LIKE '%\\\\Seen%'")
    if since:
        conditions.append("internal_date >= ?")
        params.append(since)
    if search:
        conditions.append("(subject LIKE ? OR from_addr LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with conn.execute(
        f"SELECT COUNT(*) AS total FROM emails {where}", params
    ) as cur:
        total = (await cur.fetchone())["total"]

    async with conn.execute(
        f"""SELECT id, uid, folder, from_addr, to_addr, subject, internal_date, flags,
                   has_attachments, size
            FROM emails {where}
            ORDER BY internal_date DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ) as cur:
        rows = await cur.fetchall()

    return {
        "total": total,
        "emails": [
            {
                "id": r["id"],
                "uid": r["uid"],
                "folder": r["folder"],
                "from": r["from_addr"],
                "to": json.loads(r["to_addr"] or "[]"),
                "subject": r["subject"],
                "internal_date": r["internal_date"],
                "flags": json.loads(r["flags"] or "[]"),
                "has_attachments": bool(r["has_attachments"]),
                "size": r["size"],
            }
            for r in rows
        ],
    }


@router.get("/email/{email_id}")
async def get_email(email_id: int, _: None = Depends(verify_token)):
    conn = db.get_db()

    async with conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)) as cur:
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Email not found")

    async with conn.execute(
        "SELECT id, filename, content_type, size FROM attachments WHERE email_id=?",
        (email_id,),
    ) as cur:
        atts = await cur.fetchall()

    return {
        "id": row["id"],
        "uid": row["uid"],
        "folder": row["folder"],
        "message_id": row["message_id"],
        "from": row["from_addr"],
        "to": json.loads(row["to_addr"] or "[]"),
        "cc": json.loads(row["cc_addr"] or "[]"),
        "subject": row["subject"],
        "internal_date": row["internal_date"],
        "sent_date": row["sent_date"],
        "flags": json.loads(row["flags"] or "[]"),
        "body_text": row["body_text"],
        "body_html": row["body_html"],
        "has_attachments": bool(row["has_attachments"]),
        "size": row["size"],
        "truncated": bool(row["truncated"]),
        "attachments": [
            {
                "id": a["id"],
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size": a["size"],
            }
            for a in atts
        ],
    }


@router.get("/email/{email_id}/attachments/{attachment_id}")
async def get_attachment(
    email_id: int,
    attachment_id: int,
    _: None = Depends(verify_token),
):
    conn = db.get_db()

    async with conn.execute(
        "SELECT * FROM attachments WHERE id=? AND email_id=?",
        (attachment_id, email_id),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")

    if row["content"] is None:
        raise HTTPException(
            status_code=404,
            detail="Attachment body not stored (STORE_ATTACHMENTS=false).",
        )

    filename = row["filename"] or "attachment"
    content_type = row["content_type"] or "application/octet-stream"

    return Response(
        content=bytes(row["content"]),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
