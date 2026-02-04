# Outlook 邮件管理工具 - 项目分析

本文档基于仓库当前代码进行结构化分析，覆盖架构、模块、数据模型、配置、API 与安全机制，便于维护与二次开发。

**最后更新**
- 2026-02-04

**项目定位**
- 提供一个 Web 管理界面，集中管理多个 Outlook 邮箱账号并查看邮件内容。
- 支持 Microsoft Graph API + IMAP（新/旧）三种读取方式，具备账号分组、标签管理、导出、定时刷新、临时邮箱等能力。

**相关文档**
- `ARCHITECTURE.md`：组件架构图与关键数据流
- `API_REFERENCE.md`：API 参考（请求与典型响应）
- `OPERATIONS.md`：部署与运维手册

## 技术栈与依赖

**后端**
- Python 3.11
- Flask（Web 框架）
- SQLite（本地数据库）
- requests（外部 API 请求）
- cryptography + bcrypt（敏感数据加密与密码哈希）
- APScheduler + croniter（定时刷新与 Cron 校验）
- flask-wtf（CSRF 保护，可选但已列为依赖）

**前端**
- 原生 HTML/CSS/JS（模板：`templates/index.html`, `templates/login.html`）
- DOMPurify（HTML 邮件内容净化）

**部署**
- Docker / Docker Compose
- Gunicorn（容器内默认启动方式）

## 目录结构

- `web_outlook_app.py`: 主应用（Flask + API + DB + OAuth + 任务调度）
- `outlook_mail_reader.py`: 独立命令行测试工具（IMAP/Graph 读取）
- `templates/index.html`: 主界面（四栏布局 + 大量前端逻辑）
- `templates/login.html`: 登录页面
- `requirements.txt`: Python 依赖
- `Dockerfile`: 容器镜像构建脚本
- `docker-compose.yml`: 运行示例
- `img/`: 文档截图资源

## 运行与配置

**必需环境变量**
- `SECRET_KEY`: 加密密钥与 Flask 会话密钥来源，缺失会直接报错并停止启动。

**常用环境变量**
- `LOGIN_PASSWORD`: 默认登录密码（首次初始化会写入数据库）
- `DATABASE_PATH`: SQLite 路径，默认 `data/outlook_accounts.db`
- `GPTMAIL_BASE_URL`: 临时邮箱 API 地址，默认 `https://mail.chatgpt.org.uk`
- `GPTMAIL_API_KEY`: 临时邮箱 API Key
- `OAUTH_CLIENT_ID`: OAuth 助手默认 client_id
- `OAUTH_REDIRECT_URI`: OAuth 回调地址
- `PORT` / `HOST` / `FLASK_ENV`: 本地启动配置

**Docker 运行**
- `docker-compose.yml` 默认使用 Gunicorn 监听 `5001`。
- 数据持久化目录：`./data` -> `/app/data`。

## 数据模型（SQLite）

**核心表**

| 表名 | 作用 | 关键字段 |
| --- | --- | --- |
| `settings` | 全局配置 | `key`, `value`, `updated_at` |
| `groups` | 邮箱分组 | `id`, `name`, `color`, `is_system` |
| `accounts` | 邮箱账号 | `email`, `password`, `client_id`, `refresh_token`, `group_id`, `status`, `last_refresh_at` |
| `tags` | 标签 | `name`, `color` |
| `account_tags` | 账号-标签关联 | `account_id`, `tag_id` |
| `account_refresh_logs` | 刷新日志 | `account_id`, `refresh_type`, `status`, `error_message` |
| `audit_logs` | 审计日志 | `action`, `resource_type`, `details`, `user_ip` |

**临时邮箱相关表**

| 表名 | 作用 | 关键字段 |
| --- | --- | --- |
| `temp_emails` | 临时邮箱账户 | `email`, `status` |
| `temp_email_messages` | 临时邮箱邮件 | `message_id`, `email_address`, `subject`, `content`, `html_content`, `timestamp` |

**数据安全**
- `accounts.password` 与 `accounts.refresh_token` 会使用 Fernet 加密后存储（`enc:` 前缀）。
- 登录密码存储为 bcrypt 哈希（settings 表的 `login_password`）。

## 核心模块解析

**1) Web 主服务（`web_outlook_app.py`）**
- 入口即初始化：创建数据目录、建表、迁移加密字段、创建默认分组。
- 提供完整 API 层，前端通过 JSON 调用。
- 内置 OAuth2 授权流程助手，简化 refresh token 获取。

**2) 邮件读取流程（Graph + IMAP）**
- 优先 Graph API 获取邮件列表/详情。
- Graph 失败时自动回退到 IMAP 新服务器 `outlook.live.com`。
- 再失败则尝试旧服务器 `outlook.office365.com`。
- Graph 列表支持分页 `skip/top`；IMAP 分页为简化实现，`has_more` 固定为 `false`。

**3) Token 刷新与日志**
- 单账号刷新：`/api/accounts/<id>/refresh`。
- 全量刷新：`/api/accounts/refresh-all`，使用 SSE 流式返回进度。
- 失败重试：`/api/accounts/refresh-failed`。
- 刷新结果记录到 `account_refresh_logs`，并更新 `accounts.last_refresh_at`。

**4) 定时刷新调度**
- APScheduler 后台调度，支持两种策略：
- 固定天数间隔检查（默认每日 2:00 检查）。
- 自定义 Cron 表达式（`refresh_cron`）。

**5) 邮箱管理与分组标签**
- 分组 CRUD + 颜色管理。
- 邮箱账号 CRUD，支持批量导入（格式：`email----password----client_id----refresh_token`）。
- 标签管理与账号标签关联。

**6) 导出与审计**
- 支持按分组、全部、选中分组导出账号信息（TXT）。
- 导出需要二次验证（一次性 token）。
- 导出操作写入 `audit_logs`。

**7) 临时邮箱（GPTMail）**
- 调用 GPTMail API 获取/删除临时邮箱邮件。
- 邮件会落地到本地数据库，支持刷新与缓存展示。

**8) 前端（`templates/index.html`）**
- 四栏布局：分组、邮箱、邮件列表、邮件详情。
- 邮件详情通过 iframe 展示 HTML 内容，并用 DOMPurify 清理。
- “信任邮件模式”可绕过净化，但有明确确认提示。
- 邮件列表本地缓存（账号 + 文件夹维度），提高切换速度。
- 支持全屏查看邮件与滚动加载。

**9) 命令行测试工具（`outlook_mail_reader.py`）**
- 提供三种读取方式的独立测试入口，便于调试 Access Token 与 IMAP 连通性。

## API 概览

**认证与页面**
- `GET/POST /login`
- `GET /logout`
- `GET /`
- `GET /api/csrf-token`

**分组**
- `GET /api/groups`
- `GET /api/groups/<id>`
- `POST /api/groups`
- `PUT /api/groups/<id>`
- `DELETE /api/groups/<id>`
- `GET /api/groups/<id>/export`

**账号**
- `GET /api/accounts`
- `GET /api/accounts/<id>`
- `POST /api/accounts`
- `PUT /api/accounts/<id>`
- `DELETE /api/accounts/<id>`
- `DELETE /api/accounts/email/<email>`
- `GET /api/accounts/search`

**标签**
- `GET /api/tags`
- `POST /api/tags`
- `DELETE /api/tags/<id>`
- `POST /api/accounts/tags`

**邮件**
- `GET /api/emails/<email>`
- `GET /api/email/<email>/<message_id>`
- `POST /api/emails/delete`

**导出**
- `GET /api/accounts/export`
- `POST /api/accounts/export-selected`
- `POST /api/export/verify`

**Token 刷新与日志**
- `POST /api/accounts/<id>/refresh`
- `GET /api/accounts/refresh-all`
- `POST /api/accounts/<id>/retry-refresh`
- `POST /api/accounts/refresh-failed`
- `GET /api/accounts/trigger-scheduled-refresh`
- `GET /api/accounts/refresh-logs`
- `GET /api/accounts/<id>/refresh-logs`
- `GET /api/accounts/refresh-logs/failed`
- `GET /api/accounts/refresh-stats`

**临时邮箱**
- `GET /api/temp-emails`
- `POST /api/temp-emails/generate`
- `DELETE /api/temp-emails/<email>`
- `GET /api/temp-emails/<email>/messages`
- `GET /api/temp-emails/<email>/messages/<message_id>`
- `DELETE /api/temp-emails/<email>/messages/<message_id>`
- `DELETE /api/temp-emails/<email>/clear`
- `POST /api/temp-emails/<email>/refresh`

**OAuth2 助手**
- `GET /api/oauth/auth-url`
- `POST /api/oauth/exchange-token`

**系统设置**
- `POST /api/settings/validate-cron`
- `GET /api/settings`
- `PUT /api/settings`

## 安全机制概览

- 登录密码使用 bcrypt 哈希。
- Refresh Token 与邮箱密码加密存储（Fernet）。
- 可选 CSRF 防护（`flask-wtf` 可用时启用）。
- XSS 防护：前端 DOMPurify + iframe sandbox；后端输入净化。
- 登录失败次数限制（按 IP 记录）。
- 导出功能二次验证与审计日志记录。
- 统一错误负载（带 `trace_id`）与敏感信息脱敏。

## 观察与注意事项

- IMAP 读取为 Graph 失败后的兜底方案，分页能力有限。
- 批量删除目前仅支持 Graph API，IMAP 删除逻辑未完整实现。
- 定时刷新依赖 APScheduler 与 croniter，缺失会自动降级并提示。

## 快速理解系统运行流程

1. 启动时初始化数据库与默认分组。
2. 登录后加载分组与账号信息。
3. 选择账号后调用邮件 API 获取列表。
4. 邮件详情通过 Graph/IMAP 拉取内容并在前端渲染。
5. 刷新任务可手动触发或由调度器定时执行。
