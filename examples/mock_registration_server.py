#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟注册服务器
用于测试邮箱注册自动化流程
"""

import os
import random
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify
from threading import Thread

app = Flask(__name__)

# 存储注册会话（内存中）
registration_sessions = {}

# 存储已注册用户
registered_users = {}


def generate_verification_code():
    """生成6位数字验证码"""
    return str(random.randint(100000, 999999))


def send_verification_email(email, code):
    """
    发送验证邮件
    注意：这是模拟函数，实际需要配置 SMTP 服务器
    """
    print(f"[模拟] 发送验证邮件到 {email}")
    print(f"[模拟] 验证码: {code}")

    # 实际发送邮件的代码（需要配置 SMTP）
    # 这里仅作演示，实际使用时需要配置真实的 SMTP 服务器
    """
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    
    if smtp_user and smtp_password:
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = "验证您的账号 - Verify Your Account"
            msg['From'] = smtp_user
            msg['To'] = email
            
            # 纯文本版本
            text = f"您的验证码是: {code}\n\n此验证码将在10分钟后过期。\n\nYour verification code is: {code}\n\nThis code will expire in 10 minutes."
            
            # HTML 版本
            html = f'''
            <html>
              <body>
                <h2>账号验证 / Account Verification</h2>
                <p>您的验证码是 / Your verification code is:</p>
                <h1 style="color: #4CAF50; font-size: 32px; letter-spacing: 5px;">{code}</h1>
                <p>此验证码将在10分钟后过期。</p>
                <p>This code will expire in 10 minutes.</p>
                <hr>
                <p style="color: #999; font-size: 12px;">如果您没有请求此验证码，请忽略此邮件。</p>
                <p style="color: #999; font-size: 12px;">If you did not request this code, please ignore this email.</p>
              </body>
            </html>
            '''
            
            part1 = MIMEText(text, 'plain')
            part2 = MIMEText(html, 'html')
            
            msg.attach(part1)
            msg.attach(part2)
            
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            
            print(f"[真实] 验证邮件已发送到 {email}")
        except Exception as e:
            print(f"[错误] 发送邮件失败: {e}")
    """

    return True


@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    return jsonify(
        {
            "status": "ok",
            "service": "mock_registration_server",
            "timestamp": datetime.utcnow().isoformat(),
        }
    )


@app.route("/api/register", methods=["POST"])
def register():
    """
    注册接口
    接收邮箱地址，生成验证码并发送邮件
    """
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    username = data.get("username", "").strip()

    if not email:
        return jsonify({"success": False, "error": "Email is required"}), 400

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400

    # 检查是否已注册
    if email in registered_users:
        return jsonify({"success": False, "error": "Email already registered"}), 409

    # 生成验证码
    code = generate_verification_code()
    session_id = f"sess_{int(time.time())}_{random.randint(1000, 9999)}"

    # 存储会话
    registration_sessions[session_id] = {
        "email": email,
        "username": username,
        "code": code,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=10),
        "verified": False,
    }

    # 异步发送邮件（模拟延迟）
    def send_email_async():
        time.sleep(random.uniform(2, 5))  # 模拟邮件发送延迟
        send_verification_email(email, code)

    Thread(target=send_email_async, daemon=True).start()

    return jsonify(
        {
            "success": True,
            "session_id": session_id,
            "message": "Verification code sent to your email",
            "expires_in_seconds": 600,
        }
    )


@app.route("/api/verify", methods=["POST"])
def verify():
    """
    验证接口
    验证验证码是否正确
    """
    data = request.json or {}
    session_id = data.get("session_id", "")
    code = data.get("code", "").strip()

    if not session_id or not code:
        return jsonify(
            {"success": False, "error": "Session ID and code are required"}
        ), 400

    # 检查会话是否存在
    session = registration_sessions.get(session_id)
    if not session:
        return jsonify({"success": False, "error": "Invalid session ID"}), 404

    # 检查是否过期
    if datetime.utcnow() > session["expires_at"]:
        return jsonify({"success": False, "error": "Verification code expired"}), 410

    # 检查是否已验证
    if session["verified"]:
        return jsonify({"success": False, "error": "Already verified"}), 409

    # 验证码校验
    if code != session["code"]:
        return jsonify({"success": False, "error": "Invalid verification code"}), 400

    # 标记为已验证
    session["verified"] = True
    session["verified_at"] = datetime.utcnow()

    # 注册用户
    user_id = len(registered_users) + 1
    registered_users[session["email"]] = {
        "user_id": user_id,
        "email": session["email"],
        "username": session["username"],
        "registered_at": datetime.utcnow().isoformat(),
    }

    return jsonify(
        {
            "success": True,
            "message": "Registration completed successfully",
            "user": {
                "user_id": user_id,
                "email": session["email"],
                "username": session["username"],
            },
        }
    )


@app.route("/api/resend", methods=["POST"])
def resend():
    """
    重发验证码
    """
    data = request.json or {}
    session_id = data.get("session_id", "")

    if not session_id:
        return jsonify({"success": False, "error": "Session ID is required"}), 400

    # 检查会话是否存在
    session = registration_sessions.get(session_id)
    if not session:
        return jsonify({"success": False, "error": "Invalid session ID"}), 404

    # 检查是否已验证
    if session["verified"]:
        return jsonify({"success": False, "error": "Already verified"}), 409

    # 生成新验证码
    code = generate_verification_code()
    session["code"] = code
    session["expires_at"] = datetime.utcnow() + timedelta(minutes=10)

    # 异步发送邮件
    def send_email_async():
        time.sleep(random.uniform(2, 5))
        send_verification_email(session["email"], code)

    Thread(target=send_email_async, daemon=True).start()

    return jsonify(
        {
            "success": True,
            "message": "Verification code resent",
            "expires_in_seconds": 600,
        }
    )


@app.route("/api/users", methods=["GET"])
def list_users():
    """
    列出所有已注册用户（仅用于测试）
    """
    return jsonify(
        {
            "success": True,
            "count": len(registered_users),
            "users": list(registered_users.values()),
        }
    )


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    """
    列出所有注册会话（仅用于测试）
    """
    sessions = []
    for session_id, session in registration_sessions.items():
        sessions.append(
            {
                "session_id": session_id,
                "email": session["email"],
                "username": session["username"],
                "verified": session["verified"],
                "created_at": session["created_at"].isoformat(),
                "expires_at": session["expires_at"].isoformat(),
                "expired": datetime.utcnow() > session["expires_at"],
            }
        )

    return jsonify({"success": True, "count": len(sessions), "sessions": sessions})


@app.route("/api/reset", methods=["POST"])
def reset():
    """
    重置所有数据（仅用于测试）
    """
    registration_sessions.clear()
    registered_users.clear()

    return jsonify({"success": True, "message": "All data cleared"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5002))
    print(f"模拟注册服务器启动在端口 {port}")
    print(f"访问 http://localhost:{port}/health 检查服务状态")
    print(f"\n注意：此服务器仅用于测试，不会真实发送邮件")
    print(f"验证码会打印在控制台中\n")

    app.run(host="0.0.0.0", port=port, debug=True)
