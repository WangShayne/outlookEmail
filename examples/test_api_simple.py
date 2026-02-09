#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简单的 API 测试脚本
测试邮箱领取和释放功能，不涉及邮件验证
"""

import os
import sys
import requests
from datetime import datetime


def test_api():
    """测试外部 API 功能"""

    # 配置
    api_base = os.getenv("OUTLOOK_API_BASE", "http://localhost:5001")
    api_key = os.getenv("OUTLOOK_API_KEY") or os.getenv("SECRET_KEY")

    if not api_key:
        print("错误: 请设置 OUTLOOK_API_KEY 或 SECRET_KEY 环境变量")
        return False

    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    print("=" * 60)
    print("外部 API 测试")
    print("=" * 60)
    print(f"API 地址: {api_base}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 测试 1: 领取邮箱
    print("测试 1: 领取邮箱")
    print("-" * 60)

    try:
        response = requests.post(
            f"{api_base}/api/external/checkout",
            json={"owner": "test_script", "ttl_seconds": 300},
            headers=headers,
            timeout=10,
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                lease_id = data["lease_id"]
                email = data["email"]
                expires_at = data["expires_at"]

                print(f"✓ 成功领取邮箱")
                print(f"  邮箱: {email}")
                print(f"  租约ID: {lease_id}")
                print(f"  过期时间: {expires_at}")
                print()
            else:
                print(f"✗ 领取失败: {data.get('error')}")
                return False
        else:
            print(f"✗ HTTP 错误: {response.status_code}")
            return False
    except Exception as e:
        print(f"✗ 异常: {e}")
        return False

    # 测试 2: 获取账号信息
    print("测试 2: 获取账号信息")
    print("-" * 60)

    try:
        response = requests.get(
            f"{api_base}/api/external/account/{lease_id}", headers=headers, timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                account = data["account"]
                print(f"✓ 成功获取账号信息")
                print(f"  邮箱: {account['email']}")
                print(f"  账号ID: {account['account_id']}")
                print(f"  状态: {account['status']}")
                print()
            else:
                print(f"✗ 获取失败: {data.get('error')}")
        else:
            print(f"✗ HTTP 错误: {response.status_code}")
    except Exception as e:
        print(f"✗ 异常: {e}")

    # 测试 3: 获取邮件列表
    print("测试 3: 获取邮件列表")
    print("-" * 60)

    try:
        response = requests.get(
            f"{api_base}/api/external/emails/{lease_id}",
            params={"folder": "inbox", "top": 5},
            headers=headers,
            timeout=15,
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                emails = data.get("emails", [])
                method = data.get("method", "Unknown")
                print(f"✓ 成功获取邮件列表")
                print(f"  方法: {method}")
                print(f"  邮件数量: {len(emails)}")

                if emails:
                    print(f"  最新邮件:")
                    for i, email_item in enumerate(emails[:3], 1):
                        print(f"    {i}. {email_item.get('subject', '无主题')}")
                        print(f"       发件人: {email_item.get('from', '未知')}")
                        print(f"       日期: {email_item.get('date', '')}")
                print()
            else:
                print(f"✗ 获取失败: {data.get('error')}")
                print(f"  详情: {data.get('details', {})}")
                print()
        else:
            print(f"✗ HTTP 错误: {response.status_code}")
            print()
    except Exception as e:
        print(f"✗ 异常: {e}")
        print()

    # 测试 4: 释放邮箱
    print("测试 4: 释放邮箱")
    print("-" * 60)

    try:
        response = requests.post(
            f"{api_base}/api/external/checkout/complete",
            json={"lease_id": lease_id, "result": "test_success"},
            headers=headers,
            timeout=10,
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                print(f"✓ 成功释放邮箱")
                print()
            else:
                print(f"✗ 释放失败: {data.get('error')}")
                print()
        else:
            print(f"✗ HTTP 错误: {response.status_code}")
            print()
    except Exception as e:
        print(f"✗ 异常: {e}")
        print()

    print("=" * 60)
    print("测试完成")
    print("=" * 60)

    return True


if __name__ == "__main__":
    success = test_api()
    sys.exit(0 if success else 1)
