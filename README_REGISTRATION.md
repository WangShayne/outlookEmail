# 注册自动化系统使用指南

## 🎯 快速开始（3分钟）

### 第一步：启动 Outlook Email API
```bash
cd /Users/shayne/work/outlookEmail
export SECRET_KEY=your-secret-key
python web_outlook_app.py
```
访问 http://localhost:5001 确认服务运行

### 第二步：启动模拟注册服务器
```bash
# 新终端
cd /Users/shayne/work/outlookEmail/examples
python mock_registration_server.py
```
访问 http://localhost:5002/health 确认服务运行

### 第三步：运行注册机器人
```bash
# 新终端
cd /Users/shayne/work/outlookEmail/examples
export OUTLOOK_API_KEY=your-secret-key
python registration_bot.py
```

## 📁 项目文件

```
/Users/shayne/work/outlookEmail/
├── web_outlook_app.py              # 主应用（已添加6个外部API端点）
├── EXTERNAL_API.md                 # 外部API完整文档（11KB）
├── QUICKSTART.md                   # 快速开始指南（8.5KB）
├── SUMMARY.md                      # 实现总结（11KB）
├── README_REGISTRATION.md          # 本文件
└── examples/
    ├── .env.example                # 环境变量示例
    ├── README.md                   # 详细使用说明（12KB）
    ├── mock_registration_server.py # 模拟注册服务器（308行）
    ├── registration_bot.py         # 注册自动化客户端（465行）
    └── test_workflow.sh            # 一键测试脚本（可执行）
```

## 🔑 核心功能

### 1. 外部 API（已添加到 web_outlook_app.py）

| API 端点 | 功能 |
|---------|------|
| `POST /api/external/checkout` | 领取可用邮箱 |
| `GET /api/external/account/<lease_id>` | 获取账号完整信息 |
| `GET /api/external/emails/<lease_id>` | 获取邮件列表 |
| `GET /api/external/email/<lease_id>/<message_id>` | 获取邮件详情 |
| `POST /api/external/emails/delete` | 删除邮件 |
| `POST /api/external/checkout/complete` | 释放邮箱 |

**认证方式**：所有请求需要在 Header 中携带 `X-API-Key: your-secret-key`

### 2. 注册流程

```
1. 领取邮箱 → 2. 注册账号 → 3. 等待邮件 → 4. 提取验证码 → 5. 提交验证 → 6. 释放邮箱
```

### 3. 使用示例

#### 单次注册
```python
from examples.registration_bot import RegistrationBot

bot = RegistrationBot(
    outlook_api_base="http://localhost:5001",
    outlook_api_key="your-secret-key",
    registration_api_base="http://localhost:5002"
)

success = bot.run(username="testuser")
```

#### 批量注册（10个账号）
```python
import time
from examples.registration_bot import RegistrationBot

bot = RegistrationBot(
    outlook_api_base="http://localhost:5001",
    outlook_api_key="your-secret-key",
    registration_api_base="http://localhost:5002"
)

for i in range(10):
    username = f"user_{int(time.time())}_{i}"
    success = bot.run(username)
    if success:
        print(f"✓ 注册成功: {username}")
    time.sleep(5)  # 间隔5秒
```

#### 并发注册（3个进程，每个注册5个）
```python
from multiprocessing import Process
from examples.registration_bot import RegistrationBot
import time

def worker(worker_id, count):
    bot = RegistrationBot(
        outlook_api_base="http://localhost:5001",
        outlook_api_key="your-secret-key",
        registration_api_base="http://localhost:5002",
        bot_name=f"worker_{worker_id}"
    )
    
    for i in range(count):
        username = f"user_w{worker_id}_{int(time.time())}_{i}"
        bot.run(username)
        time.sleep(2)

# 启动3个工作进程
processes = []
for worker_id in range(3):
    p = Process(target=worker, args=(worker_id, 5))
    p.start()
    processes.append(p)

# 等待所有进程完成
for p in processes:
    p.join()

print("所有注册完成")
```

## 🧪 测试

### 自动化测试
```bash
cd /Users/shayne/work/outlookEmail/examples
export SECRET_KEY=your-secret-key
./test_workflow.sh
```

### 手动测试 API

#### 1. 领取邮箱
```bash
curl -X POST http://localhost:5001/api/external/checkout \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"owner": "test", "ttl_seconds": 900}'
```

响应：
```json
{
  "success": true,
  "lease_id": "abc123...",
  "email": "example@outlook.com",
  "expires_at": "2026-02-09 10:30:00"
}
```

#### 2. 获取邮件列表
```bash
curl -X GET "http://localhost:5001/api/external/emails/abc123?folder=inbox&top=10" \
  -H "X-API-Key: your-secret-key"
```

#### 3. 释放邮箱
```bash
curl -X POST http://localhost:5001/api/external/checkout/complete \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"lease_id": "abc123", "result": "success"}'
```

## 📊 系统状态检查

### 检查可用邮箱数量
```bash
sqlite3 data/outlook_accounts.db "SELECT COUNT(*) FROM accounts WHERE status='active'"
```

### 检查当前租约
```bash
sqlite3 data/outlook_accounts.db "SELECT * FROM account_leases"
```

### 清理过期租约
```bash
sqlite3 data/outlook_accounts.db "DELETE FROM account_leases WHERE expires_at <= datetime('now')"
```

### 查看审计日志
```bash
sqlite3 data/outlook_accounts.db "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 10"
```

## 🐛 常见问题

### 问题1：无可用邮箱
**错误**：`{"success": false, "error": "没有可用邮箱"}`

**解决**：
```bash
# 检查活跃账号
sqlite3 data/outlook_accounts.db "SELECT COUNT(*) FROM accounts WHERE status='active'"

# 检查租约
sqlite3 data/outlook_accounts.db "SELECT * FROM account_leases"

# 清理过期租约
sqlite3 data/outlook_accounts.db "DELETE FROM account_leases WHERE expires_at <= datetime('now')"
```

### 问题2：未找到验证邮件
**原因**：邮件发送延迟或进入垃圾箱

**解决**：
1. 检查模拟服务器控制台，验证码会打印出来
2. 增加轮询次数：修改 `registration_bot.py` 中的 `max_attempts`
3. 检查垃圾箱：`folder=junkemail`

### 问题3：租约过期
**错误**：`{"success": false, "error": "租约已过期"}`

**解决**：
1. 增加租约时长：`ttl_seconds=1800`（30分钟）
2. 优化注册流程，减少等待时间

### 问题4：API Key 无效
**错误**：`{"success": false, "error": "Unauthorized"}`

**解决**：
```bash
# 确保 API Key 与 SECRET_KEY 一致
export OUTLOOK_API_KEY=$(echo $SECRET_KEY)
```

## 📚 完整文档

| 文档 | 说明 |
|------|------|
| **EXTERNAL_API.md** | 外部 API 完整文档，包含所有端点详细说明 |
| **QUICKSTART.md** | 快速开始指南，3步启动系统 |
| **SUMMARY.md** | 实现总结，包含架构设计和技术细节 |
| **examples/README.md** | 详细使用说明，包含更多示例和故障排查 |

## 🎯 下一步

### 开发环境
- ✅ 使用模拟注册服务器测试
- ✅ 单次注册验证流程
- ✅ 批量注册测试性能

### 生产环境
1. **配置真实 SMTP**：在 `mock_registration_server.py` 中取消注释 SMTP 代码
2. **使用 HTTPS**：配置 Nginx 反向代理
3. **添加监控**：监控可用邮箱数量、注册成功率
4. **优化性能**：使用 Redis 缓存、连接池

## 💡 提示

1. **并发限制**：根据可用邮箱数量调整并发数
2. **速率控制**：避免过快请求导致限流
3. **错误处理**：实现重试机制和熔断器
4. **日志记录**：所有操作都会记录到 `audit_logs` 表

## 🎉 总结

完整的注册自动化系统已就绪：

✅ **6个外部 API 端点** - 完整的邮箱租用和邮件获取
✅ **模拟注册服务器** - 用于测试和开发
✅ **自动化客户端** - 完整的注册流程自动化
✅ **完整文档** - API 文档、使用指南、故障排查
✅ **测试脚本** - 一键测试完整流程

现在可以：
- 运行 `./test_workflow.sh` 进行完整测试
- 使用 Python 模块进行批量或并发注册
- 根据需要扩展和定制功能

**当前状态**：1928个活跃邮箱账号可用 🚀

祝使用愉快！
