# imap-api 开发文档

---

## 0. 一句话定位

**一个可远程调用的单账号收信服务:对一个 IMAP 邮箱维持 IDLE 长连接,把新邮件实时同步进 SQLite,通过 API 只读地对外提供邮件。**

它解决的问题是:让调用方不必自己维护 IMAP 连接、不必反复登录、不必关心 IDLE 心跳与重连补偿,只要轮询本服务的 HTTP API 就能拿到近实时(秒级)的邮件。

---

## 1. 边界(先读这节,决定项目会不会失控)

这是一个**职责单一**的收信服务,不是邮件客户端,也不是 webmail。守住边界它才能独立复用。

### 只做「把一个邮箱的来信同步出来并对外只读提供」
- ✅ 同步邮件(信头、正文、附件元数据/可选附件体)入库
- ✅ 维护同步状态(UIDVALIDITY、last_seen_uid、上次同步时间)
- ✅ 维护并暴露连接健康状态(是否在线、上次 IDLE/同步时间)
- ✅ 运行日志

### 明确不做的事(负向约束,优先级最高)
- ❌ **不发信**。SMTP 完全不在范围内,本服务只收不发。
- ❌ **不支持多账号**。单容器 = 单账号,账号信息全部来自环境变量。要监控多个邮箱 = 起多个容器,各自独立的 `/data` 卷与端口。
- ❌ **不支持 OAuth2 / XOAUTH2**。本版本只支持「用户名 + 密码(或授权码)」基础认证。Gmail / Microsoft 365 等已关闭基础认证的服务商,需用应用专用密码;若服务商强制 OAuth2 则本版本不适用(见 §8)。
- ❌ **默认不回写服务器**。不删信、不改标志、不移动邮件。本服务对邮箱是「只读观察者」。(可选的标记已读见 §7,默认关闭。)
- ❌ **本地清理 ≠ 服务器删信**。§5 的保留期清理只删本地 SQLite 缓存,**绝不**在 IMAP 服务器上删邮件。
- ❌ **不暴露凭证**。`IMAP_PASSWORD` 既不进 API 响应,也不写日志。
- ✅ **支持多文件夹**。`IMAP_FOLDERS` 为逗号分隔列表(默认 `INBOX`),每个文件夹独立一条 IDLE 长连接,互不影响。

---

## 2. 核心机制

### 2.1 单连接 IDLE + 每 15 分钟主动重连(重登)

IMAP 的 `IDLE`(RFC 2177)让连接挂起、由服务器在有新邮件时主动推 `* n EXISTS`,这是实现秒级延迟、又不高频轮询的关键。

本服务采用**固定周期的主动重连**策略(比 RFC 的「29 分钟内 DONE/重进 IDLE」更保守):

```
登录 → SELECT 文件夹 → 进入 IDLE 等待
  → 每 IDLE_RECONNECT_INTERVAL 秒(默认 900 = 15 分钟)
     DONE → LOGOUT → 关闭连接 → 重新登录
```

> ⚠️ 区分两个时限:
> - **IDLE 自身上限 ~29 分钟**:很多服务器(尤其 Gmail)会掐掉超过约 29 分钟的 IDLE。
> - **本服务策略 15 分钟全量重连**:不仅满足上述上限,还顺带定期刷新整条连接、规避 NAT/服务端的空闲连接回收,代价只是每 15 分钟一次轻量重登。

`IDLE_RECONNECT_INTERVAL` 可配,但**不建议超过 1500 秒(25 分钟)**,给 29 分钟上限留足余量。

### 2.2 重连补偿:重登后必须立刻补一次增量同步(最容易漏邮件的地方)

`DONE → LOGOUT → 重新登录 → SELECT` 之间存在一个**空窗**,这期间到达的邮件不会有 IDLE 推送。如果重登后傻等下一个 `EXISTS`,这封邮件可能延迟到下一封来信才被发现。

**铁律:每次(重)登录并 SELECT 成功后,先做一次增量同步,再进入 IDLE。** 增量同步靠比对 UID(见 §2.4),把空窗期漏掉的邮件补齐。这样空窗几乎不产生可感知延迟。

### 2.3 收到 EXISTS 只是信号,真正取信靠 UID FETCH

`* n EXISTS` 只告诉你「现在文件夹里有 n 封了」,不带内容、也不直接告诉你新邮件的 UID。收到推送后的动作是:

```
拿到 EXISTS 推送 → 触发增量同步:
  UID SEARCH / 直接 UID FETCH (UID > last_seen_uid)
  → 取回新邮件的 RFC822 原文 + INTERNALDATE + FLAGS
  → 解析 MIME → 入库 → 更新 last_seen_uid
```

> ⚠️ **一律用 UID,不要用序列号(sequence number)。** 序列号会随删信整体前移,跨连接不稳定;UID 在同一 UIDVALIDITY 内稳定且单调。`aioimaplib` 用 `uid_search` / `uid('fetch', ...)`。

### 2.4 增量同步与 UIDVALIDITY(IMAP 的核心正确性陷阱)

UID 只在「同一个 UIDVALIDITY 周期内」稳定。服务器重建邮箱索引等情况会改变 `UIDVALIDITY`,此时旧的 `last_seen_uid` **作废**,继续按它增量会错乱。

每次 `SELECT` 后拿到的响应里含 `UIDVALIDITY`,处理逻辑:

| 情况 | 行为 |
|---|---|
| 库里无记录(首次) | 记录当前 `UIDVALIDITY`,做一次初始同步(策略见下),设 `last_seen_uid` |
| 库里 `UIDVALIDITY` == 服务器当前值 | 正常增量:`UID FETCH (UID > last_seen_uid)` |
| 库里 `UIDVALIDITY` != 服务器当前值 | **判定服务器已重新编号**:旧 UID 不再可比。重置同步状态,重新做一次同步基线(以 Message-ID 去重,避免重复入库,见 §2.5) |

> **初始同步策略**(首次启动或 UIDVALIDITY 变更):全量拉历史可能很大。建议提供 `INITIAL_SYNC_DAYS`(默认只同步最近 N 天,比如 30 天)避免首次启动就把多年历史全拉下来;或干脆只取「当前最大 UID」作为基线、只同步此后的新邮件(`INITIAL_SYNC_DAYS=0` 表示「只收今后的新信」)。

### 2.5 幂等与去重

重连补偿、UIDVALIDITY 变更、推送抖动都可能让同一封邮件被取多次。**入库必须幂等**:

- 主去重键:`(uidvalidity, uid)` 加 **UNIQUE 约束**,`INSERT OR IGNORE`。
- 辅助去重:`Message-ID`(用于 UIDVALIDITY 变更后跨周期识别同一封信)。
- 切勿用「序列号」或「到达顺序」做主键。

### 2.6 连接健康探测(对应你 scraping 系统里的 heartbeat / orphan 检测)

IDLE 连接可能「看着还在,其实 socket 已死」(NAT 超时、服务端静默断开、网络抖动)。15 分钟重连兜了底,但一个周期内仍可能 15 分钟收不到新邮件。

机制:
- 给 IDLE 等待设**读超时**(如 `wait_server_push(timeout=...)`)。超时本身正常(说明这段时间没新邮件),但可借机做一次轻量探活:发 `NOOP`,失败即判定连接已死,**立刻重连(不等 15 分钟边界)**。
- 任何 IMAP 调用抛异常(连接重置、登录失效)→ 进入「指数退避重连」循环,而不是让 worker 协程崩掉。
- 退避建议:1s → 2s → 4s → … 上限封顶(如 60s),并把每次重连/失败写入状态与日志,供 `GET /status` 观测。

> 这套和你 scraping 里「心跳保活 + orphan 检测 + 死了立即重建」是同构的,迁移过来即可。

### 2.7 双层架构:IDLE worker(唯一写者) + FastAPI(只读)

把「维护长连接」与「对外提供 API」解耦成两层:

```
┌──────────────────────────────────────┐        ┌──────────────────────┐
│  IDLE workers (每 folder 一个 Task)   │  写    │   SQLite (WAL)        │
│  ├─ worker[INBOX]  登录/IDLE/同步    │ ─────▶ │   emails / sync_state │
│  ├─ worker[Sent]   登录/IDLE/同步    │        └──────────────────────┘
│  └─ ...                               │                  ▲ 只读
│  + 保留期清理(唯一写者群)            │                  │
└──────────────────────────────────────┘       ┌──────────────────────┐
                                               │  FastAPI (API 层)     │
                                               │  GET /emails 等       │
                                               └──────────────────────┘
```

- **IDLE workers**:在 FastAPI 的 `lifespan` 启动时,对 `IMAP_FOLDERS` 中的**每个文件夹**各启动一个长驻 `asyncio.Task`,每个 worker 独占一条 IMAP 连接,共同写同一个 SQLite(唯一写者群,串行写,WAL 保护)。
- **FastAPI 层**:所有端点**只读** SQLite,绝不直接碰 IMAP。
- 这是相比 mitm-api 的一个**天然简化**:写操作只有 worker 一个来源,API 全是读。配合 WAL(读写不互斥),基本不会出现 `database is locked`,也无需复杂的 writer 队列。

> 关于「双进程/线程」:推荐**同进程单事件循环**——worker 协程与 API handler 共用一个 asyncio loop,通过 SQLite 解耦。`aioimaplib` 是原生 asyncio,IDLE 等待不阻塞 API。
> 若你更看重隔离(worker 崩溃不影响 API 存活、或想独立重启 worker),可拆成**两个进程共享同一 `/data/imap-api.db`**:worker 进程写、API 进程读。此时仍要守住「只有 worker 写」这一条,WAL 下多进程读 + 单进程写是安全的。第一版建议先用同进程方案,简单且够用。

---

## 3. 同步状态抽象

整个服务围绕**一个邮箱、多个文件夹**的同步状态运转:

```
连接态:disconnected / connecting / idle / syncing  —— 实时,内存 + 暴露给 /status
同步进度:uidvalidity + last_seen_uid + last_sync_at  —— 持久化在 sync_state 表
```

### 生命周期(worker 视角)
| 阶段 | 动作 |
|---|---|
| 启动 | 读环境变量 → 连接 + 登录 → SELECT 文件夹 |
| 基线 | 检查 `UIDVALIDITY`(见 §2.4)→ 初始同步 / 增量补偿 |
| 监听 | 进入 IDLE,收到 `EXISTS` → 增量同步入库 |
| 周期重连 | 每 15 分钟 DONE→LOGOUT→重登→**补偿同步**→再 IDLE(见 §2.1/§2.2) |
| 异常恢复 | 任意 IMAP 异常 → 指数退避重连(见 §2.6) |
| 清理 | 后台周期任务按保留期物理删除过期邮件(见 §5),与 worker 同为写者,串行执行 |

---

## 4. 配置(环境变量)

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `IMAP_HOST` | *(必填)* | IMAP 服务器地址,如 `imap.example.com`。 |
| `IMAP_PORT` | `993` | IMAP 端口。SSL 默认 993,STARTTLS 通常 143。 |
| `IMAP_SSL` | `true` | `true` → 直接 SSL(993);`false` → 明文/STARTTLS(按服务器,见实现注)。 |
| `IMAP_USERNAME` | *(必填)* | 登录账号(通常是完整邮箱地址)。 |
| `IMAP_PASSWORD` | *(必填)* | 登录密码或**授权码**(QQ/163 等用授权码;Gmail 用应用专用密码)。**敏感,勿入库勿入日志。** |
| `IMAP_FOLDERS` | `INBOX` | 监听的文件夹列表,逗号分隔,如 `INBOX,Sent`。每个文件夹独立一条 IDLE 连接。 |
| `IMAP_API_TOKEN` | *(空)* | 控制 API 的 Bearer Token。**留空则不启用认证**(仅限可信内网/VPN)。 |
| `IDLE_RECONNECT_INTERVAL` | `900` | IDLE 主动重连周期(秒),默认 15 分钟。**不建议 > 1500**。 |
| `IMAP_MAIL_RETENTION_DAYS` | `365` | 本地邮件最大保留天数。超期邮件从 SQLite **物理删除**(不动服务器)。`0` 表示永不清理。 |
| `INITIAL_SYNC_DAYS` | `30` | 首次启动 / UIDVALIDITY 变更时初始同步的回溯天数。`0` 表示「只收今后新信」,不拉历史。 |
| `FETCH_BODY` | `true` | `true` → 同步正文(text/html);`false` → 只存信头与元数据,正文按需再取(更省库)。 |
| `STORE_ATTACHMENTS` | `false` | `true` → 把附件二进制存入库;`false` → 只存附件元数据(文件名/类型/大小)。见 §5 取舍。 |
| `MAX_FETCH_SIZE` | `26214400` | 单封邮件 FETCH 体积上限(字节,默认 25MB)。超限只取信头+元数据,正文/附件跳过并标记。 |

### 写死的常量(不可配置)
| 项 | 值 |
|---|---|
| 控制 API 监听 | `0.0.0.0:8000` |
| SQLite 文件路径 | `/data/imap-api.db` |

> 认证逻辑:`IMAP_API_TOKEN` 为空 → 所有端点免认证;非空 → 所有端点要求 `Authorization: Bearer <token>`。
> `/data` 为持久化目录,Docker 部署时映射到主机(见 §10)。

---

## 5. 存储(SQLite)

### 配置
- 文件写死 `/data/imap-api.db`,通过 Docker 卷映射 `/data` 持久化。
- 开 **WAL**:`PRAGMA journal_mode=WAL`(读写不互斥,API 读不挡 worker 写)。
- 用 `aiosqlite`。**唯一写者是 IDLE worker**(含保留期清理),API 全只读 → 几乎无写锁竞争。
- 启用外键:`PRAGMA foreign_keys=ON`。

### 表设计(参考)

**emails**
| 字段 | 说明 |
|---|---|
| id | INTEGER PRIMARY KEY |
| uidvalidity | INTEGER 当前 SELECT 的 UIDVALIDITY |
| uid | INTEGER IMAP UID |
| message_id | TEXT 邮件 Message-ID(辅助去重) |
| folder | TEXT 来源文件夹 |
| from_addr | TEXT 发件人 |
| to_addr | TEXT 收件人(JSON 或逗号串) |
| cc_addr | TEXT 抄送 |
| subject | TEXT 主题 |
| internal_date | TIMESTAMP 服务器 INTERNALDATE(收信时间,清理基准) |
| sent_date | TIMESTAMP 邮件 Date 头(可能不准,仅展示) |
| flags | TEXT 标志快照 JSON(\Seen 等,只读同步) |
| body_text | TEXT 纯文本正文(FETCH_BODY=false 时为空) |
| body_html | TEXT HTML 正文(同上) |
| has_attachments | INTEGER 0/1 |
| size | INTEGER 原始邮件字节数 |
| truncated | INTEGER 0/1,超 MAX_FETCH_SIZE 未取全时为 1 |
| synced_at | TIMESTAMP 入库时间 |

> **唯一约束:`UNIQUE(uidvalidity, uid)`**,入库用 `INSERT OR IGNORE` 保幂等(见 §2.5)。
> 建议索引:`internal_date`(列表排序/清理)、`message_id`(跨周期去重)。

**attachments**
| 字段 | 说明 |
|---|---|
| id | INTEGER PRIMARY KEY |
| email_id | INTEGER 外键 → emails(ON DELETE CASCADE) |
| filename | TEXT |
| content_type | TEXT |
| size | INTEGER |
| content | BLOB 仅 `STORE_ATTACHMENTS=true` 时填充,否则 NULL |

**sync_state**(单行或按 folder 一行)
| 字段 | 说明 |
|---|---|
| folder | TEXT PRIMARY KEY |
| uidvalidity | INTEGER 当前周期 |
| last_seen_uid | INTEGER 已同步到的最大 UID |
| last_sync_at | TIMESTAMP 上次成功同步时间 |

> 连接态(connecting/idle/...)、上次 IDLE 时间、最近错误等**只放内存**,供 `GET /status` 读,不必落库。

### 附件存储取舍(`STORE_ATTACHMENTS`)
- `false`(默认):只存元数据,库小。下载附件时由 API 触发一次按需 IMAP FETCH 回源(需 worker 暴露一个取件入口,或 API 侧另起短连接)。**第一版建议先 false**,避免库膨胀。
- `true`:附件 BLOB 入库,API 可直接吐字节,离线可用,但 SQLite 会随附件快速变大,注意配合 `MAX_FETCH_SIZE` 与保留期清理。

### 保留期清理(Retention Purge)
由 `IMAP_MAIL_RETENTION_DAYS` 控制,worker 内独立周期任务(建议每小时一次即可):

```sql
-- 先删附件(外键),再删邮件;以 internal_date(服务器收信时间)为基准
DELETE FROM emails
WHERE internal_date < datetime('now', '-' || ? || ' days');
-- attachments 若设了 ON DELETE CASCADE 会自动级联;否则先手动删
```

- 基准用 `internal_date`(服务器收信时间),而非发件人可伪造的 `sent_date`;无 internal_date 时回退 `synced_at`。
- **只删本地缓存,绝不在 IMAP 服务器删信**(呼应 §1 负向约束)。
- `IMAP_MAIL_RETENTION_DAYS=0` → 跳过此任务,本地永久保留。
- 清理与同步同为 worker 写操作,串行执行,避免并发写。

---

## 6. 连接与登录

### 登录方式
- 仅 **用户名 + 密码(/授权码)** 的基础认证(`LOGIN` / `AUTHENTICATE PLAIN`)。
- 不实现 OAuth2 / XOAUTH2(见 §1、§8)。
- SSL:`IMAP_SSL=true` 用 `aioimaplib.IMAP4_SSL`;`false` 用 `IMAP4`(必要时再 STARTTLS,视服务器)。

### IDLE 流程骨架(`aioimaplib`,示意)

**外层 fan-out(lifespan 启动时)**
```python
# 对每个文件夹启动独立 Task,共享同一 SQLite
for folder in IMAP_FOLDERS:          # IMAP_FOLDERS = ["INBOX", "Sent", ...]
    asyncio.create_task(idle_worker(folder))
```

**单个 folder 的 worker(指数退避外循环包住整段)**
```python
async def idle_worker(folder: str):
    while True:                                     # §2.6 指数退避重连
        try:
            client = aioimaplib.IMAP4_SSL(host=IMAP_HOST, port=IMAP_PORT)
            await client.wait_hello_from_server()
            await client.login(IMAP_USERNAME, IMAP_PASSWORD)
            select_resp = await client.select(folder)   # 解析出 UIDVALIDITY

            await reconcile_uidvalidity(folder, select_resp)  # §2.4
            await incremental_sync(folder)                    # §2.2 重登后先补偿

            deadline = loop.time() + IDLE_RECONNECT_INTERVAL
            while loop.time() < deadline:
                idle = await client.idle_start(timeout=IDLE_INNER_TIMEOUT)
                pushes = await client.wait_server_push()      # 收到 EXISTS 等
                client.idle_done()
                if has_exists(pushes):
                    await incremental_sync(folder)            # §2.3
                # 否则借机 NOOP 探活(§2.6),失败则 break 去重连

            # 周期到 → 主动重连(回到 while True 顶部)
            client.idle_done()
            await client.logout()
        except Exception:
            await exponential_backoff()             # §2.6,不让协程退出
```

> 每个文件夹的 worker 是完全独立的 asyncio Task:一个文件夹断线重连不影响其他文件夹。所有 worker 共享同一 SQLite,`sync_state` 以 `folder` 为主键区分各自进度(见 §5)。

---

## 7. API 规格

启用认证时,所有端点需 `Authorization: Bearer <token>`(`IMAP_API_TOKEN` 为空时免认证)。所有端点**只读数据库**。

### GET /healthz — 健康检查
进程存活即 200。不代表 IMAP 已连上(那看 `/status`)。

### GET /status — 连接与同步状态
```json
{
  "connection": "idle",
  "folders": ["INBOX", "Sent"],
  "uidvalidity": 1666000000,
  "last_seen_uid": 48213,
  "last_idle_at": "2026-06-05T09:31:02Z",
  "last_sync_at": "2026-06-05T09:31:02Z",
  "last_error": null,
  "total_emails": 1273
}
```

### GET /emails — 列表(分页,只返回元数据,不含正文与附件体)
查询参数(建议):`limit`(默认 50,上限 200)、`offset` 或 `before_uid`(游标式更稳)、`unseen`(仅未读)、`since`(internal_date 起始)、`search`(主题/发件人模糊,可选)。
```json
{
  "total": 1273,
  "emails": [
    {
      "id": 1290,
      "uid": 48213,
      "from": "sender@example.com",
      "subject": "出荷連携 完了通知",
      "internal_date": "2026-06-05T09:30:58Z",
      "flags": ["\\Seen"],
      "has_attachments": true,
      "size": 18452
    }
  ]
}
```

### GET /email/{id} — 单封完整内容(正文 + 附件元数据)
```json
{
  "id": 1290,
  "uid": 48213,
  "message_id": "<...@example.com>",
  "from": "sender@example.com",
  "to": ["me@example.com"],
  "cc": [],
  "subject": "出荷連携 完了通知",
  "internal_date": "2026-06-05T09:30:58Z",
  "flags": ["\\Seen"],
  "body_text": "...",
  "body_html": "<html>...</html>",
  "attachments": [
    { "id": 55, "filename": "result.csv", "content_type": "text/csv", "size": 2048 }
  ],
  "truncated": false
}
```
> `FETCH_BODY=false` 时 `body_*` 可能为空;实现可在此端点触发一次按需回源补取(可选)。

### GET /email/{id}/attachments/{aid} — 下载附件
- `STORE_ATTACHMENTS=true`:直接从库吐二进制(正确 `Content-Type` / `Content-Disposition`)。
- `false`:按需 IMAP FETCH 回源该 part 再吐;取不到则 404 并说明。

### (可选,默认关闭)PATCH /email/{id} — 标记已读
仅当显式开启「回写」能力时提供,对服务器 `STORE +FLAGS \Seen` 并更新本地 flags。**默认不启用**,保持只读边界(见 §1)。第一版可不实现。

### 交互式 API 文档(FastAPI 自带)
- **Swagger UI**:`/docs`
- **ReDoc**:`/redoc`
- **OpenAPI schema**:`/openapi.json`

在 `FastAPI(...)` 初始化保留默认 `docs_url`/`redoc_url`/`openapi_url`,README 标明这三个地址。

---

## 8. 安全

- **不走公网**。用 WireGuard / Tailscale,把 `8000` 只暴露在 VPN 接口。(你已有 Headscale 网络,直接挂上去。)
- **`IMAP_PASSWORD` 是最敏感项**:用 `.env`(不入库不进 git)或 Docker secret 注入;**禁止**写进日志、API 响应、异常堆栈。记录日志时对凭证打码。
- **生产开启 `IMAP_API_TOKEN`**,保护控制面。
- 服务商侧建议用**应用专用密码 / 授权码**而非主密码,便于随时吊销且权限最小。
- ⚠️ **OAuth2 现实提醒**:Gmail、Microsoft 365 等已普遍关闭基础认证。Gmail 需开两步验证后生成「应用专用密码」走 IMAP;部分企业租户彻底禁用基础认证则本版本无法接入,需另做 XOAUTH2(不在本版本范围)。**接入前先确认目标邮箱是否还允许密码 IMAP。**

---

## 9. 技术栈

- Python 3.11+
- FastAPI(控制面,启用交互式文档)
- asyncio
- **aioimaplib**(原生 asyncio 的 IMAP 客户端,支持 IDLE 异步等待)
- 标准库 **`email`**(`email.message_from_bytes`)解析 MIME / 正文 / 附件,无需重依赖
- SQLite(`aiosqlite`,WAL)
- Docker(部署)

> 不使用 Redis 或任何外部中间件:状态在内存 + SQLite,清理靠 worker 内周期任务。

---

## 10. Docker 部署

### 关键点
- SQLite 文件(`/data/imap-api.db`)在容器内 `/data`,通过卷映射主机目录持久化。
- 控制 API 固定 `0.0.0.0:8000`。
- 账号、服务器、各项策略全部由 §4 环境变量注入;**单容器单账号**。
- 多邮箱 = 起多个容器,各自独立 `/data` 卷与对外端口。

### Dockerfile(骨架)
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY imap_api/ ./imap_api/
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "-m", "imap_api.main"]
```

### docker-compose.yml(与你 mitm-api 同风格)
```yaml
services:
  imap-api:
    image: rabbir/imap-api:latest
    container_name: imap-api
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    environment:
      IMAP_HOST: ${IMAP_HOST}
      IMAP_PORT: ${IMAP_PORT:-993}
      IMAP_SSL: ${IMAP_SSL:-true}
      IMAP_USERNAME: ${IMAP_USERNAME}
      IMAP_PASSWORD: ${IMAP_PASSWORD}
      IMAP_FOLDERS: ${IMAP_FOLDERS:-INBOX}
      IMAP_API_TOKEN: ${IMAP_API_TOKEN:-}
      IDLE_RECONNECT_INTERVAL: ${IDLE_RECONNECT_INTERVAL:-900}
      IMAP_MAIL_RETENTION_DAYS: ${IMAP_MAIL_RETENTION_DAYS:-365}
      INITIAL_SYNC_DAYS: ${INITIAL_SYNC_DAYS:-30}
      FETCH_BODY: ${FETCH_BODY:-true}
      STORE_ATTACHMENTS: ${STORE_ATTACHMENTS:-false}
      MAX_FETCH_SIZE: ${MAX_FETCH_SIZE:-26214400}
```

> `IMAP_PASSWORD` 等敏感值放 `.env`(同目录、不进版本库),compose 自动读取。
> 多账号场景复制本服务为 `imap-api-2`、`imap-api-3` 等,各自改 `container_name`、对外端口与 `./data-N` 卷。

---

## 11. 建议项目结构

```
imap-api/
├── README.md                  # 含 OAuth2/基础认证限制说明 + /docs 地址 + 单账号边界
├── Dockerfile
├── docker-compose.yml
├── .env.example               # 列全部环境变量(密码留空示例)
├── pyproject.toml
├── imap_api/
│   ├── __init__.py
│   ├── main.py                # FastAPI app 入口(写死 0.0.0.0:8000),lifespan 拉起 IDLE worker
│   ├── config.py              # 读取环境变量
│   ├── api/
│   │   ├── emails.py          # GET /emails、GET /emails/{id}、附件下载
│   │   └── status.py          # GET /status、/healthz
│   ├── core/
│   │   ├── idle_worker.py     # 登录/IDLE/15min 重连/异常退避(唯一写者入口)
│   │   ├── sync.py            # UIDVALIDITY 校验 + 增量同步 + 去重入库(§2.4/§2.5)
│   │   ├── mime.py            # RFC822 原文 → 正文/附件/信头解析
│   │   └── reaper.py          # 保留期清理(物理删除,internal_date 基准)
│   ├── storage/
│   │   ├── db.py              # aiosqlite 连接、WAL、外键、唯一写者
│   │   └── models.py          # emails / attachments / sync_state
│   └── security/
│       └── auth.py            # 控制 API 的 Token 校验(空 Token 放行)
└── tests/
```

> IDLE worker 与 API 共享同一 SQLite;worker 唯一写,API 只读。worker 内部状态(连接态/最近错误)通过一个内存对象暴露给 `/status`。

---

## 12. 开发顺序建议

1. **先跑通最小同步**:硬编码账号,`aioimaplib` 登录 + SELECT + 一次性 `UID FETCH` 全部 → `email` 解析 → 打印。确认 MIME/正文/附件解析正确。
2. 加 `storage`:建表(emails/attachments/sync_state)、WAL、`INSERT OR IGNORE` 幂等入库。
3. 加 `sync.py`:实现 UIDVALIDITY 校验 + 基于 `last_seen_uid` 的增量同步 + `INITIAL_SYNC_DAYS` 初始基线。
4. 加 `idle_worker.py`:IDLE 等待 + 收到 `EXISTS` 触发增量;**重登后先补偿同步**(§2.2);15 分钟周期重连;异常指数退避(§2.6)。
5. 加 `config.py`:读取全部环境变量(API 端口与 DB 路径写死)。
6. 套 FastAPI(写死 `0.0.0.0:8000`,lifespan 启动 worker):`/healthz`、`/status`、`GET /emails`、`GET /email/{id}`、附件下载;启用 `/docs`。
7. 加 `reaper.py`:保留期物理清理(`IMAP_MAIL_RETENTION_DAYS`,internal_date 基准,`=0` 跳过),与 worker 串行写。
8. 加控制面 Token 认证(`IMAP_API_TOKEN` 空则放行)。
9. 写 Dockerfile + compose + `.env.example`(含 `IMAP_FOLDERS` 多文件夹示例),验证 `/data` 持久化、断网重连、容器重启后增量不重复。
10. 写 README(重点:**基础认证/OAuth2 限制、单账号单容器边界、`IMAP_FOLDERS` 多文件夹用法、本地清理≠服务器删信、`/docs` 地址、Docker 部署**)。

> 让 AI 协助时:先让它输出**目录结构 + 表设计 + UIDVALIDITY/增量同步状态机**确认,再分模块写实现。把 §1 的负向约束放在提示词最前面。

---

## 附:与 mitm-api 的设计差异速查

| 维度 | mitm-api | imap-api |
|---|---|---|
| 实例数 | 单进程多 mode(运行时增删) | 单账号单连接(无 mode 概念) |
| 端口管理 | 范围分配 + 绑定确认 + 回滚 | 无,固定 8000 |
| 数据面认证 | 按 session proxyauth(自校验) | 无数据面;IMAP 凭证走环境变量 |
| 写者 | capture addon | IDLE worker(唯一写者) |
| API 性质 | 读写(创建/销毁 session) | **纯只读**(收信) |
| 主要陷阱 | 异步 bind 竞态、端口泄漏 | **UIDVALIDITY、重连补偿、序列号 vs UID** |
| 清理 | TTL 回收 + 保留期物理删除 | 保留期物理删除(默认 365 天,不动服务器) |