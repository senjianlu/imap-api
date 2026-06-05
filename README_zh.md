# imap-api

一个可远程调用的单账号收信服务：对一个 IMAP 邮箱维持 IDLE 长连接，把新邮件实时同步进 SQLite，通过 HTTP API 只读对外提供邮件。

[English](README.md)

> ⚠️ **仅支持基础认证**
> 本服务只支持「用户名 + 密码（或授权码）」认证。
> Gmail / QQ / 163 等需使用**应用专用密码**（需先开启两步验证）。
> 若服务商已全面关闭基础认证并强制 OAuth2，则本版本不适用。

---

### 概述

`imap-api` 解决的问题是：让调用方不必自己维护 IMAP 连接，只要轮询本服务的 HTTP API 就能拿到近实时（秒级）的新邮件。

```
IMAP 服务器 ←─ IDLE 长连接 ─── imap-api worker
                                       │ 写
                                  SQLite (WAL)
                                       │ 读
                               FastAPI HTTP API
                                       │
                                  你的应用
```

单容器 = 单账号。多邮箱 = 启多个容器，各自独立 `/data` 卷与端口。

### 快速开始

镜像已发布到 Docker Hub：**[`rabbir/imap-api`](https://hub.docker.com/r/rabbir/imap-api)**

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

或使用 Docker Compose（复制 `.env.example` 为 `.env` 并填写参数）：

```bash
docker compose up -d
```

本地构建：

```bash
docker build -t rabbir/imap-api:latest .
```

### API

设置了 `IMAP_API_TOKEN` 时，所有端点需要 `Authorization: Bearer <token>`。
`IMAP_API_TOKEN` 留空则关闭认证（仅限可信内网）。

交互式文档：**`/docs`**（Swagger UI）· **`/redoc`** · **`/openapi.json`**

**GET /healthz** — 进程存活检查，返回 `{"status": "ok"}`，不代表 IMAP 已连接。

**GET /status** — 各文件夹的连接与同步状态。

**GET /emails** — 分页邮件列表（仅元数据，不含正文）。
查询参数：`folder`、`limit`（默认 50，上限 200）、`offset`、`unseen`（bool）、`since`（ISO 时间戳）、`search`（主题/发件人模糊匹配）。

**GET /email/{id}** — 单封完整内容（正文 + 附件元数据）。

**GET /email/{id}/attachments/{aid}** — 下载附件二进制。
需要 `STORE_ATTACHMENTS=true`，否则返回 404。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `IMAP_HOST` | *(必填)* | IMAP 服务器地址，如 `imap.example.com`。 |
| `IMAP_PORT` | `993` | IMAP 端口。SSL 默认 993，STARTTLS 通常 143。 |
| `IMAP_SSL` | `true` | `true` → 直接 SSL；`false` → 明文/STARTTLS。 |
| `IMAP_USERNAME` | *(必填)* | 登录账号（通常是完整邮箱地址）。 |
| `IMAP_PASSWORD` | *(必填)* | 密码或授权码。**不记录日志，不入库，不进 API 响应。** |
| `IMAP_FOLDERS` | `INBOX` | 监听的文件夹列表，逗号分隔，如 `INBOX,Sent`。每个文件夹独立一条 IDLE 连接。 |
| `IMAP_API_TOKEN` | *(空)* | HTTP API 的 Bearer Token。留空则不启用认证。 |
| `IDLE_RECONNECT_INTERVAL` | `900` | 主动重连周期（秒，默认 15 分钟）。**不建议超过 1500**。 |
| `IMAP_MAIL_RETENTION_DAYS` | `365` | 本地邮件保留天数。只删 SQLite 缓存，**绝不动服务器**。`0` 表示永久保留。 |
| `INITIAL_SYNC_DAYS` | `30` | 首次启动或 UIDVALIDITY 变更时的历史同步天数。`0` 表示只收今后新信。 |
| `FETCH_BODY` | `true` | 同步正文（text/plain + text/html）。`false` 只存信头与元数据，库更小。 |
| `STORE_ATTACHMENTS` | `false` | 将附件二进制存入 SQLite。`false` 只存附件元数据。 |
| `MAX_FETCH_SIZE` | `26214400` | 单封邮件 FETCH 体积上限（字节，默认 25 MB）。超限只取信头并标记 `truncated=true`。 |

写死常量（不可配置）：API 监听 `0.0.0.0:8000`，SQLite 路径 `/data/imap-api.db`。

### 安全

- **不走公网**。使用 WireGuard / Tailscale，不要将 `8000` 端口暴露到公网。
- **生产环境开启 `IMAP_API_TOKEN`**。
- 建议使用**应用专用密码**而非主密码，便于随时吊销。
- `IMAP_PASSWORD` 不进日志、不入库、不进 API 响应。

### 项目结构

```
imap-api/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── imap_api/
│   ├── main.py               # FastAPI 入口 + lifespan（启动各 folder worker）
│   ├── config.py             # 环境变量读取
│   ├── api/
│   │   ├── emails.py         # GET /emails、GET /email/{id}、附件下载
│   │   └── status.py         # GET /healthz、GET /status
│   ├── core/
│   │   ├── idle_worker.py    # IMAP IDLE 循环、15 分钟重连、指数退避
│   │   ├── sync.py           # UIDVALIDITY 校验 + 增量同步 + 去重入库
│   │   ├── mime.py           # RFC822 → 正文 / 附件解析
│   │   └── reaper.py         # 保留期清理（按 internal_date，只删本地）
│   ├── storage/
│   │   ├── db.py             # aiosqlite 连接、WAL、写锁
│   │   └── models.py         # CREATE TABLE 语句
│   └── security/
│       └── auth.py           # Bearer Token FastAPI 依赖
└── tests/
```
