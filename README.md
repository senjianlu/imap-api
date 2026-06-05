# imap-api

A remotely-callable single-account mail sync service: maintains persistent IMAP IDLE connections, syncs incoming mail into SQLite in near real-time, and exposes a read-only HTTP API.

[中文文档](README_zh.md)

> ⚠️ **Basic-auth only**
> This service uses username + password (or app-specific password) authentication only.
> Gmail and Microsoft 365 require an **app-specific password** (enable 2FA first).
> Providers that have fully disabled basic auth require OAuth2, which is out of scope for this version.

---

### Overview

`imap-api` solves the problem of keeping an IMAP connection alive so callers don't have to. Point it at a mailbox and poll the HTTP API for near real-time (second-level) mail delivery.

```
IMAP server ←─ IDLE long connection ─── imap-api worker
                                                │ writes
                                           SQLite (WAL)
                                                │ reads
                                         FastAPI HTTP API
                                                │
                                         Your application
```

One container = one account. Multiple mailboxes = multiple containers with separate `/data` volumes and ports.

### Quick Start

The image is published to Docker Hub: **[`rabbir/imap-api`](https://hub.docker.com/r/rabbir/imap-api)**

```bash
docker run -d --name imap-api \
  -p 8000:8000 \
  -v /srv/imap-api/data:/data \
  -e IMAP_HOST=imap.example.com \
  -e IMAP_USERNAME=you@example.com \
  -e IMAP_PASSWORD=your-app-password \
  -e IMAP_API_TOKEN=your-secret-token \
  rabbir/imap-api:latest
```

Or with Docker Compose (copy `.env.example` to `.env` and fill in the values):

```bash
docker compose up -d
```

Build locally:

```bash
docker build -t rabbir/imap-api:latest .
```

### API

All endpoints require `Authorization: Bearer <IMAP_API_TOKEN>` when `IMAP_API_TOKEN` is set.
Leave `IMAP_API_TOKEN` empty to disable authentication (trusted network only).

Interactive docs: **`/docs`** (Swagger UI) · **`/redoc`** · **`/openapi.json`**

**GET /healthz** — Process liveness. Returns `{"status": "ok"}`. Does not check IMAP connectivity.

**GET /status** — Per-folder connection and sync state.

```json
{
  "folders": {
    "INBOX": {
      "connection": "idle",
      "uidvalidity": 1666000000,
      "last_seen_uid": 48213,
      "last_idle_at": "2026-06-05T09:31:02Z",
      "last_sync_at": "2026-06-05T09:31:02Z",
      "last_error": null,
      "total_emails": 1273
    }
  }
}
```

**GET /emails** — Paginated list (metadata only, no body).

Query params: `folder`, `limit` (default 50, max 200), `offset`, `unseen` (bool), `since` (ISO timestamp), `search` (subject/from fuzzy).

**GET /email/{id}** — Full email including body and attachment metadata.

**GET /email/{id}/attachments/{aid}** — Download attachment binary.
Requires `STORE_ATTACHMENTS=true`; returns 404 with explanation otherwise.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IMAP_HOST` | *(required)* | IMAP server hostname, e.g. `imap.example.com`. |
| `IMAP_PORT` | `993` | IMAP port. SSL default 993, STARTTLS typically 143. |
| `IMAP_SSL` | `true` | `true` → direct SSL; `false` → plain/STARTTLS. |
| `IMAP_USERNAME` | *(required)* | Login name (usually the full email address). |
| `IMAP_PASSWORD` | *(required)* | Password or app-specific password. Never logged or returned by the API. |
| `IMAP_FOLDERS` | `INBOX` | Comma-separated list of folders to monitor, e.g. `INBOX,Sent`. One IDLE connection per folder. |
| `IMAP_API_TOKEN` | *(empty)* | Bearer token for the HTTP API. Empty = no auth (trusted network only). |
| `IDLE_RECONNECT_INTERVAL` | `900` | Proactive reconnect interval in seconds (default 15 min). Do not exceed 1500. |
| `IMAP_MAIL_RETENTION_DAYS` | `365` | Local retention in days. Purges SQLite only — never touches the server. `0` = keep forever. |
| `INITIAL_SYNC_DAYS` | `30` | Days of history to sync on first start or after UIDVALIDITY change. `0` = only future mail. |
| `FETCH_BODY` | `true` | Sync plain-text and HTML body. `false` = headers + metadata only (smaller DB). |
| `STORE_ATTACHMENTS` | `false` | Store attachment binaries in SQLite. `false` = metadata only. |
| `MAX_FETCH_SIZE` | `26214400` | Per-message FETCH size cap (bytes, default 25 MB). Oversized messages store headers only and are flagged `truncated=true`. |

Fixed constants (not configurable): API on `0.0.0.0:8000`, SQLite at `/data/imap-api.db`.

### Security

- **Run on a private network.** Use WireGuard/Tailscale; do not expose port `8000` to the public internet.
- **Enable `IMAP_API_TOKEN`** in production.
- **Use an app-specific password** rather than your main account password — easier to revoke and limits blast radius.
- `IMAP_PASSWORD` is never written to logs, the database, or API responses.

### Project Structure

```
imap-api/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── imap_api/
│   ├── main.py               # FastAPI app + lifespan (starts workers)
│   ├── config.py             # Environment variable parsing
│   ├── api/
│   │   ├── emails.py         # GET /emails, GET /email/{id}, attachment download
│   │   └── status.py         # GET /healthz, GET /status
│   ├── core/
│   │   ├── idle_worker.py    # IMAP IDLE loop, 15-min reconnect, exponential backoff
│   │   ├── sync.py           # UIDVALIDITY reconciliation + incremental sync + dedup
│   │   ├── mime.py           # RFC822 → body / attachment parsing
│   │   └── reaper.py         # Retention purge (internal_date basis, SQLite only)
│   ├── storage/
│   │   ├── db.py             # aiosqlite connection, WAL, write lock
│   │   └── models.py         # CREATE TABLE statements
│   └── security/
│       └── auth.py           # Bearer token FastAPI dependency
└── tests/
```
