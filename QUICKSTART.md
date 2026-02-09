# 快速开始指南

## 完整的注册自动化系统已就绪！

### 📁 文件结构

```
/Users/shayne/work/outlookEmail/
├── web_outlook_app.py              # 主应用（已添加外部API）
├── EXTERNAL_API.md                 # 外部API完整文档
├── examples/
│   ├── README.md                   # 示例使用说明
│   ├── mock_registration_server.py # 模拟注册服务器
│   ├── registration_bot.py         # 注册自动化客户端
│   └── test_workflow.sh            # 一键测试脚本
└── data/
    └── outlook_accounts.db         # 数据库（1928个活跃账号）
```

### 🚀 快速测试（3步启动）

#### 终端 1：启动 Outlook Email API
```bash
cd /Users/shayne/work/outlookEmail
export SECRET_KEY=your-secret-key
python web_outlook_app.py
```

#### 终端 2：启动模拟注册服务器
```bash
cd /Users/shayne/work/outlookEmail/examples
python mock_registration_server.py
```

#### 终端 3：运行自动化测试
```bash
cd /Users/shayne/work/outlookEmail/examples
export SECRET_KEY=your-secret-key
./test_workflow.sh
```

或者手动运行机器人：
```bash
export OUTLOOK_API_KEY=your-secret-key
python registration_bot.py
```

### ✅ 已实现的功能

#### 1. 外部 API 端点（已添加到 web_outlook_app.py）
- ✅ `POST /api/external/checkout` - 领取邮箱
- ✅ `POST /api/external/checkout/complete` - 释放邮箱
- ✅ `GET /api/external/account/<lease_id>` - 获取账号完整信息
- ✅ `GET /api/external/emails/<lease_id>` - 获取邮件列表
- ✅ `GET /api/external/email/<lease_id>/<message_id>` - 获取邮件详情
- ✅ `POST /api/external/emails/delete` - 删除邮件

#### 2. 模拟注册服务器（mock_registration_server.py）
- ✅ 注册接口
- ✅ 验证码生成
- ✅ 验证接口
- ✅ 会话管理
- ✅ 测试接口

#### 3. 注册自动化客户端（registration_bot.py）
- ✅ 领取邮箱
- ✅ 注册账号
- ✅ 轮询邮件
- ✅ 提取验证码（多种格式）
- ✅ 提交验证
- ✅ 释放邮箱
- ✅ 完整日志
- ✅ 错误处理

#### 4. 文档
- ✅ 完整 API 文档（EXTERNAL_API.md）
- ✅ 使用示例（examples/README.md）
- ✅ 快速开始指南（本文件）

### 📊 工作流程

```
┌─────────────┐
│ 注册机器人   │
└──────┬──────┘
       │
       │ 1. POST /api/external/checkout
       ▼
┌─────────────────────┐
│ Outlook Email API   │
│ (领取邮箱)          │
└──────┬──────────────┘
       │ 返回: lease_id, email
       │
       │ 2. POST /api/register
       ▼
┌─────────────────────┐
│ 注册服务            │
│ (发送验证邮件)      │
└─────────────────────┘
       │
       │ 3. 轮询: GET /api/external/emails/{lease_id}
       ▼
┌─────────────────────┐
│ Outlook Email API   │
│ (获取邮件列表)      │
└──────┬──────────────┘
       │ 返回: 邮件列表
       │
       │ 4. GET /api/external/email/{lease_id}/{message_id}
       ▼
┌─────────────────────┐
│ Outlook Email API   │
│ (获取邮件详情)      │
└──────┬──────────────┘
       │ 返回: 邮件内容（含验证码）
       │
       │ 5. POST /api/verify
       ▼
┌─────────────────────┐
│ 注册服务            │
│ (验证验证码)        │
└──────┬──────────────┘
       │ 返回: 注册成功
       │
       │ 6. POST /api/external/checkout/complete
       ▼
┌─────────────────────┐
│ Outlook Email API   │
│ (释放邮箱)          │
└─────────────────────┘
```

### 🔧 配置说明

#### 环境变量
```bash
# 必需
export SECRET_KEY=your-secret-key           # Outlook Email API 密钥

# 可选（有默认值）
export OUTLOOK_API_BASE=http://localhost:5001
export REGISTRATION_API_BASE=http://localhost:5002
export PORT=5001                            # Outlook API 端口
```

#### 数据库状态
```bash
# 检查可用账号
sqlite3 data/outlook_accounts.db "SELECT COUNT(*) FROM accounts WHERE status='active'"

# 检查租约
sqlite3 data/outlook_accounts.db "SELECT * FROM account_leases"

# 清理过期租约
sqlite3 data/outlook_accounts.db "DELETE FROM account_leases WHERE expires_at <= datetime('now')"
```

### 📝 使用示例

#### 单次注册
```python
from examples.registration_bot import RegistrationBot

bot = RegistrationBot(
    outlook_api_base="http://localhost:5001",
    outlook_api_key="your-secret-key",
    registration_api_base="http://localhost:5002"
)

success = bot.run(username="testuser123")
```

#### 批量注册
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
    bot.run(username)
    time.sleep(5)  # 间隔5秒
```

#### 并发注册（多进程）
```python
from multiprocessing import Process
from examples.registration_bot import RegistrationBot

def worker(worker_id, count):
    bot = RegistrationBot(
        outlook_api_base="http://localhost:5001",
        outlook_api_key="your-secret-key",
        registration_api_base="http://localhost:5002",
        bot_name=f"worker_{worker_id}"
    )
    for i in range(count):
        bot.run(f"user_w{worker_id}_{i}")

# 3个进程，每个注册5个账号
for i in range(3):
    Process(target=worker, args=(i, 5)).start()
```

### 🧪 测试 API

#### 测试领取邮箱
```bash
curl -X POST http://localhost:5001/api/external/checkout \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"owner": "test", "ttl_seconds": 900}'
```

#### 测试获取邮件
```bash
curl -X GET "http://localhost:5001/api/external/emails/lease_id_here?folder=inbox&top=10" \
  -H "X-API-Key: your-secret-key"
```

#### 测试释放邮箱
```bash
curl -X POST http://localhost:5001/api/external/checkout/complete \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"lease_id": "lease_id_here", "result": "success"}'
```

### 🐛 故障排查

#### 问题：无可用邮箱
```bash
# 检查活跃账号数量
sqlite3 data/outlook_accounts.db "SELECT COUNT(*) FROM accounts WHERE status='active'"

# 检查租约
sqlite3 data/outlook_accounts.db "SELECT * FROM account_leases"

# 清理过期租约
sqlite3 data/outlook_accounts.db "DELETE FROM account_leases WHERE expires_at <= datetime('now')"
```

#### 问题：未找到验证邮件
- 检查模拟服务器控制台，验证码会打印出来
- 增加轮询次数或间隔
- 检查邮件是否进入垃圾箱（junkemail 文件夹）

#### 问题：Token 过期
```bash
# 手动刷新所有账号
curl -X GET http://localhost:5001/api/accounts/trigger-scheduled-refresh?force=true \
  -H "Cookie: session=your-session-cookie"
```

### 📚 完整文档

- **API 参考**: `EXTERNAL_API.md` - 完整的外部 API 文档
- **示例说明**: `examples/README.md` - 详细的使用示例
- **项目分析**: `PROJECT_ANALYSIS.md` - 项目架构分析
- **架构文档**: `ARCHITECTURE.md` - 系统架构说明

### 🎯 下一步

1. **生产环境部署**
   - 配置真实 SMTP 服务器
   - 使用 HTTPS
   - 添加速率限制
   - 配置监控告警

2. **功能扩展**
   - 添加租约续期接口
   - 支持邮件搜索
   - 添加邮件标记功能
   - 实现邮件转发

3. **性能优化**
   - 使用 Redis 缓存
   - 实现连接池
   - 优化数据库查询
   - 添加异步处理

### ✨ 总结

所有功能已完成并可以使用：

✅ 外部 API 端点已添加到 `web_outlook_app.py`
✅ 模拟注册服务器已创建
✅ 注册自动化客户端已创建
✅ 完整文档已编写
✅ 测试脚本已准备

现在你可以：
1. 启动三个服务（Outlook API、模拟注册服务器、注册机器人）
2. 运行 `./test_workflow.sh` 进行完整测试
3. 使用 Python 模块进行批量或并发注册
4. 根据需要扩展和定制功能

祝使用愉快！🎉
