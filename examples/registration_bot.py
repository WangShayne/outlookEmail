#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
注册自动化客户端
使用 Outlook Email API 完成自动注册流程
"""

import os
import re
import sys
import time
import requests
from datetime import datetime
from typing import Optional, Dict, Any


class RegistrationBot:
    """注册自动化机器人"""

    def __init__(
        self,
        outlook_api_base: str,
        outlook_api_key: str,
        registration_api_base: str,
        bot_name: str = "bot_1",
    ):
        """
        初始化注册机器人

        Args:
            outlook_api_base: Outlook Email API 地址
            outlook_api_key: API Key (SECRET_KEY)
            registration_api_base: 注册服务 API 地址
            bot_name: 机器人标识
        """
        self.outlook_api_base = outlook_api_base.rstrip("/")
        self.outlook_api_key = outlook_api_key
        self.registration_api_base = registration_api_base.rstrip("/")
        self.bot_name = bot_name

        self.outlook_headers = {
            "X-API-Key": outlook_api_key,
            "Content-Type": "application/json",
        }

        self.lease_id: Optional[str] = None
        self.email: Optional[str] = None
        self.session_id: Optional[str] = None

    def log(self, message: str, level: str = "INFO"):
        """打印日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{level}] {message}")

    def checkout_email(self, ttl_seconds: int = 1800) -> bool:
        """
        领取邮箱

        Args:
            ttl_seconds: 租约时长（秒）

        Returns:
            是否成功
        """
        self.log("开始领取邮箱...")

        try:
            response = requests.post(
                f"{self.outlook_api_base}/api/external/checkout",
                json={"owner": self.bot_name, "ttl_seconds": ttl_seconds},
                headers=self.outlook_headers,
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    self.lease_id = data["lease_id"]
                    self.email = data["email"]
                    expires_at = data["expires_at"]

                    self.log(f"成功领取邮箱: {self.email}")
                    self.log(f"租约ID: {self.lease_id}")
                    self.log(f"过期时间: {expires_at}")
                    return True
                else:
                    self.log(f"领取失败: {data.get('error')}", "ERROR")
                    return False
            else:
                self.log(f"领取失败: HTTP {response.status_code}", "ERROR")
                return False

        except Exception as e:
            self.log(f"领取邮箱异常: {e}", "ERROR")
            return False

    def register_account(self, username: str) -> bool:
        """
        注册账号

        Args:
            username: 用户名

        Returns:
            是否成功
        """
        if not self.email:
            self.log("未领取邮箱，无法注册", "ERROR")
            return False

        self.log(f"开始注册账号: {username}")

        try:
            response = requests.post(
                f"{self.registration_api_base}/api/register",
                json={"email": self.email, "username": username},
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    self.session_id = data["session_id"]
                    expires_in = data.get("expires_in_seconds", 600)

                    self.log(f"注册请求成功，验证码已发送")
                    self.log(f"会话ID: {self.session_id}")
                    self.log(f"验证码有效期: {expires_in}秒")
                    return True
                else:
                    self.log(f"注册失败: {data.get('error')}", "ERROR")
                    return False
            else:
                self.log(f"注册失败: HTTP {response.status_code}", "ERROR")
                return False

        except Exception as e:
            self.log(f"注册异常: {e}", "ERROR")
            return False

    def wait_for_verification_email(
        self, max_attempts: int = 30, interval: int = 10
    ) -> Optional[str]:
        """
        等待并获取验证邮件中的验证码

        Args:
            max_attempts: 最大尝试次数
            interval: 检查间隔（秒）

        Returns:
            验证码，如果未找到则返回 None
        """
        if not self.lease_id:
            self.log("未领取邮箱，无法获取邮件", "ERROR")
            return None

        self.log(
            f"开始等待验证邮件（最多尝试 {max_attempts} 次，间隔 {interval} 秒）..."
        )

        for attempt in range(1, max_attempts + 1):
            self.log(f"第 {attempt}/{max_attempts} 次检查邮件...")

            try:
                # 获取邮件列表
                response = requests.get(
                    f"{self.outlook_api_base}/api/external/emails/{self.lease_id}",
                    params={"folder": "inbox", "top": 10},
                    headers=self.outlook_headers,
                    timeout=15,
                )

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        emails = data.get("emails", [])
                        self.log(f"收到 {len(emails)} 封邮件")

                        # 查找验证邮件
                        for email_item in emails:
                            subject = email_item.get("subject", "").lower()
                            from_addr = email_item.get("from", "").lower()
                            body_preview = email_item.get("body_preview", "")

                            # 判断是否为验证邮件
                            if any(
                                keyword in subject
                                for keyword in [
                                    "验证",
                                    "verify",
                                    "verification",
                                    "code",
                                ]
                            ):
                                self.log(
                                    f"找到可能的验证邮件: {email_item.get('subject')}"
                                )

                                # 先尝试从预览中提取验证码
                                code = self._extract_verification_code(body_preview)

                                if code:
                                    self.log(
                                        f"从邮件预览中提取到验证码: {code}", "SUCCESS"
                                    )
                                    return code

                                # 如果预览中没有，获取完整邮件
                                self.log("预览中未找到验证码，获取完整邮件...")
                                code = self._get_code_from_email_detail(
                                    email_item["id"]
                                )

                                if code:
                                    self.log(
                                        f"从完整邮件中提取到验证码: {code}", "SUCCESS"
                                    )
                                    return code
                    else:
                        self.log(f"获取邮件失败: {data.get('error')}", "WARN")
                elif response.status_code == 410:
                    self.log("租约已过期", "ERROR")
                    return None
                else:
                    self.log(f"获取邮件失败: HTTP {response.status_code}", "WARN")

            except Exception as e:
                self.log(f"获取邮件异常: {e}", "WARN")

            # 如果不是最后一次尝试，等待后继续
            if attempt < max_attempts:
                time.sleep(interval)

        self.log("未能找到验证邮件", "ERROR")
        return None

    def _extract_verification_code(self, text: str) -> Optional[str]:
        """
        从文本中提取验证码

        Args:
            text: 文本内容

        Returns:
            验证码，如果未找到则返回 None
        """
        if not text:
            return None

        # 尝试多种验证码格式
        patterns = [
            r"\b(\d{6})\b",  # 6位数字
            r"\b([A-Z0-9]{6})\b",  # 6位字母数字
            r"验证码[：:]\s*(\d{6})",  # 中文格式
            r"code[：:]\s*(\d{6})",  # 英文格式
            r"verification code[：:]\s*(\d{6})",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def _get_code_from_email_detail(self, message_id: str) -> Optional[str]:
        """
        从邮件详情中获取验证码

        Args:
            message_id: 邮件ID

        Returns:
            验证码，如果未找到则返回 None
        """
        try:
            response = requests.get(
                f"{self.outlook_api_base}/api/external/email/{self.lease_id}/{message_id}",
                headers=self.outlook_headers,
                timeout=15,
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    email_detail = data.get("email", {})
                    body = email_detail.get("body", "")

                    # 从 HTML 中移除标签
                    body_text = re.sub(r"<[^>]+>", " ", body)

                    return self._extract_verification_code(body_text)

        except Exception as e:
            self.log(f"获取邮件详情异常: {e}", "WARN")

        return None

    def verify_code(self, code: str) -> bool:
        """
        提交验证码

        Args:
            code: 验证码

        Returns:
            是否验证成功
        """
        if not self.session_id:
            self.log("未注册账号，无法验证", "ERROR")
            return False

        self.log(f"提交验证码: {code}")

        try:
            response = requests.post(
                f"{self.registration_api_base}/api/verify",
                json={"session_id": self.session_id, "code": code},
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    user = data.get("user", {})
                    self.log(f"验证成功！", "SUCCESS")
                    self.log(f"用户ID: {user.get('user_id')}")
                    self.log(f"用户名: {user.get('username')}")
                    self.log(f"邮箱: {user.get('email')}")
                    return True
                else:
                    self.log(f"验证失败: {data.get('error')}", "ERROR")
                    return False
            else:
                self.log(f"验证失败: HTTP {response.status_code}", "ERROR")
                return False

        except Exception as e:
            self.log(f"验证异常: {e}", "ERROR")
            return False

    def cleanup_emails(self, message_ids: list) -> bool:
        """
        清理邮件（可选）

        Args:
            message_ids: 邮件ID列表

        Returns:
            是否成功
        """
        if not self.lease_id or not message_ids:
            return True

        self.log(f"清理 {len(message_ids)} 封邮件...")

        try:
            response = requests.post(
                f"{self.outlook_api_base}/api/external/emails/delete",
                json={"lease_id": self.lease_id, "message_ids": message_ids},
                headers=self.outlook_headers,
                timeout=15,
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    success_count = data.get("success_count", 0)
                    self.log(f"成功删除 {success_count} 封邮件")
                    return True
                else:
                    self.log(f"删除邮件失败: {data.get('error')}", "WARN")
                    return False
            else:
                self.log(f"删除邮件失败: HTTP {response.status_code}", "WARN")
                return False

        except Exception as e:
            self.log(f"删除邮件异常: {e}", "WARN")
            return False

    def release_email(self, result: str = "success") -> bool:
        """
        释放邮箱

        Args:
            result: 结果标识

        Returns:
            是否成功
        """
        if not self.lease_id:
            self.log("未领取邮箱，无需释放", "WARN")
            return True

        self.log(f"释放邮箱: {self.email}")

        try:
            response = requests.post(
                f"{self.outlook_api_base}/api/external/checkout/complete",
                json={"lease_id": self.lease_id, "result": result},
                headers=self.outlook_headers,
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    self.log("邮箱已释放", "SUCCESS")
                    self.lease_id = None
                    self.email = None
                    return True
                else:
                    self.log(f"释放失败: {data.get('error')}", "ERROR")
                    return False
            else:
                self.log(f"释放失败: HTTP {response.status_code}", "ERROR")
                return False

        except Exception as e:
            self.log(f"释放异常: {e}", "ERROR")
            return False

    def run(self, username: str) -> bool:
        """
        执行完整的注册流程

        Args:
            username: 用户名

        Returns:
            是否成功
        """
        self.log("=" * 60)
        self.log(f"开始自动注册流程: {username}")
        self.log("=" * 60)

        result = "failed"

        try:
            # 1. 领取邮箱
            if not self.checkout_email():
                return False

            # 2. 注册账号
            if not self.register_account(username):
                return False

            # 3. 等待验证邮件
            code = self.wait_for_verification_email()
            if not code:
                return False

            # 4. 提交验证码
            if not self.verify_code(code):
                return False

            result = "success"
            self.log("=" * 60)
            self.log("注册流程完成！", "SUCCESS")
            self.log("=" * 60)
            return True

        except KeyboardInterrupt:
            self.log("用户中断", "WARN")
            result = "interrupted"
            return False

        except Exception as e:
            self.log(f"流程异常: {e}", "ERROR")
            import traceback

            traceback.print_exc()
            return False

        finally:
            # 5. 释放邮箱
            self.release_email(result)


def main():
    """主函数"""
    # 从环境变量读取配置
    outlook_api_base = os.getenv("OUTLOOK_API_BASE", "http://localhost:5001")
    outlook_api_key = os.getenv("OUTLOOK_API_KEY", "")
    registration_api_base = os.getenv("REGISTRATION_API_BASE", "http://localhost:5002")

    if not outlook_api_key:
        print("错误: 请设置环境变量 OUTLOOK_API_KEY")
        print("export OUTLOOK_API_KEY=your-secret-key")
        sys.exit(1)

    # 创建机器人
    bot = RegistrationBot(
        outlook_api_base=outlook_api_base,
        outlook_api_key=outlook_api_key,
        registration_api_base=registration_api_base,
        bot_name="bot_1",
    )

    # 执行注册
    username = f"user_{int(time.time())}"
    success = bot.run(username)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
