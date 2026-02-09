# 故障排查指南

## 问题：注册机器人获取邮件时返回 404 错误

### 错误信息
```
[2026-02-09 11:00:10] [WARN] 获取邮件失败: HTTP 404
```

### 原因分析

这个问题发生的原因是：

1. **租约过期** - 默认租约时长为 900 秒（15分钟）
2. **模拟服务器不发送真实邮件** - `mock_registration_server.py` 只是打印验证码到控制台，不会真的发送邮件到 Outlook 账号
3. **机器人一直等待** - 因为没有真实邮件，机器人会一直轮询直到超时或租约过期
4. **租约过期后返回 404** - 当租约过期被自动清理后，API 返回 404

### 解决方案

#### 方案 1：使用模拟服务器测试（推荐用于开发）

模拟服务器不会发送真实邮件，所以需要手动模拟整个流程：

```python
# 创建一个简化的测试脚本
from examples.registration_bot import RegistrationBot
import requests

bot = RegistrationBot(
    outlook_api_base="http://localhost:5001",
    outlook_api_key="your-secret-key",
    registration_api_base="http://localhost:5002"
)

# 1. 领取邮箱
bot.checkout_email()
print(f"领取邮箱: {bot.email}")

# 2. 注册账号
bot.register_account("testuser")
print(f"会话ID: {bot.session_id}")

# 3. 从模拟服务器控制台获取验证码（手动）
# 模拟服务器会打印: [模拟] 验证码: 123456
code = input("请输入验证码: ")

# 4. 提交验证码
bot.verify_code(code)

# 5. 释放邮箱
bot.release_email()
```

#### 方案 2：配置真实 SMTP 发送邮件

修改 `examples/mock_registration_server.py`，取消注释 SMTP 代码：

```python
# 在 send_verification_email 函数中
smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
smtp_port = int(os.getenv("SMTP_PORT", "587"))
smtp_user = os.getenv("SMTP_USER")
smtp_password = os.getenv("SMTP_PASSWORD")

# 配置环境变量
export SMTP_SERVER=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=your-email@gmail.com
export SMTP_PASSWORD=your-app-password
```

然后重启模拟服务器，这样验证邮件会真实发送到 Outlook 账号。

#### 方案 3：增加租约时长

如果需要更长的测试时间，可以增加租约时长：

```python
# 在 registration_bot.py 中
bot.checkout_email(ttl_seconds=3600)  # 1小时
```

或者在 API 调用时：

```bash
curl -X POST http://localhost:5001/api/external/checkout \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"owner": "test", "ttl_seconds": 3600}'
```

#### 方案 4：跳过邮件验证（仅测试用）

创建一个测试脚本，跳过邮件验证步骤：

```python
from examples.registration_bot import RegistrationBot

bot = RegistrationBot(
    outlook_api_base="http://localhost:5001",
    outlook_api_key="your-secret-key",
    registration_api_base="http://localhost:5002"
)

# 测试 API 连接
if bot.checkout_email():
    print(f"✓ 成功领取邮箱: {bot.email}")
    print(f"✓ 租约ID: {bot.lease_id}")
    
    # 直接释放，不进行注册
    bot.release_email()
    print("✓ 测试完成")
```

### 最佳实践

#### 开发环境
1. **使用方案 1** - 手动输入验证码进行测试
2. **使用方案 4** - 仅测试 API 连接，不测试完整流程

#### 生产环境
1. **使用方案 2** - 配置真实 SMTP 服务器
2. **监控租约使用** - 定期清理过期租约
3. **调整租约时长** - 根据实际注册流程耗时调整

### 清理过期租约

```bash
# 手动清理
sqlite3 data/outlook_accounts.db "DELETE FROM account_leases WHERE expires_at <= datetime('now')"

# 查看当前租约
sqlite3 data/outlook_accounts.db "SELECT * FROM account_leases"

# 查看可用邮箱
sqlite3 data/outlook_accounts.db "SELECT COUNT(*) FROM accounts WHERE status='active' AND id NOT IN (SELECT account_id FROM account_leases)"
```

### 监控和调试

#### 查看审计日志
```bash
sqlite3 data/outlook_accounts.db "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 20"
```

#### 查看租约历史
```bash
sqlite3 data/outlook_accounts.db "SELECT action, resource_id, details, created_at FROM audit_logs WHERE action IN ('checkout', 'checkout_complete') ORDER BY created_at DESC LIMIT 10"
```

#### 实时监控
```bash
# 监控可用邮箱数量
watch -n 5 'sqlite3 data/outlook_accounts.db "SELECT COUNT(*) FROM accounts WHERE status='\''active'\'' AND id NOT IN (SELECT account_id FROM account_leases)"'
```

## 其他常见问题

### 问题：租约已过期 (410 错误)

**错误**: `{"success": false, "error": "租约已过期"}`

**原因**: 租约的 `expires_at` 时间已过

**解决**:
1. 增加租约时长
2. 优化注册流程，减少耗时
3. 实现租约续期功能（需要扩展 API）

### 问题：租约不存在 (404 错误)

**错误**: `{"success": false, "error": "租约不存在"}`

**原因**: 
1. 租约已被删除（过期自动清理）
2. lease_id 错误

**解决**:
1. 检查 lease_id 是否正确
2. 确认租约未过期
3. 重新领取邮箱

### 问题：无法获取邮件

**错误**: `{"success": false, "error": "无法获取邮件，所有方式均失败"}`

**原因**:
1. Token 过期
2. Graph API 限流
3. IMAP 连接失败

**解决**:
1. 刷新 Token: `POST /api/accounts/{id}/refresh`
2. 检查账号状态
3. 查看详细错误信息

## 总结

对于开发和测试：
- ✅ 使用手动输入验证码的方式
- ✅ 或者仅测试 API 连接，不测试完整流程
- ✅ 定期清理过期租约

对于生产环境：
- ✅ 配置真实 SMTP 服务器
- ✅ 根据实际情况调整租约时长
- ✅ 实现监控和告警
