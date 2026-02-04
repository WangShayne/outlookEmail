# 架构与数据流

本页聚焦系统组件关系与关键业务数据流，配合 `PROJECT_ANALYSIS.md` 使用。

## 组件架构

```mermaid
flowchart LR
    Browser["Browser (Admin UI)"] -->|"HTTP/JSON + Session Cookie"| Flask["Flask App (web_outlook_app.py)"]
    Flask -->|"SQL"| SQLite[("SQLite DB")]
    Flask -->|"Graph API"| Graph["Microsoft Graph"]
    Flask -->|"IMAP OAuth2"| IMAP["Outlook IMAP (live/office365)"]
    Flask -->|"HTTP"| GPTMail["GPTMail API"]
    Scheduler["APScheduler"] -->|"call"| Flask
```

## 数据流一：登录与会话

```mermaid
flowchart TD
    A["Browser: POST /login"] --> B["Flask Login Handler"]
    B --> C["SQLite: settings.login_password"]
    C --> D["bcrypt verify"]
    D -->|"ok"| E["session.logged_in = true"]
    D -->|"fail"| F["rate limit record"]
    E --> G["JSON success + session cookie"]
```

## 数据流二：获取邮件列表（Graph 优先）

```mermaid
flowchart TD
    A["Browser: GET /api/emails/<email>"] --> B["load account from DB"]
    B --> C["Graph API: list messages"]
    C -->|"success"| D["format + return emails"]
    C -->|"fail"| E["IMAP new server"]
    E -->|"success"| D
    E -->|"fail"| F["IMAP old server"]
    F -->|"success"| D
    F -->|"fail"| G["return aggregated errors"]
```

## 数据流三：邮件详情

```mermaid
flowchart TD
    A["Browser: GET /api/email/<email>/<message_id>"] --> B["Graph API: message detail"]
    B -->|"success"| C["return detail"]
    B -->|"fail"| D["IMAP detail"]
    D -->|"success"| C
    D -->|"fail"| E["return error"]
```

## 数据流四：Token 刷新

**手动刷新（单账号）**

```mermaid
flowchart TD
    A["Browser: POST /api/accounts/<id>/refresh"] --> B["decrypt refresh_token"]
    B --> C["Graph token endpoint"]
    C --> D["log to account_refresh_logs"]
    D --> E["update accounts.last_refresh_at"]
    E --> F["JSON result"]
```

**全量刷新（SSE）**

```mermaid
flowchart TD
    A["Browser: GET /api/accounts/refresh-all"] --> B["open SSE stream"]
    B --> C["loop accounts + delay"]
    C --> D["Graph token endpoint"]
    D --> E["write refresh log"]
    E --> F["SSE progress events"]
```

**定时刷新**

```mermaid
flowchart TD
    A["APScheduler"] --> B["scheduled_refresh_task"]
    B --> C["check interval/cron"]
    C --> D["trigger_refresh_internal"]
    D --> E["update logs + last_refresh_at"]
```

## 数据流五：导出账号

```mermaid
flowchart TD
    A["Browser: POST /api/export/verify"] --> B["verify password"]
    B --> C["session.export_verify_token"]
    C --> D["Browser: GET /api/accounts/export?verify_token=..."]
    D --> E["load accounts + decrypt"]
    E --> F["audit_logs write"]
    F --> G["download txt"]
```

## 前端缓存边界

- 邮件列表缓存存在浏览器内存（按 `account + folder` 维度）。
- 后端邮件 API 不做缓存，每次请求都会向 Graph/IMAP 获取。

## 关键依赖边界

- `SECRET_KEY` 一旦变更，将导致已加密的 `refresh_token`/`password` 无法解密。
- 生产场景建议将 DB 与 `SECRET_KEY` 一同持久化与备份。

