# API 参考

本页整理后端 API 的请求方式与典型返回结构，方便前端或外部脚本集成。

## 通用说明

**鉴权**
- 登录成功后使用 Session Cookie 进行鉴权。
- 未登录访问 API 会返回：

```json
{"success": false, "error": "请先登录", "need_login": true}
```

**CSRF**
- 如果安装了 `flask-wtf`，所有非 GET 请求需要携带 `X-CSRFToken`。
- 前端会通过 `/api/csrf-token` 获取并自动注入。

**错误格式**
- 部分接口使用统一错误负载：

```json
{
  "success": false,
  "error": {
    "code": "ACCOUNT_NOT_FOUND",
    "message": "账号不存在",
    "type": "NotFoundError",
    "status": 404,
    "details": "email=xxx@outlook.com",
    "trace_id": "..."
  }
}
```

- 其他接口可能直接返回字符串错误：

```json
{"success": false, "error": "删除失败"}
```

**分页参数**
- 邮件列表：`skip` / `top`
- 刷新日志：`limit` / `offset`

## 认证与页面

### `GET /login`
- 返回登录页 HTML。

### `POST /login`
- 支持 `application/json` 或表单。

**请求**
```json
{"password": "your-password"}
```

**成功**
```json
{"success": true, "message": "登录成功"}
```

**失败**
```json
{"success": false, "error": "密码错误"}
```

### `GET /logout`
- 清除 Session 并跳转登录页。

### `GET /api/csrf-token`
**成功**
```json
{"csrf_token": "..."}
```

**禁用 CSRF**
```json
{"csrf_token": null, "csrf_disabled": true}
```

## 账号

### `GET /api/accounts`
- 返回安全视图（`client_id` 截断，`refresh_token` 不返回）。

**响应（节选）**
```json
{
  "success": true,
  "accounts": [
    {
      "id": 1,
      "email": "a@outlook.com",
      "client_id": "abcdef12...",
      "status": "active",
      "last_refresh_at": "2026-02-01 10:00:00",
      "tags": []
    }
  ]
}
```

### `GET /api/accounts/<id>`
- 返回完整账号详情（包含解密后的敏感字段）。

**响应（节选）**
```json
{
  "success": true,
  "account": {
    "email": "a@outlook.com",
    "password": "...",
    "client_id": "...",
    "refresh_token": "..."
  }
}
```

### `POST /api/accounts`
- 批量导入，按行解析。

**请求**
```json
{
  "account_string": "email----password----client_id----refresh_token\n..."
}
```

### `PUT /api/accounts/<id>`
- 更新账号信息。

**请求**
```json
{
  "email": "a@outlook.com",
  "password": "pwd",
  "client_id": "client",
  "refresh_token": "rt",
  "remark": "备注",
  "status": "active"
}
```

### `PUT /api/accounts/<id>`（仅更新状态）
**请求**
```json
{"status": "inactive"}
```

### `DELETE /api/accounts/<id>`
### `DELETE /api/accounts/email/<email>`

### `GET /api/accounts/search?q=keyword`
- 支持邮箱、备注、标签搜索。

## 标签

### `GET /api/tags`
### `POST /api/tags`
**请求**
```json
{"name": "VIP", "color": "#ff9900"}
```

### `DELETE /api/tags/<id>`

### `POST /api/accounts/tags`
**请求**
```json
{"account_ids": [1, 2], "tag_id": 5, "action": "add"}
```

## 邮件

### `GET /api/emails/<email>`
- 支持 `folder`（`inbox`, `junkemail`, `deleteditems`）
- 支持分页 `skip` / `top`

**响应（Graph 成功）**
```json
{
  "success": true,
  "method": "Graph API",
  "has_more": true,
  "emails": [
    {"id": "...", "subject": "...", "from": "...", "date": "..."}
  ]
}
```

**响应（全部失败）**
```json
{
  "success": false,
  "error": "无法获取邮件，所有方式均失败",
  "details": {"graph": "...", "imap_new": "...", "imap_old": "..."}
}
```

### `GET /api/email/<email>/<message_id>`
- 可带 `method=graph` 和 `folder`。

**响应（节选）**
```json
{
  "success": true,
  "email": {
    "subject": "...",
    "from": "...",
    "body_type": "html",
    "body": "..."
  }
}
```

### `POST /api/emails/delete`
**请求**
```json
{"email": "a@outlook.com", "ids": ["msg1", "msg2"]}
```

**响应**
```json
{"success": true, "success_count": 2, "failed_count": 0, "errors": []}
```

## 导出与验证

### `POST /api/export/verify`
**请求**
```json
{"password": "your-password"}
```

**成功**
```json
{"success": true, "verify_token": "..."}
```

### `GET /api/accounts/export?verify_token=...`

## Token 刷新与日志

### `POST /api/accounts/<id>/refresh`
### `POST /api/accounts/<id>/retry-refresh`
### `POST /api/accounts/refresh-failed`

### `GET /api/accounts/refresh-all`
- 返回 `text/event-stream`。
- 事件类型：`start`, `progress`, `delay`, `complete`。

**示例事件**
```
data: {"type":"progress","current":3,"total":10,"email":"a@outlook.com","success_count":2,"failed_count":0}
```

### `GET /api/accounts/trigger-scheduled-refresh`
- 触发定时刷新逻辑，同样使用 SSE。
- 可加 `?force=true` 跳过刷新周期检查。

### `GET /api/accounts/refresh-logs?limit=100&offset=0`
### `GET /api/accounts/<id>/refresh-logs`
### `GET /api/accounts/refresh-logs/failed`
### `GET /api/accounts/refresh-stats`

## OAuth2 助手

### `GET /api/oauth/auth-url`
- 返回授权 URL（用于浏览器跳转）。

### `POST /api/oauth/exchange-token`
**请求**
```json
{"code": "authorization_code"}
```

## 系统设置

### `POST /api/settings/validate-cron`
### `GET /api/settings`
### `PUT /api/settings`
- 支持更新：`login_password`, `refresh_interval_days`, `refresh_delay_seconds`, `refresh_cron`, `use_cron_schedule`, `enable_scheduled_refresh`。
