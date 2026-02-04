# 运维手册

本页覆盖部署、备份、升级、常见故障处理。

## 运行方式概览

**本地运行（开发）**
- 使用 `python web_outlook_app.py` 启动。
- 默认端口 `5000`（可通过 `PORT` 修改）。

**容器运行（生产/测试）**
- Dockerfile 默认监听 `APP_PORT=5001`。
- `docker-compose.yml` 映射 `5001:5001`。

## 必需与常用环境变量

**必需**
- `SECRET_KEY`: 用于加密/解密敏感字段与 Flask Session。必须稳定且长期不变。

**常用**
- `LOGIN_PASSWORD`: 首次初始化默认登录密码
- `DATABASE_PATH`: SQLite 路径（默认 `data/outlook_accounts.db`）
- `OAUTH_CLIENT_ID` / `OAUTH_REDIRECT_URI`
- `PORT`, `HOST`, `FLASK_ENV`

## 本地启动

```bash
export SECRET_KEY="your-secret-key"
export LOGIN_PASSWORD="admin123"
export PORT=5000
export FLASK_ENV=development
python web_outlook_app.py
```

访问：`http://localhost:5000`

## Docker Compose 启动

```bash
docker-compose up -d
```

检查日志：
```bash
docker-compose logs -f
```

访问：`http://localhost:5001`

## 数据持久化与备份

**SQLite 数据库**
- 默认位置：`data/outlook_accounts.db`
- 备份建议：

```bash
cp data/outlook_accounts.db data/outlook_accounts.db.bak
```

**注意**
- 备份与恢复必须配合同一 `SECRET_KEY`。
- 若 `SECRET_KEY` 变更，历史加密数据将无法解密。

## 升级与迁移

- 启动时自动执行 `init_db()` 和字段迁移逻辑。
- 不需要手动 SQL 迁移。
- 升级前建议先备份数据库文件。

## 运行健康检查

- Dockerfile / compose 使用 `/login` 作为健康检查端点。
- 期望返回 200，若返回非 200 说明服务不可用或初始化失败。

## 常见故障排查

**1) 启动即报错 `SECRET_KEY environment variable is required`**
- 解决：设置 `SECRET_KEY` 后重启。

**2) 登录失败次数过多**
- 服务对 IP 做速率限制，5 次失败后锁定 5 分钟。

**3) Token 刷新失败**
- 可能原因：refresh_token 失效、账号被封、Graph 访问受限。
- 建议：通过 OAuth2 助手重新获取 refresh_token。

**4) 定时刷新无效**
- APScheduler 未安装或 Cron 表达式无效。
- 检查依赖：`APScheduler`, `croniter`。

**5) 导出需要二次验证**
- 必须先调用 `/api/export/verify` 获取一次性 token。
- token 存在于 Session 内，浏览器需保持同一登录会话。

## 安全建议

- 生产环境务必使用高强度 `SECRET_KEY`。
- 不要在日志中输出 refresh_token。
- 如果需要对外访问，建议放在反向代理后并启用 HTTPS。
