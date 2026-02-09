# 注册自动化示例

本目录包含完整的注册自动化示例代码。

## 文件说明

### 1. `mock_registration_server.py`
模拟注册服务器，用于测试注册流程。

**功能**：
- 接收注册请求
- 生成验证码
- 模拟发送验证邮件（打印到控制台）
- 验证验证码
- 管理注册会话

**端口**：默认 5002

### 2. `registration_bot.py`
注册自动化客户端，完整实现注册流程。

**功能**：
- 从 Outlook Email API 领取邮箱
- 调用注册服务注册账号
- 轮询获取验证邮件
- 提取验证码
- 提交验证完成注册
- 释放邮箱

## 快速开始

### 前置条件

1. **Outlook Email API 服务运行中**
   ```bash
   cd /Users/shayne/work/outlookEmail
   export SECRET_KEY=your-secret-key
   python web_outlook_app.py
   ```
   默认端口：5001

2. **数据库中有可用的 active 账号**
   ```bash
   # 检查可用账号数量
   sqlite3 data/outlook_accounts.db "SELECT COUNT(*) FROM accounts WHERE status='active'"
   ```

### 步骤 1：启动模拟注册服务器

```bash
cd /Users/shayne/work/outlookEmail/examples
python mock_registration_server.py
```

服务器将在 `http://localhost:5002` 启动。

**测试服务器**：
```bash
curl http://localhost:5002/health
```

### 步骤 2：运行注册机器人

在新终端中：

```bash
cd /Users/shayne/work/outlookEmail/examples

# 设置环境变量
export OUTLOOK_API_KEY=your-secret-key
export OUTLOOK_API_BASE=http://localhost:5001
export REGISTRATION_API_BASE=http://localhost:5002

# 运行机器人
python registration_bot.py
```

## 完整流程演示

### 终端 1：Outlook Email API
```bash
$ cd /Users/shayne/work/outlookEmail
$ export SECRET_KEY=your-secret-key
$ python web_outlook_app.py

 * Running on http://0.0.0.0:5001
```

### 终端 2：模拟注册服务器
```bash
$ cd /Users/shayne/work/outlookEmail/examples
$ python mock_registration_server.py

模拟注册服务器启动在端口 5002
访问 http://localhost:5002/health 检查服务状态

注意：此服务器仅用于测试，不会真实发送邮件
验证码会打印在控制台中

 * Running on http://0.0.0.0:5002
```

### 终端 3：注册机器人
```bash
$ cd /Users/shayne/work/outlookEmail/examples
$ export OUTLOOK_API_KEY=your-secret-key
$ python registration_bot.py

[2026-02-09 10:00:00] [INFO] ============================================================
[2026-02-09 10:00:00] [INFO] 开始自动注册流程: user_1707451200
[2026-02-09 10:00:00] [INFO] ============================================================
[2026-02-09 10:00:00] [INFO] 开始领取邮箱...
[2026-02-09 10:00:01] [INFO] 成功领取邮箱: example@outlook.com
[2026-02-09 10:00:01] [INFO] 租约ID: abc123def456...
[2026-02-09 10:00:01] [INFO] 过期时间: 2026-02-09 10:30:00
[2026-02-09 10:00:01] [INFO] 开始注册账号: user_1707451200
[2026-02-09 10:00:02] [INFO] 注册请求成功，验证码已发送
[2026-02-09 10:00:02] [INFO] 会话ID: sess_1707451202_1234
[2026-02-09 10:00:02] [INFO] 验证码有效期: 600秒
[2026-02-09 10:00:02] [INFO] 开始等待验证邮件（最多尝试 30 次，间隔 10 秒）...
[2026-02-09 10:00:02] [INFO] 第 1/30 次检查邮件...
[2026-02-09 10:00:03] [INFO] 收到 0 封邮件
[2026-02-09 10:00:13] [INFO] 第 2/30 次检查邮件...
[2026-02-09 10:00:14] [INFO] 收到 1 封邮件
[2026-02-09 10:00:14] [INFO] 找到可能的验证邮件: 验证您的账号 - Verify Your Account
[2026-02-09 10:00:14] [SUCCESS] 从邮件预览中提取到验证码: 123456
[2026-02-09 10:00:14] [INFO] 提交验证码: 123456
[2026-02-09 10:00:15] [SUCCESS] 验证成功！
[2026-02-09 10:00:15] [INFO] 用户ID: 1
[2026-02-09 10:00:15] [INFO] 用户名: user_1707451200
[2026-02-09 10:00:15] [INFO] 邮箱: example@outlook.com
[2026-02-09 10:00:15] [INFO] ============================================================
[2026-02-09 10:00:15] [SUCCESS] 注册流程完成！
[2026-02-09 10:00:15] [INFO] ============================================================
[2026-02-09 10:00:15] [INFO] 释放邮箱: example@outlook.com
[2026-02-09 10:00:16] [SUCCESS] 邮箱已释放
```

同时，在终端 2（模拟注册服务器）会看到：
```bash
[模拟] 发送验证邮件到 example@outlook.com
[模拟] 验证码: 123456
```

## 环境变量配置

### 必需
- `OUTLOOK_API_KEY`: Outlook Email API 的 SECRET_KEY

### 可选
- `OUTLOOK_API_BASE`: Outlook Email API 地址（默认 `http://localhost:5001`）
- `REGISTRATION_API_BASE`: 注册服务地址（默认 `http://localhost:5002`）

## 自定义使用

### 作为 Python 模块使用

```python
from registration_bot import RegistrationBot

# 创建机器人实例
bot = RegistrationBot(
    outlook_api_base="http://localhost:5001",
    outlook_api_key="your-secret-key",
    registration_api_base="http://localhost:5002",
    bot_name="bot_1"
)

# 执行注册
success = bot.run(username="testuser123")

if success:
    print("注册成功！")
else:
    print("注册失败")
```

### 批量注册

```python
from registration_bot import RegistrationBot
import time

bot = RegistrationBot(
    outlook_api_base="http://localhost:5001",
    outlook_api_key="your-secret-key",
    registration_api_base="http://localhost:5002",
    bot_name="batch_bot"
)

# 批量注册 10 个账号
for i in range(10):
    username = f"user_{int(time.time())}_{i}"
    success = bot.run(username)
    
    if success:
        print(f"[{i+1}/10] 注册成功: {username}")
    else:
        print(f"[{i+1}/10] 注册失败: {username}")
    
    # 间隔 5 秒
    time.sleep(5)
```

### 并发注册（多进程）

```python
from registration_bot import RegistrationBot
from multiprocessing import Process
import time

def register_worker(worker_id, count):
    """注册工作进程"""
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

# 启动 3 个并发工作进程，每个注册 5 个账号
workers = []
for worker_id in range(3):
    p = Process(target=register_worker, args=(worker_id, 5))
    p.start()
    workers.append(p)

# 等待所有工作进程完成
for p in workers:
    p.join()

print("所有注册任务完成")
```

## API 测试

### 测试模拟注册服务器

```bash
# 健康检查
curl http://localhost:5002/health

# 注册账号
curl -X POST http://localhost:5002/api/register \
  -H "Content-Type: application/json" \
  -d '{"email": "test@outlook.com", "username": "testuser"}'

# 验证验证码
curl -X POST http://localhost:5002/api/verify \
  -H "Content-Type: application/json" \
  -d '{"session_id": "sess_xxx", "code": "123456"}'

# 查看所有会话
curl http://localhost:5002/api/sessions

# 查看已注册用户
curl http://localhost:5002/api/users

# 重置所有数据
curl -X POST http://localhost:5002/api/reset
```

### 测试 Outlook Email API

```bash
# 领取邮箱
curl -X POST http://localhost:5001/api/external/checkout \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"owner": "test", "ttl_seconds": 900}'

# 获取邮件列表
curl -X GET "http://localhost:5001/api/external/emails/lease_id_here?folder=inbox&top=10" \
  -H "X-API-Key: your-secret-key"

# 释放邮箱
curl -X POST http://localhost:5001/api/external/checkout/complete \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"lease_id": "lease_id_here", "result": "success"}'
```

## 故障排查

### 问题：无可用邮箱

**错误信息**：
```
领取失败: 没有可用邮箱
```

**解决方法**：
1. 检查数据库中是否有 active 状态的账号
2. 检查是否所有账号都被租用（查看 account_leases 表）
3. 等待租约过期或手动清理过期租约

### 问题：未找到验证邮件

**错误信息**：
```
未能找到验证邮件
```

**可能原因**：
1. 邮件发送延迟（模拟服务器有 2-5 秒随机延迟）
2. Token 过期，无法获取邮件
3. 邮件进入垃圾箱（检查 junkemail 文件夹）

**解决方法**：
1. 增加 `max_attempts` 或 `interval` 参数
2. 检查 Outlook Email API 日志
3. 手动刷新账号 Token

### 问题：验证码提取失败

**可能原因**：
1. 验证码格式不匹配正则表达式
2. 邮件内容被 HTML 标签干扰

**解决方法**：
1. 修改 `_extract_verification_code` 方法中的正则表达式
2. 检查邮件原始内容，调整提取逻辑

## 生产环境建议

### 1. 配置真实 SMTP
在 `mock_registration_server.py` 中取消注释 SMTP 代码，配置真实邮件服务器。

### 2. 错误重试
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def register_with_retry(bot, username):
    return bot.run(username)
```

### 3. 日志记录
```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('registration.log'),
        logging.StreamHandler()
    ]
)
```

### 4. 监控告警
- 监控可用邮箱数量
- 监控注册成功率
- 监控租约使用情况
- 设置告警阈值

### 5. 安全加固
- 使用 HTTPS
- 限制 API 访问 IP
- 添加速率限制
- 定期轮换 API Key

## 许可证

本示例代码仅供学习和测试使用。
