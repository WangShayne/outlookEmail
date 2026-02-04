# Outlook 邮件管理工具

一个完整的 Outlook 邮箱管理与查看平台，支持多账号与标签管理，集成 Graph API + IMAP 的邮件读取能力，并提供 Web 界面与定时刷新机制。

## 功能概览

- 多账号管理：批量导入、备注、启用/停用
- 邮件读取：Graph API 优先，IMAP 新/旧服务器兜底
- 邮件列表与详情：分页、全屏查看、HTML 安全净化
- 标签系统：账号打标签与筛选
- Token 刷新：单个/全量刷新、失败重试、刷新日志
- 安全机制：bcrypt 密码哈希、Fernet 数据加密、CSRF/XSS 防护、审计日志

## 文档索引

- 项目分析：`PROJECT_ANALYSIS.md`
- 架构与数据流：`ARCHITECTURE.md`
- API 参考：`API_REFERENCE.md`
- 运维手册：`OPERATIONS.md`

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
cp .env.example .env  # 如果你有模板，可自行创建
# 必须设置 SECRET_KEY

# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f
```

默认访问：`http://localhost:5001`

### 方式二：Docker（直接运行）

```bash
docker build -t outlook-email .

docker run -d \
  --name outlook-email \
  -p 5001:5001 \
  -e SECRET_KEY=your-secret-key \
  -e LOGIN_PASSWORD=admin123 \
  -v $(pwd)/data:/app/data \
  outlook-email
```

### 方式三：本地运行（开发）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY=your-secret-key
export LOGIN_PASSWORD=admin123
export PORT=5000
export FLASK_ENV=development

python web_outlook_app.py
```

默认访问：`http://localhost:5000`

## 环境变量

**必需**
- `SECRET_KEY`：加密与 Session 的核心密钥，必须稳定且不可变。

**常用**
- `LOGIN_PASSWORD`：首次初始化默认登录密码
- `DATABASE_PATH`：SQLite 路径（默认 `data/outlook_accounts.db`）
- `OAUTH_CLIENT_ID` / `OAUTH_REDIRECT_URI`
- `PORT` / `HOST` / `FLASK_ENV`

## 数据存储

- 使用 SQLite 持久化账号/标签/日志数据
- 默认数据库：`data/outlook_accounts.db`
- 账号敏感字段加密存储（Fernet）

## 安全要点

- 登录密码使用 bcrypt 哈希
- refresh_token / password 加密存储
- 可选 CSRF 保护（flask-wtf 安装时自动启用）
- DOMPurify + iframe sandbox 防 XSS
- 导出操作二次验证 + 审计日志

## 已知限制

- IMAP 为 Graph 失败后的兜底方案，分页能力有限
- 批量删除邮件仅支持 Graph API

## 截图预览

> 详细截图请参考 `img/` 目录。

---

如需更完整的接口说明与运维细节，请阅读文档索引中的文件。
