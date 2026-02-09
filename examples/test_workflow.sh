git#!/bin/bash
# 测试完整的注册自动化流程

set -e

echo "=========================================="
echo "注册自动化流程测试"
echo "=========================================="
echo ""

# 尝试从 .env 文件加载 SECRET_KEY
if [ -z "$SECRET_KEY" ]; then
    if [ -f "../.env" ]; then
        echo "从 ../.env 加载环境变量..."
        export $(grep -v '^#' ../.env | grep SECRET_KEY | xargs)
    elif [ -f ".env" ]; then
        echo "从 .env 加载环境变量..."
        export $(grep -v '^#' .env | grep SECRET_KEY | xargs)
    fi
fi

# 检查环境变量
if [ -z "$SECRET_KEY" ]; then
    echo "错误: 请设置 SECRET_KEY 环境变量"
    echo ""
    echo "方式1: 直接设置"
    echo "  export SECRET_KEY=your-secret-key"
    echo ""
    echo "方式2: 从 .env 文件加载"
    echo "  source ../.env"
    echo ""
    exit 1
fi

# 配置
OUTLOOK_API_BASE="http://localhost:5001"
REGISTRATION_API_BASE="http://localhost:5002"
OUTLOOK_API_KEY="$SECRET_KEY"

echo "配置信息:"
echo "  Outlook API: $OUTLOOK_API_BASE"
echo "  Registration API: $REGISTRATION_API_BASE"
echo ""

# 检查 Outlook Email API 是否运行
echo "1. 检查 Outlook Email API..."
if curl -s -f "$OUTLOOK_API_BASE/login" > /dev/null 2>&1; then
    echo "   ✓ Outlook Email API 运行正常"
else
    echo "   ✗ Outlook Email API 未运行"
    echo "   请先启动: python web_outlook_app.py"
    exit 1
fi

# 检查模拟注册服务器是否运行
echo "2. 检查模拟注册服务器..."
if curl -s -f "$REGISTRATION_API_BASE/health" > /dev/null 2>&1; then
    echo "   ✓ 模拟注册服务器运行正常"
else
    echo "   ✗ 模拟注册服务器未运行"
    echo "   请先启动: python examples/mock_registration_server.py"
    exit 1
fi

# 检查可用邮箱数量
echo "3. 检查可用邮箱..."
AVAILABLE_COUNT=$(sqlite3 ../data/outlook_accounts.db "SELECT COUNT(*) FROM accounts WHERE status='active' AND id NOT IN (SELECT account_id FROM account_leases WHERE expires_at > datetime('now'))" 2>/dev/null || echo "0")
echo "   可用邮箱数量: $AVAILABLE_COUNT"
if [ "$AVAILABLE_COUNT" -eq "0" ]; then
    echo "   ✗ 没有可用邮箱"
    echo "   请添加账号或等待租约过期"
    echo ""
    echo "   清理过期租约："
    echo "   sqlite3 ../data/outlook_accounts.db \"DELETE FROM account_leases WHERE expires_at <= datetime('now')\""
    exit 1
fi

# 测试 API 连接
echo "4. 测试 API 连接..."

# 测试领取邮箱
echo "   测试领取邮箱..."
CHECKOUT_RESPONSE=$(curl -s -X POST "$OUTLOOK_API_BASE/api/external/checkout" \
    -H "X-API-Key: $OUTLOOK_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"owner": "test_script", "ttl_seconds": 300}')

if echo "$CHECKOUT_RESPONSE" | grep -q '"success":true'; then
    LEASE_ID=$(echo "$CHECKOUT_RESPONSE" | grep -o '"lease_id":"[^"]*"' | cut -d'"' -f4)
    EMAIL=$(echo "$CHECKOUT_RESPONSE" | grep -o '"email":"[^"]*"' | cut -d'"' -f4)
    echo "   ✓ 成功领取邮箱: $EMAIL"
    echo "   租约ID: $LEASE_ID"
    
    # 释放测试邮箱
    echo "   释放测试邮箱..."
    curl -s -X POST "$OUTLOOK_API_BASE/api/external/checkout/complete" \
        -H "X-API-Key: $OUTLOOK_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"lease_id\": \"$LEASE_ID\", \"result\": \"test\"}" > /dev/null
    echo "   ✓ 测试邮箱已释放"
else
    echo "   ✗ 领取邮箱失败"
    echo "   响应: $CHECKOUT_RESPONSE"
    exit 1
fi

# 运行注册机器人
echo ""
echo "5. 运行注册机器人..."
echo "=========================================="
echo ""

export OUTLOOK_API_BASE
export OUTLOOK_API_KEY
export REGISTRATION_API_BASE

cd "$(dirname "$0")"
python3 registration_bot.py

EXIT_CODE=$?

echo ""
echo "=========================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ 测试完成：注册流程成功"
else
    echo "✗ 测试失败：注册流程出错"
fi
echo "=========================================="

exit $EXIT_CODE
