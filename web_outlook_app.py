#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Outlook 邮件 Web 应用
基于 Flask 的 Web 界面，支持多邮箱管理和邮件查看
使用 SQLite 数据库存储邮箱信息，支持分组管理
支持 GPTMail 临时邮箱服务
"""

import email
import imaplib
import sqlite3
import os
import hashlib
import hmac
import secrets
import time
import json
import re
import uuid
import random
import bcrypt
import base64
import html
from datetime import datetime, timedelta
from email.header import decode_header
from typing import Optional, List, Dict, Any
from urllib.parse import quote
from flask import Flask, render_template, request, jsonify, g, session, redirect, url_for, Response
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# 尝试加载 .env（uv venv 不会自动注入）
try:
    from dotenv import load_dotenv
    from pathlib import Path

    _dotenv_path = Path(__file__).resolve().parent / ".env"
    if _dotenv_path.exists():
        load_dotenv(_dotenv_path)
    else:
        load_dotenv()
except Exception as _e:
    print(f"Warning: failed to load .env: {_e}")

# 尝试导入 Flask-WTF CSRF 保护
try:
    from flask_wtf.csrf import CSRFProtect, generate_csrf
    CSRF_AVAILABLE = True
except ImportError:
    CSRF_AVAILABLE = False
    print("Warning: flask-wtf not installed. CSRF protection is disabled. Install with: pip install flask-wtf")

app = Flask(__name__)
# 强制从环境变量读取 secret_key，不提供默认值以防止安全漏洞
secret_key = os.getenv("SECRET_KEY")
if not secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is required. "
        "Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
    )
app.secret_key = secret_key
# 设置 session 过期时间（默认 7 天）
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * 7  # 7 天

# 初始化 CSRF 保护（如果可用）
if CSRF_AVAILABLE:
    csrf = CSRFProtect(app)
    # 配置 CSRF
    app.config['WTF_CSRF_TIME_LIMIT'] = None  # CSRF token 不过期
    app.config['WTF_CSRF_SSL_STRICT'] = False  # 允许非HTTPS环境（开发环境）
    print("CSRF protection enabled")

    # 创建CSRF排除装饰器
    def csrf_exempt(f):
        return csrf.exempt(f)
else:
    csrf = None
    # 显式禁用CSRF保护
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['WTF_CSRF_CHECK_DEFAULT'] = False
    print("CSRF protection disabled")

    # 创建空装饰器
    def csrf_exempt(f):
        return f

# 登录密码配置（可以修改为你想要的密码）
LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD", "admin123")

# ==================== 配置 ====================
# Token 端点
TOKEN_URL_LIVE = "https://login.live.com/oauth20_token.srf"
TOKEN_URL_GRAPH = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
TOKEN_URL_IMAP = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

# IMAP 服务器配置
IMAP_SERVER_OLD = "outlook.office365.com"
IMAP_SERVER_NEW = "outlook.live.com"
IMAP_PORT = 993

# 数据库文件
DATABASE = os.getenv("DATABASE_PATH", "data/outlook_accounts.db")

# GPTMail API 配置
GPTMAIL_BASE_URL = os.getenv("GPTMAIL_BASE_URL", "https://mail.chatgpt.org.uk")
GPTMAIL_API_KEY = os.getenv("GPTMAIL_API_KEY", "gpt-test")  # 测试 API Key，可以修改为正式 Key

# 临时邮箱分组 ID（系统保留）
TEMP_EMAIL_GROUP_ID = -1

# OAuth 配置
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "24d9a0ed-8787-4584-883c-2fd79308940a")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8080")
OAUTH_SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/User.Read"
]

# 刷新任务配置
REFRESH_MAX_WORKERS = int(os.getenv("REFRESH_MAX_WORKERS", "12"))
REFRESH_BATCH_SIZE = int(os.getenv("REFRESH_BATCH_SIZE", "80"))
REFRESH_BACKOFF_RETRIES = int(os.getenv("REFRESH_BACKOFF_RETRIES", "3"))
REFRESH_BACKOFF_BASE = float(os.getenv("REFRESH_BACKOFF_BASE", "1.0"))
REFRESH_BACKOFF_MAX = float(os.getenv("REFRESH_BACKOFF_MAX", "10.0"))
REFRESH_RESUME_TTL_SECONDS = int(os.getenv("REFRESH_RESUME_TTL_SECONDS", "21600"))  # 6小时


# ==================== 登录速率限制 ====================

# 存储登录失败记录 {ip: {'count': int, 'last_attempt': timestamp, 'locked_until': timestamp}}
login_attempts = {}

# 速率限制配置
MAX_LOGIN_ATTEMPTS = 5  # 最大失败次数
LOCKOUT_DURATION = 300  # 锁定时长（秒）- 5分钟
ATTEMPT_WINDOW = 600    # 失败计数窗口（秒）- 10分钟


def check_rate_limit(ip: str) -> tuple[bool, Optional[int]]:
    """
    检查 IP 是否被速率限制
    返回: (是否允许登录, 剩余锁定秒数)
    """
    current_time = time.time()

    if ip not in login_attempts:
        return True, None

    attempt_data = login_attempts[ip]

    # 检查是否在锁定期内
    if 'locked_until' in attempt_data and current_time < attempt_data['locked_until']:
        remaining = int(attempt_data['locked_until'] - current_time)
        return False, remaining

    # 检查失败计数是否过期
    if current_time - attempt_data.get('last_attempt', 0) > ATTEMPT_WINDOW:
        # 重置计数
        login_attempts[ip] = {'count': 0, 'last_attempt': current_time}
        return True, None

    # 检查失败次数
    if attempt_data.get('count', 0) >= MAX_LOGIN_ATTEMPTS:
        # 锁定账号
        attempt_data['locked_until'] = current_time + LOCKOUT_DURATION
        remaining = LOCKOUT_DURATION
        return False, remaining

    return True, None


def record_login_failure(ip: str):
    """记录登录失败"""
    current_time = time.time()

    if ip not in login_attempts:
        login_attempts[ip] = {'count': 1, 'last_attempt': current_time}
    else:
        attempt_data = login_attempts[ip]
        # 如果在窗口期内，增加计数
        if current_time - attempt_data.get('last_attempt', 0) <= ATTEMPT_WINDOW:
            attempt_data['count'] = attempt_data.get('count', 0) + 1
        else:
            # 重置计数
            attempt_data['count'] = 1
        attempt_data['last_attempt'] = current_time


def reset_login_attempts(ip: str):
    """重置登录失败记录（登录成功时调用）"""
    if ip in login_attempts:
        del login_attempts[ip]


# ==================== 密码安全工具 ====================

def hash_password(password: str) -> str:
    """使用 bcrypt 哈希密码"""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def verify_password(password: str, hashed: str) -> bool:
    """验证密码是否匹配哈希值"""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


def is_password_hashed(password: str) -> bool:
    """检查密码是否已经是 bcrypt 哈希值"""
    return password.startswith('$2b$') or password.startswith('$2a$') or password.startswith('$2y$')


# ==================== 数据加密工具 ====================

# 全局加密器实例
_cipher_suite = None


def get_encryption_key() -> bytes:
    """
    从 SECRET_KEY 派生加密密钥
    使用 PBKDF2 从 SECRET_KEY 派生 32 字节密钥
    """
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        raise RuntimeError("SECRET_KEY is required for encryption")

    # 使用固定盐（因为我们需要确保重启后能解密）
    # 注意：这里使用固定盐是为了确保密钥一致性，安全性依赖于 SECRET_KEY 的强度
    salt = b'outlook_email_encryption_salt_v1'

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode()))
    return key


def get_cipher() -> Fernet:
    """获取加密器实例（单例模式）"""
    global _cipher_suite
    if _cipher_suite is None:
        key = get_encryption_key()
        _cipher_suite = Fernet(key)
    return _cipher_suite


def encrypt_data(data: str) -> str:
    """
    加密敏感数据
    返回 base64 编码的加密字符串，带有 'enc:' 前缀标识
    """
    if not data:
        return data

    # 如果已经加密，直接返回
    if data.startswith('enc:'):
        return data

    cipher = get_cipher()
    encrypted = cipher.encrypt(data.encode('utf-8'))
    return 'enc:' + encrypted.decode('utf-8')


def decrypt_data(encrypted_data: str) -> str:
    """
    解密敏感数据
    如果数据未加密（没有 'enc:' 前缀），直接返回原始数据
    """
    if not encrypted_data:
        return encrypted_data

    # 如果没有加密标识，返回原始数据（向后兼容）
    if not encrypted_data.startswith('enc:'):
        return encrypted_data

    try:
        cipher = get_cipher()
        encrypted_bytes = encrypted_data[4:].encode('utf-8')  # 移除 'enc:' 前缀
        decrypted = cipher.decrypt(encrypted_bytes)
        return decrypted.decode('utf-8')
    except Exception as e:
        # 解密失败，可能是密钥变更或数据损坏
        import sys
        error_msg = f"Failed to decrypt data: {str(e)}"
        print(f"[ERROR] {error_msg}", file=sys.stderr)
        print(f"[ERROR] Data preview: {encrypted_data[:50]}...", file=sys.stderr)
        print(f"[ERROR] This usually means SECRET_KEY has changed or data is corrupted", file=sys.stderr)
        raise RuntimeError(error_msg)


def is_encrypted(data: str) -> bool:
    """检查数据是否已加密"""
    return data and data.startswith('enc:')


# ==================== 错误处理工具 ====================

def generate_trace_id() -> str:
    return uuid.uuid4().hex


def sanitize_error_details(details: Optional[str]) -> str:
    if not details:
        return ""
    sanitized = details
    patterns = [
        (r'(?i)(bearer\s+)[A-Za-z0-9\-._~\+/]+=*', r'\1***'),
        (r'(?i)(refresh_token|access_token|token|password|passwd|secret)\s*[:=]\s*\"?[A-Za-z0-9\-._~\+/]+=*\"?', r'\1=***'),
        (r'(?i)(\"refresh_token\"\s*:\s*\")[^\"]+(\"?)', r'\1***\2'),
        (r'(?i)(\"access_token\"\s*:\s*\")[^\"]+(\"?)', r'\1***\2'),
        (r'(?i)(\"password\"\s*:\s*\")[^\"]+(\"?)', r'\1***\2'),
        (r'(?i)(client_secret|refresh_token|access_token)=[^&\s]+', r'\1=***')
    ]
    for pattern, repl in patterns:
        sanitized = re.sub(pattern, repl, sanitized)
    return sanitized


def build_error_payload(
    code: str,
    message: str,
    err_type: str = "Error",
    status: int = 500,
    details: Any = None,
    trace_id: Optional[str] = None
) -> Dict[str, Any]:
    if details is not None and not isinstance(details, str):
        try:
            details = json.dumps(details, ensure_ascii=True)
        except Exception:
            details = str(details)
    sanitized_details = sanitize_error_details(details) if details else ""
    trace_id_value = trace_id or generate_trace_id()
    payload = {
        "code": code,
        "message": message,
        "type": err_type,
        "status": status,
        "details": sanitized_details,
        "trace_id": trace_id_value
    }
    try:
        app.logger.error(
            "trace_id=%s code=%s status=%s type=%s details=%s",
            trace_id_value,
            code,
            status,
            err_type,
            sanitized_details
        )
    except Exception:
        pass
    return payload


def get_response_details(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text or response.reason


# ==================== 数据库操作 ====================

def configure_sqlite(conn: sqlite3.Connection) -> None:
    """配置 SQLite 连接参数（WAL + busy_timeout）"""
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
    except Exception:
        pass

def get_db():
    """获取数据库连接"""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, timeout=10)
        configure_sqlite(db)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    """关闭数据库连接"""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DATABASE, timeout=10)
    configure_sqlite(conn)
    cursor = conn.cursor()
    
    # 创建设置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 创建分组表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            color TEXT DEFAULT '#1a1a1a',
            is_system INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 创建邮箱账号表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT,
            client_id TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            group_id INTEGER,
            remark TEXT,
            status TEXT DEFAULT 'active',
            last_refresh_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups (id)
        )
    ''')
    
    # 创建临时邮箱表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS temp_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 创建临时邮件表（存储从 GPTMail 获取的邮件）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS temp_email_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            email_address TEXT NOT NULL,
            from_address TEXT,
            subject TEXT,
            content TEXT,
            html_content TEXT,
            has_html INTEGER DEFAULT 0,
            timestamp INTEGER,
            raw_content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (email_address) REFERENCES temp_emails (email)
        )
    ''')

    # 创建账号刷新记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS account_refresh_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            account_email TEXT NOT NULL,
            refresh_type TEXT DEFAULT 'manual',
            status TEXT NOT NULL,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
        )
    ''')

    # 创建刷新运行记录表（用于统计与趋势）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS refresh_runs (
            run_id TEXT PRIMARY KEY,
            refresh_type TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            finished_at TIMESTAMP,
            total INTEGER DEFAULT 0,
            total_all INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            resumed INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            group_id INTEGER,
            max_workers INTEGER,
            batch_size INTEGER,
            delay_seconds INTEGER,
            status TEXT DEFAULT 'running'
        )
    ''')

    # 外部领取邮箱租约表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS account_leases (
            lease_id TEXT PRIMARY KEY,
            account_id INTEGER UNIQUE NOT NULL,
            owner TEXT,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE
        )
    ''')

    # 创建审计日志表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            user_ip TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 创建标签表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            color TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 创建账号标签关联表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS account_tags (
            account_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account_id, tag_id),
            FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE
        )
    ''')

    # 检查并添加缺失的列（数据库迁移）
    cursor.execute("PRAGMA table_info(accounts)")
    columns = [col[1] for col in cursor.fetchall()]

    if 'group_id' not in columns:
        cursor.execute('ALTER TABLE accounts ADD COLUMN group_id INTEGER DEFAULT 1')
    if 'remark' not in columns:
        cursor.execute('ALTER TABLE accounts ADD COLUMN remark TEXT')
    if 'status' not in columns:
        cursor.execute("ALTER TABLE accounts ADD COLUMN status TEXT DEFAULT 'active'")
    if 'updated_at' not in columns:
        cursor.execute('ALTER TABLE accounts ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
    if 'last_refresh_at' not in columns:
        cursor.execute('ALTER TABLE accounts ADD COLUMN last_refresh_at TIMESTAMP')
    
    # 检查 groups 表是否有 is_system 列
    cursor.execute("PRAGMA table_info(groups)")
    group_columns = [col[1] for col in cursor.fetchall()]
    if 'is_system' not in group_columns:
        cursor.execute('ALTER TABLE groups ADD COLUMN is_system INTEGER DEFAULT 0')

    # 检查 refresh_runs 表的缺失列
    cursor.execute("PRAGMA table_info(refresh_runs)")
    refresh_columns = [col[1] for col in cursor.fetchall()]
    if 'total_all' not in refresh_columns:
        cursor.execute('ALTER TABLE refresh_runs ADD COLUMN total_all INTEGER DEFAULT 0')
    if 'group_id' not in refresh_columns:
        cursor.execute('ALTER TABLE refresh_runs ADD COLUMN group_id INTEGER')
    if 'max_workers' not in refresh_columns:
        cursor.execute('ALTER TABLE refresh_runs ADD COLUMN max_workers INTEGER')
    if 'batch_size' not in refresh_columns:
        cursor.execute('ALTER TABLE refresh_runs ADD COLUMN batch_size INTEGER')
    if 'delay_seconds' not in refresh_columns:
        cursor.execute('ALTER TABLE refresh_runs ADD COLUMN delay_seconds INTEGER')
    
    # 创建默认分组
    cursor.execute('''
        INSERT OR IGNORE INTO groups (name, description, color)
        VALUES ('默认分组', '未分组的邮箱', '#666666')
    ''')
    
    # 创建临时邮箱分组（系统分组）
    cursor.execute('''
        INSERT OR IGNORE INTO groups (name, description, color, is_system)
        VALUES ('临时邮箱', 'GPTMail 临时邮箱服务', '#00bcf2', 1)
    ''')
    
    # 初始化默认设置
    # 检查是否已有密码设置
    cursor.execute("SELECT value FROM settings WHERE key = 'login_password'")
    existing_password = cursor.fetchone()

    if existing_password:
        # 如果存在密码但是明文，则迁移为哈希
        password_value = existing_password[0]
        if not is_password_hashed(password_value):
            hashed_password = hash_password(password_value)
            cursor.execute('''
                UPDATE settings SET value = ? WHERE key = 'login_password'
            ''', (hashed_password,))
    else:
        # 首次初始化，哈希默认密码
        hashed_password = hash_password(LOGIN_PASSWORD)
        cursor.execute('''
            INSERT INTO settings (key, value)
            VALUES ('login_password', ?)
        ''', (hashed_password,))

    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('gptmail_api_key', ?)
    ''', (GPTMAIL_API_KEY,))

    # 初始化刷新配置
    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('refresh_interval_days', '30')
    ''')

    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('refresh_delay_seconds', '5')
    ''')

    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('refresh_max_workers', '12')
    ''')

    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('refresh_batch_size', '80')
    ''')

    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('refresh_cron', '0 2 * * *')
    ''')

    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('use_cron_schedule', 'false')
    ''')

    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value)
        VALUES ('enable_scheduled_refresh', 'true')
    ''')

    # 创建索引以优化查询性能
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_accounts_last_refresh_at
        ON accounts(last_refresh_at)
    ''')

    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_accounts_status
        ON accounts(status)
    ''')

    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_account_refresh_logs_account_id
        ON account_refresh_logs(account_id)
    ''')

    # 迁移现有明文数据为加密数据
    migrate_sensitive_data(conn)

    conn.commit()
    conn.close()


def migrate_sensitive_data(conn):
    """迁移现有明文敏感数据为加密数据"""
    cursor = conn.cursor()

    # 获取所有账号
    cursor.execute('SELECT id, password, refresh_token FROM accounts')
    accounts = cursor.fetchall()

    migrated_count = 0
    for account_id, password, refresh_token in accounts:
        needs_update = False
        new_password = password
        new_refresh_token = refresh_token

        # 检查并加密 password
        if password and not is_encrypted(password):
            new_password = encrypt_data(password)
            needs_update = True

        # 检查并加密 refresh_token
        if refresh_token and not is_encrypted(refresh_token):
            new_refresh_token = encrypt_data(refresh_token)
            needs_update = True

        # 更新数据库
        if needs_update:
            cursor.execute('''
                UPDATE accounts
                SET password = ?, refresh_token = ?
                WHERE id = ?
            ''', (new_password, new_refresh_token, account_id))
            migrated_count += 1

    if migrated_count > 0:
        print(f"已迁移 {migrated_count} 个账号的敏感数据为加密存储")


# ==================== 应用初始化 ====================

def init_app():
    """初始化应用（确保目录和数据库存在）"""
    # 确保 templates 目录存在
    os.makedirs('templates', exist_ok=True)
    
    # 确保数据目录存在
    data_dir = os.path.dirname(DATABASE)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    
    # 初始化数据库
    init_db()
    
    print("=" * 60)
    print("Outlook 邮件 Web 应用已初始化")
    print(f"数据库文件: {DATABASE}")
    print(f"GPTMail API: {GPTMAIL_BASE_URL}")
    print("=" * 60)


# 在模块加载时初始化应用
init_app()


# ==================== 设置操作 ====================

def get_setting(key: str, default: str = '') -> str:
    """获取设置值"""
    db = get_db()
    cursor = db.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cursor.fetchone()
    return row['value'] if row else default


def set_setting(key: str, value: str) -> bool:
    """设置值"""
    db = get_db()
    try:
        db.execute('''
            INSERT OR REPLACE INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, value))
        db.commit()
        return True
    except Exception:
        return False


def get_all_settings() -> Dict[str, str]:
    """获取所有设置"""
    db = get_db()
    cursor = db.execute('SELECT key, value FROM settings')
    rows = cursor.fetchall()
    return {row['key']: row['value'] for row in rows}


def get_login_password() -> str:
    """获取登录密码（优先从数据库读取）"""
    password = get_setting('login_password')
    return password if password else LOGIN_PASSWORD


def get_gptmail_api_key() -> str:
    """获取 GPTMail API Key（优先从数据库读取）"""
    api_key = get_setting('gptmail_api_key')
    return api_key if api_key else GPTMAIL_API_KEY


# ==================== 分组操作 ====================

def load_groups() -> List[Dict]:
    """加载所有分组（临时邮箱分组排在最前面）"""
    db = get_db()
    # 使用 CASE 语句让临时邮箱分组排在最前面
    cursor = db.execute('''
        SELECT * FROM groups
        ORDER BY
            CASE WHEN name = '临时邮箱' THEN 0 ELSE 1 END,
            id
    ''')
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def get_group_by_id(group_id: int) -> Optional[Dict]:
    """根据 ID 获取分组"""
    db = get_db()
    cursor = db.execute('SELECT * FROM groups WHERE id = ?', (group_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def add_group(name: str, description: str = '', color: str = '#1a1a1a') -> Optional[int]:
    """添加分组"""
    db = get_db()
    try:
        cursor = db.execute('''
            INSERT INTO groups (name, description, color)
            VALUES (?, ?, ?)
        ''', (name, description, color))
        db.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def update_group(group_id: int, name: str, description: str, color: str) -> bool:
    """更新分组"""
    db = get_db()
    try:
        db.execute('''
            UPDATE groups SET name = ?, description = ?, color = ?
            WHERE id = ?
        ''', (name, description, color, group_id))
        db.commit()
        return True
    except Exception:
        return False


def delete_group(group_id: int) -> bool:
    """删除分组（将该分组下的邮箱移到默认分组）"""
    db = get_db()
    try:
        # 将该分组下的邮箱移到默认分组（id=1）
        db.execute('UPDATE accounts SET group_id = 1 WHERE group_id = ?', (group_id,))
        # 删除分组（不能删除默认分组）
        if group_id != 1:
            db.execute('DELETE FROM groups WHERE id = ?', (group_id,))
        db.commit()
        return True
    except Exception:
        return False


def get_group_account_count(group_id: int) -> int:
    """获取分组下的邮箱数量"""
    db = get_db()
    cursor = db.execute('SELECT COUNT(*) as count FROM accounts WHERE group_id = ?', (group_id,))
    row = cursor.fetchone()
    return row['count'] if row else 0


def get_group_account_status_counts(group_id: int) -> Dict[str, int]:
    """获取分组下邮箱的最近刷新状态统计"""
    db = get_db()
    row = db.execute('''
        SELECT
            SUM(CASE WHEN l.status = 'success' THEN 1 ELSE 0 END) as success_count,
            SUM(CASE WHEN l.status = 'failed' THEN 1 ELSE 0 END) as failed_count
        FROM accounts a
        LEFT JOIN (
            SELECT l1.account_id, l1.status
            FROM account_refresh_logs l1
            INNER JOIN (
                SELECT account_id, MAX(created_at) as max_created
                FROM account_refresh_logs
                GROUP BY account_id
            ) latest ON l1.account_id = latest.account_id AND l1.created_at = latest.max_created
        ) l ON a.id = l.account_id
        WHERE a.group_id = ?
    ''', (group_id,)).fetchone()
    return {
        'success_count': row['success_count'] or 0,
        'failed_count': row['failed_count'] or 0
    }


# ==================== 邮箱账号操作 ====================

def load_accounts(group_id: int = None) -> List[Dict]:
    """从数据库加载邮箱账号"""
    db = get_db()
    if group_id:
        cursor = db.execute('''
            SELECT a.*, g.name as group_name, g.color as group_color
            FROM accounts a
            LEFT JOIN groups g ON a.group_id = g.id
            WHERE a.group_id = ?
            ORDER BY a.created_at DESC
        ''', (group_id,))
    else:
        cursor = db.execute('''
            SELECT a.*, g.name as group_name, g.color as group_color
            FROM accounts a
            LEFT JOIN groups g ON a.group_id = g.id
            ORDER BY a.created_at DESC
        ''')
    rows = cursor.fetchall()
    accounts = []
    for row in rows:
        account = dict(row)
        # 解密敏感字段
        if account.get('password'):
            try:
                account['password'] = decrypt_data(account['password'])
            except Exception:
                pass  # 解密失败保持原值
        if account.get('refresh_token'):
            try:
                account['refresh_token'] = decrypt_data(account['refresh_token'])
            except Exception:
                pass  # 解密失败保持原值
        
        # 加载账号标签
        account['tags'] = get_account_tags(account['id'])
        accounts.append(account)
    return accounts


# ==================== 标签管理 ====================

def get_tags() -> List[Dict]:
    """获取所有标签"""
    db = get_db()
    cursor = db.execute('SELECT * FROM tags ORDER BY created_at DESC')
    return [dict(row) for row in cursor.fetchall()]


def add_tag(name: str, color: str) -> Optional[int]:
    """添加标签"""
    db = get_db()
    try:
        cursor = db.execute(
            'INSERT INTO tags (name, color) VALUES (?, ?)',
            (name, color)
        )
        db.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def delete_tag(tag_id: int) -> bool:
    """删除标签"""
    db = get_db()
    cursor = db.execute('DELETE FROM tags WHERE id = ?', (tag_id,))
    db.commit()
    return cursor.rowcount > 0


def get_account_tags(account_id: int) -> List[Dict]:
    """获取账号的标签"""
    db = get_db()
    cursor = db.execute('''
        SELECT t.*
        FROM tags t
        JOIN account_tags at ON t.id = at.tag_id
        WHERE at.account_id = ?
        ORDER BY t.created_at DESC
    ''', (account_id,))
    return [dict(row) for row in cursor.fetchall()]


def add_account_tag(account_id: int, tag_id: int) -> bool:
    """给账号添加标签"""
    db = get_db()
    try:
        db.execute(
            'INSERT OR IGNORE INTO account_tags (account_id, tag_id) VALUES (?, ?)',
            (account_id, tag_id)
        )
        db.commit()
        return True
    except Exception:
        return False


def remove_account_tag(account_id: int, tag_id: int) -> bool:
    """移除账号标签"""
    db = get_db()
    db.execute(
        'DELETE FROM account_tags WHERE account_id = ? AND tag_id = ?',
        (account_id, tag_id)
    )
    db.commit()
    return True



def get_account_by_email(email_addr: str) -> Optional[Dict]:
    """根据邮箱地址获取账号"""
    db = get_db()
    cursor = db.execute('SELECT * FROM accounts WHERE email = ?', (email_addr,))
    row = cursor.fetchone()
    if not row:
        return None
    account = dict(row)
    # 解密敏感字段
    if account.get('password'):
        try:
            account['password'] = decrypt_data(account['password'])
        except Exception:
            pass
    if account.get('refresh_token'):
        try:
            account['refresh_token'] = decrypt_data(account['refresh_token'])
        except Exception:
            pass
    return account


def get_account_by_id(account_id: int) -> Optional[Dict]:
    """根据 ID 获取账号"""
    db = get_db()
    cursor = db.execute('''
        SELECT a.*, g.name as group_name, g.color as group_color
        FROM accounts a
        LEFT JOIN groups g ON a.group_id = g.id
        WHERE a.id = ?
    ''', (account_id,))
    row = cursor.fetchone()
    if not row:
        return None
    account = dict(row)
    # 解密敏感字段
    if account.get('password'):
        try:
            account['password'] = decrypt_data(account['password'])
        except Exception:
            pass
    if account.get('refresh_token'):
        try:
            account['refresh_token'] = decrypt_data(account['refresh_token'])
        except Exception:
            pass
    return account


def add_account(email_addr: str, password: str, client_id: str, refresh_token: str,
                group_id: int = 1, remark: str = '') -> bool:
    """添加邮箱账号"""
    db = get_db()
    try:
        # 加密敏感字段
        encrypted_password = encrypt_data(password) if password else password
        encrypted_refresh_token = encrypt_data(refresh_token) if refresh_token else refresh_token

        db.execute('''
            INSERT INTO accounts (email, password, client_id, refresh_token, group_id, remark)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (email_addr, encrypted_password, client_id, encrypted_refresh_token, group_id, remark))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def update_account(account_id: int, email_addr: str, password: str, client_id: str,
                   refresh_token: str, group_id: int, remark: str, status: str) -> bool:
    """更新邮箱账号"""
    db = get_db()
    try:
        # 加密敏感字段
        encrypted_password = encrypt_data(password) if password else password
        encrypted_refresh_token = encrypt_data(refresh_token) if refresh_token else refresh_token

        db.execute('''
            UPDATE accounts
            SET email = ?, password = ?, client_id = ?, refresh_token = ?,
                group_id = ?, remark = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (email_addr, encrypted_password, client_id, encrypted_refresh_token, group_id, remark, status, account_id))
        db.commit()
        return True
    except Exception:
        return False


def delete_account_by_id(account_id: int) -> bool:
    """删除邮箱账号"""
    db = get_db()
    try:
        db.execute('DELETE FROM account_leases WHERE account_id = ?', (account_id,))
        db.execute('DELETE FROM account_tags WHERE account_id = ?', (account_id,))
        db.execute('DELETE FROM account_refresh_logs WHERE account_id = ?', (account_id,))
        db.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
        db.commit()
        return True
    except Exception:
        return False


def delete_account_by_email(email_addr: str) -> bool:
    """根据邮箱地址删除账号"""
    db = get_db()
    try:
        row = db.execute('SELECT id FROM accounts WHERE email = ?', (email_addr,)).fetchone()
        if row:
            account_id = row['id']
            db.execute('DELETE FROM account_leases WHERE account_id = ?', (account_id,))
            db.execute('DELETE FROM account_tags WHERE account_id = ?', (account_id,))
            db.execute('DELETE FROM account_refresh_logs WHERE account_id = ?', (account_id,))
        db.execute('DELETE FROM accounts WHERE email = ?', (email_addr,))
        db.commit()
        return True
    except Exception:
        return False


# ==================== 工具函数 ====================

def sanitize_input(text: str, max_length: int = 500) -> str:
    """
    净化用户输入，防止XSS攻击
    - 转义HTML特殊字符
    - 限制长度
    - 移除控制字符
    """
    if not text:
        return ""

    # 限制长度
    text = text[:max_length]

    # 移除控制字符（保留换行和制表符）
    text = ''.join(char for char in text if char.isprintable() or char in '\n\t')

    # 转义HTML特殊字符
    text = html.escape(text, quote=True)

    return text


def log_audit(action: str, resource_type: str, resource_id: str = None, details: str = None):
    """
    记录审计日志
    :param action: 操作类型（如 'export', 'delete', 'update'）
    :param resource_type: 资源类型（如 'account', 'group'）
    :param resource_id: 资源ID
    :param details: 详细信息
    """
    try:
        db = get_db()
        user_ip = request.remote_addr if request else 'unknown'
        db.execute('''
            INSERT INTO audit_logs (action, resource_type, resource_id, user_ip, details)
            VALUES (?, ?, ?, ?, ?)
        ''', (action, resource_type, resource_id, user_ip, details))
        db.commit()
    except Exception:
        # 审计日志失败不应影响主流程
        pass


def decode_header_value(header_value: str) -> str:
    """解码邮件头字段"""
    if not header_value:
        return ""
    try:
        decoded_parts = decode_header(str(header_value))
        decoded_string = ""
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                try:
                    decoded_string += part.decode(charset if charset else 'utf-8', 'replace')
                except (LookupError, UnicodeDecodeError):
                    decoded_string += part.decode('utf-8', 'replace')
            else:
                decoded_string += str(part)
        return decoded_string
    except Exception:
        return str(header_value) if header_value else ""


def get_email_body(msg) -> str:
    """提取邮件正文"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or 'utf-8'
                    body = payload.decode(charset, errors='replace')
                    break
                except Exception:
                    continue
            elif content_type == "text/html" and "attachment" not in content_disposition and not body:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or 'utf-8'
                    body = payload.decode(charset, errors='replace')
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or 'utf-8'
            body = payload.decode(charset, errors='replace')
        except Exception:
            body = str(msg.get_payload())
    
    return body


def parse_account_string(account_str: str) -> Optional[Dict]:
    """
    解析账号字符串
    格式: email----password----client_id----refresh_token
    """
    parts = account_str.strip().split('----')
    if len(parts) >= 4:
        return {
            'email': parts[0],
            'password': parts[1],
            'client_id': parts[2],
            'refresh_token': parts[3]
        }
    return None


# ==================== Graph API 方式 ====================

def get_access_token_graph_result(client_id: str, refresh_token: str) -> Dict[str, Any]:
    """获取 Graph API access_token（包含错误详情）"""
    try:
        res = requests.post(
            TOKEN_URL_GRAPH,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "https://graph.microsoft.com/.default"
            },
            timeout=30
        )

        if res.status_code != 200:
            details = get_response_details(res)
            return {
                "success": False,
                "error": build_error_payload(
                    "GRAPH_TOKEN_FAILED",
                    "获取访问令牌失败",
                    "GraphAPIError",
                    res.status_code,
                    details
                )
            }

        payload = res.json()
        access_token = payload.get("access_token")
        if not access_token:
            return {
                "success": False,
                "error": build_error_payload(
                    "GRAPH_TOKEN_MISSING",
                    "获取访问令牌失败",
                    "GraphAPIError",
                    res.status_code,
                    payload
                )
            }

        return {"success": True, "access_token": access_token}
    except Exception as exc:
        return {
            "success": False,
            "error": build_error_payload(
                "GRAPH_TOKEN_EXCEPTION",
                "获取访问令牌失败",
                type(exc).__name__,
                500,
                str(exc)
            )
        }


def get_access_token_graph(client_id: str, refresh_token: str) -> Optional[str]:
    """获取 Graph API access_token"""
    result = get_access_token_graph_result(client_id, refresh_token)
    if result.get("success"):
        return result.get("access_token")
    return None


def get_emails_graph(client_id: str, refresh_token: str, folder: str = 'inbox', skip: int = 0, top: int = 20) -> Dict[str, Any]:
    """使用 Graph API 获取邮件列表（支持分页和文件夹选择）"""
    token_result = get_access_token_graph_result(client_id, refresh_token)
    if not token_result.get("success"):
        return {"success": False, "error": token_result.get("error")}

    access_token = token_result.get("access_token")

    try:
        # 根据文件夹类型选择 API 端点
        # 使用 Well-known folder names，这些是 Microsoft Graph API 的标准文件夹名称
        folder_map = {
            'inbox': 'inbox',
            'junkemail': 'junkemail',  # 垃圾邮件的标准名称
            'deleteditems': 'deleteditems',  # 已删除邮件的标准名称
            'trash': 'deleteditems'  # 垃圾箱的别名
        }
        folder_name = folder_map.get(folder.lower(), 'inbox')

        url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder_name}/messages"
        params = {
            "$top": top,
            "$skip": skip,
            "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview",
            "$orderby": "receivedDateTime desc"
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": "outlook.body-content-type='text'"
        }

        res = requests.get(url, headers=headers, params=params, timeout=30)

        if res.status_code != 200:
            details = get_response_details(res)
            return {
                "success": False,
                "error": build_error_payload(
                    "EMAIL_FETCH_FAILED",
                    "获取邮件失败，请检查账号配置",
                    "GraphAPIError",
                    res.status_code,
                    details
                )
            }

        return {"success": True, "emails": res.json().get("value", [])}
    except Exception as exc:
        return {
            "success": False,
            "error": build_error_payload(
                "EMAIL_FETCH_FAILED",
                "获取邮件失败，请检查账号配置",
                type(exc).__name__,
                500,
                str(exc)
            )
        }


def get_email_detail_graph(client_id: str, refresh_token: str, message_id: str) -> Optional[Dict]:
    """使用 Graph API 获取邮件详情"""
    access_token = get_access_token_graph(client_id, refresh_token)
    if not access_token:
        return None
    
    try:
        url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
        params = {
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,hasAttachments,body,bodyPreview"
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": "outlook.body-content-type='html'"
        }
        
        res = requests.get(url, headers=headers, params=params, timeout=30)
        
        if res.status_code != 200:
            return None
        
        return res.json()
    except Exception:
        return None


# ==================== IMAP 方式 ====================

def get_access_token_imap_result(client_id: str, refresh_token: str) -> Dict[str, Any]:
    """获取 IMAP access_token（包含错误详情）"""
    try:
        res = requests.post(
            TOKEN_URL_IMAP,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
            },
            timeout=30
        )

        if res.status_code != 200:
            details = get_response_details(res)
            return {
                "success": False,
                "error": build_error_payload(
                    "IMAP_TOKEN_FAILED",
                    "获取访问令牌失败",
                    "IMAPError",
                    res.status_code,
                    details
                )
            }

        payload = res.json()
        access_token = payload.get("access_token")
        if not access_token:
            return {
                "success": False,
                "error": build_error_payload(
                    "IMAP_TOKEN_MISSING",
                    "获取访问令牌失败",
                    "IMAPError",
                    res.status_code,
                    payload
                )
            }

        return {"success": True, "access_token": access_token}
    except Exception as exc:
        return {
            "success": False,
            "error": build_error_payload(
                "IMAP_TOKEN_EXCEPTION",
                "获取访问令牌失败",
                type(exc).__name__,
                500,
                str(exc)
            )
        }


def get_access_token_imap(client_id: str, refresh_token: str) -> Optional[str]:
    """获取 IMAP access_token"""
    result = get_access_token_imap_result(client_id, refresh_token)
    if result.get("success"):
        return result.get("access_token")
    return None


def get_emails_imap(account: str, client_id: str, refresh_token: str, folder: str = 'inbox', skip: int = 0, top: int = 20) -> Dict[str, Any]:
    """使用 IMAP 获取邮件列表（支持分页和文件夹选择）- 默认使用新版服务器"""
    return get_emails_imap_with_server(account, client_id, refresh_token, folder, skip, top, IMAP_SERVER_NEW)


def get_emails_imap_with_server(account: str, client_id: str, refresh_token: str, folder: str = 'inbox', skip: int = 0, top: int = 20, server: str = IMAP_SERVER_NEW) -> Dict[str, Any]:
    """使用 IMAP 获取邮件列表（支持分页、文件夹选择和服务器选择）"""
    token_result = get_access_token_imap_result(client_id, refresh_token)
    if not token_result.get("success"):
        return {"success": False, "error": token_result.get("error")}

    access_token = token_result.get("access_token")

    connection = None
    try:
        connection = imaplib.IMAP4_SSL(server, IMAP_PORT)
        auth_string = f"user={account}\1auth=Bearer {access_token}\1\1".encode('utf-8')
        connection.authenticate('XOAUTH2', lambda x: auth_string)

        # 根据文件夹类型选择 IMAP 文件夹
        # 尝试多种可能的文件夹名称
        folder_map = {
            'inbox': ['"INBOX"', 'INBOX'],
            'junkemail': ['"Junk"', '"Junk Email"', 'Junk', '"垃圾邮件"'],
            'deleteditems': ['"Deleted"', '"Deleted Items"', '"Trash"', 'Deleted', '"已删除邮件"'],
            'trash': ['"Deleted"', '"Deleted Items"', '"Trash"', 'Deleted', '"已删除邮件"']
        }
        possible_folders = folder_map.get(folder.lower(), ['"INBOX"'])

        # 尝试选择文件夹
        selected_folder = None
        last_error = None
        for imap_folder in possible_folders:
            try:
                status, response = connection.select(imap_folder, readonly=True)
                if status == 'OK':
                    selected_folder = imap_folder
                    break
                else:
                    last_error = f"select {imap_folder} status={status}"
            except Exception as e:
                last_error = f"select {imap_folder} error={str(e)}"
                continue

        if not selected_folder:
            # 如果所有尝试都失败，列出所有可用文件夹以便调试
            try:
                status, folder_list = connection.list()
                available_folders = []
                if status == 'OK' and folder_list:
                    for folder_item in folder_list:
                        if isinstance(folder_item, bytes):
                            available_folders.append(folder_item.decode('utf-8', errors='ignore'))
                        else:
                            available_folders.append(str(folder_item))
                
                error_details = {
                    "last_error": last_error,
                    "tried_folders": possible_folders,
                    "available_folders": available_folders[:10]  # 只返回前10个
                }
            except Exception:
                error_details = {
                    "last_error": last_error,
                    "tried_folders": possible_folders
                }

            return {
                "success": False,
                "error": build_error_payload(
                    "EMAIL_FETCH_FAILED",
                    f"无法访问文件夹，请检查账号配置",
                    "IMAPSelectError",
                    500,
                    error_details
                )
            }

        status, messages = connection.search(None, 'ALL')
        if status != 'OK':
            return {
                "success": False,
                "error": build_error_payload(
                    "EMAIL_FETCH_FAILED",
                    "获取邮件失败，请检查账号配置",
                    "IMAPSearchError",
                    500,
                    f"search status={status}"
                )
            }
        if not messages or not messages[0]:
            return {"success": True, "emails": []}

        message_ids = messages[0].split()
        # 计算分页范围
        total = len(message_ids)
        start_idx = max(0, total - skip - top)
        end_idx = total - skip

        if start_idx >= end_idx:
            return {"success": True, "emails": []}

        paged_ids = message_ids[start_idx:end_idx][::-1]  # 倒序，最新的在前

        emails = []
        for msg_id in paged_ids:
            try:
                status, msg_data = connection.fetch(msg_id, '(RFC822)')
                if status == 'OK' and msg_data and msg_data[0]:
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)

                    emails.append({
                        'id': msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
                        'subject': decode_header_value(msg.get("Subject", "无主题")),
                        'from': decode_header_value(msg.get("From", "未知发件人")),
                        'date': msg.get("Date", "未知时间"),
                        'body_preview': get_email_body(msg)[:200] + "..." if len(get_email_body(msg)) > 200 else get_email_body(msg)
                    })
            except Exception:
                continue

        return {"success": True, "emails": emails}
    except Exception as exc:
        return {
            "success": False,
            "error": build_error_payload(
                "EMAIL_FETCH_FAILED",
                "获取邮件失败，请检查账号配置",
                type(exc).__name__,
                500,
                str(exc)
            )
        }
    finally:
        if connection:
            try:
                connection.logout()
            except Exception:
                pass


def get_email_detail_imap(account: str, client_id: str, refresh_token: str, message_id: str, folder: str = 'inbox') -> Optional[Dict]:
    """使用 IMAP 获取邮件详情"""
    access_token = get_access_token_imap(client_id, refresh_token)
    if not access_token:
        return None

    connection = None
    try:
        connection = imaplib.IMAP4_SSL(IMAP_SERVER_NEW, IMAP_PORT)
        auth_string = f"user={account}\1auth=Bearer {access_token}\1\1".encode('utf-8')
        connection.authenticate('XOAUTH2', lambda x: auth_string)

        # 根据文件夹类型选择 IMAP 文件夹
        folder_map = {
            'inbox': ['"INBOX"', 'INBOX'],
            'junkemail': ['"Junk"', '"Junk Email"', 'Junk', '"垃圾邮件"'],
            'deleteditems': ['"Deleted"', '"Deleted Items"', '"Trash"', 'Deleted', '"已删除邮件"'],
            'trash': ['"Deleted"', '"Deleted Items"', '"Trash"', 'Deleted', '"已删除邮件"']
        }
        possible_folders = folder_map.get(folder.lower(), ['"INBOX"'])

        # 尝试选择文件夹
        selected_folder = None
        for imap_folder in possible_folders:
            try:
                status, response = connection.select(imap_folder, readonly=True)
                if status == 'OK':
                    selected_folder = imap_folder
                    break
            except Exception:
                continue

        if not selected_folder:
            return None

        status, msg_data = connection.fetch(message_id.encode() if isinstance(message_id, str) else message_id, '(RFC822)')
        if status != 'OK' or not msg_data or not msg_data[0]:
            return None

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        return {
            'id': message_id,
            'subject': decode_header_value(msg.get("Subject", "无主题")),
            'from': decode_header_value(msg.get("From", "未知发件人")),
            'to': decode_header_value(msg.get("To", "")),
            'cc': decode_header_value(msg.get("Cc", "")),
            'date': msg.get("Date", "未知时间"),
            'body': get_email_body(msg)
        }
    except Exception:
        return None
    finally:
        if connection:
            try:
                connection.logout()
            except Exception:
                pass


# ==================== 登录验证 ====================

def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': '请先登录', 'need_login': True}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def external_api_required(f):
    """外部 API 验证（X-API-Key == SECRET_KEY）"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = request.headers.get('X-API-Key', '')
        if not api_key or not secret_key or not hmac.compare_digest(api_key, secret_key):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function


# ==================== Flask 路由 ====================

@app.route('/login', methods=['GET', 'POST'])
@csrf_exempt  # 登录接口排除CSRF保护（用户未登录时无法获取token）
def login():
    """登录页面"""
    if request.method == 'POST':
        try:
            # 获取客户端 IP
            client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            if client_ip:
                client_ip = client_ip.split(',')[0].strip()

            # 检查速率限制
            allowed, remaining_time = check_rate_limit(client_ip)
            if not allowed:
                return jsonify({
                    'success': False,
                    'error': f'登录失败次数过多，请在 {remaining_time} 秒后重试'
                }), 429

            data = request.json if request.is_json else request.form
            password = data.get('password', '')

            # 从数据库获取密码哈希
            stored_password = get_login_password()

            # 验证密码
            if verify_password(password, stored_password):
                # 登录成功，重置失败记录
                reset_login_attempts(client_ip)
                session['logged_in'] = True
                session.permanent = True
                return jsonify({'success': True, 'message': '登录成功'})
            else:
                # 登录失败，记录失败次数
                record_login_failure(client_ip)
                return jsonify({'success': False, 'error': '密码错误'})
        except Exception as e:
            print(f"Login error: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'error': f'登录处理失败: {str(e)}'}), 500

    # GET 请求返回登录页面
    return render_template('login.html')


@app.route('/logout')
def logout():
    """退出登录"""
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    """主页"""
    return render_template('index.html')


@app.route('/api/csrf-token', methods=['GET'])
@csrf_exempt  # CSRF token获取接口排除CSRF保护
def get_csrf_token():
    """获取CSRF Token"""
    if CSRF_AVAILABLE:
        token = generate_csrf()
        return jsonify({'csrf_token': token})
    else:
        return jsonify({'csrf_token': None, 'csrf_disabled': True})


# ==================== 分组 API ====================

@app.route('/api/groups', methods=['GET'])
@login_required
def api_get_groups():
    """获取所有分组"""
    groups = load_groups()
    # 添加每个分组的邮箱数量
    for group in groups:
        if group['name'] == '临时邮箱':
            # 临时邮箱分组从 temp_emails 表获取数量
            group['account_count'] = get_temp_email_count()
            group['success_count'] = 0
            group['failed_count'] = 0
        else:
            group['account_count'] = get_group_account_count(group['id'])
            status_counts = get_group_account_status_counts(group['id'])
            group['success_count'] = status_counts['success_count']
            group['failed_count'] = status_counts['failed_count']
    return jsonify({'success': True, 'groups': groups})


@app.route('/api/groups/<int:group_id>', methods=['GET'])
@login_required
def api_get_group(group_id):
    """获取单个分组"""
    group = get_group_by_id(group_id)
    if not group:
        return jsonify({'success': False, 'error': '分组不存在'})
    group['account_count'] = get_group_account_count(group_id)
    status_counts = get_group_account_status_counts(group_id)
    group['success_count'] = status_counts['success_count']
    group['failed_count'] = status_counts['failed_count']
    return jsonify({'success': True, 'group': group})


@app.route('/api/groups', methods=['POST'])
@login_required
def api_add_group():
    """添加分组"""
    data = request.json
    name = sanitize_input(data.get('name', '').strip(), max_length=100)
    description = sanitize_input(data.get('description', ''), max_length=500)
    color = data.get('color', '#1a1a1a')

    if not name:
        return jsonify({'success': False, 'error': '分组名称不能为空'})

    group_id = add_group(name, description, color)
    if group_id:
        return jsonify({'success': True, 'message': '分组创建成功', 'group_id': group_id})
    else:
        return jsonify({'success': False, 'error': '分组名称已存在'})


@app.route('/api/groups/<int:group_id>', methods=['PUT'])
@login_required
def api_update_group(group_id):
    """更新分组"""
    data = request.json
    name = sanitize_input(data.get('name', '').strip(), max_length=100)
    description = sanitize_input(data.get('description', ''), max_length=500)
    color = data.get('color', '#1a1a1a')

    if not name:
        return jsonify({'success': False, 'error': '分组名称不能为空'})

    if update_group(group_id, name, description, color):
        return jsonify({'success': True, 'message': '分组更新成功'})
    else:
        return jsonify({'success': False, 'error': '更新失败'})


@app.route('/api/groups/<int:group_id>', methods=['DELETE'])
@login_required
def api_delete_group(group_id):
    """删除分组"""
    if group_id == 1:
        return jsonify({'success': False, 'error': '默认分组不能删除'})
    
    if delete_group(group_id):
        return jsonify({'success': True, 'message': '分组已删除，邮箱已移至默认分组'})
    else:
        return jsonify({'success': False, 'error': '删除失败'})


@app.route('/api/groups/<int:group_id>/export')
@login_required
def api_export_group(group_id):
    """导出分组下的所有邮箱账号为 TXT 文件（需要二次验证）"""
    # 检查二次验证token
    verify_token = request.args.get('verify_token')
    if not verify_token or not session.get('export_verify_token') or verify_token != session.get('export_verify_token'):
        return jsonify({'success': False, 'error': '需要二次验证', 'need_verify': True})

    # 清除验证token（一次性使用）
    session.pop('export_verify_token', None)

    group = get_group_by_id(group_id)
    if not group:
        return jsonify({'success': False, 'error': '分组不存在'})

    # 使用 load_accounts 获取该分组下的所有账号（自动解密）
    accounts = load_accounts(group_id)

    if not accounts:
        return jsonify({'success': False, 'error': '该分组下没有邮箱账号'})

    # 记录审计日志
    log_audit('export', 'group', str(group_id), f"导出分组 '{group['name']}' 的 {len(accounts)} 个账号")

    # 生成导出内容（格式：email----password----client_id----refresh_token）
    lines = []
    for acc in accounts:
        line = f"{acc['email']}----{acc.get('password', '')}----{acc['client_id']}----{acc['refresh_token']}"
        lines.append(line)

    content = '\n'.join(lines)

    # 生成文件名（使用 URL 编码处理中文）
    filename = f"{group['name']}_accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    encoded_filename = quote(filename)

    # 返回文件下载响应
    return Response(
        content,
        mimetype='text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )


@app.route('/api/groups/<int:group_id>/refresh', methods=['GET'])
@login_required
def api_refresh_group_accounts(group_id):
    """刷新指定分组的账号 token（流式响应，实时返回进度）"""
    import json

    resume = request.args.get('resume', 'true').lower() == 'true'
    group = get_group_by_id(group_id)
    if not group:
        return jsonify({'success': False, 'error': '分组不存在'})
    if group.get('name') == '临时邮箱':
        return jsonify({'success': False, 'error': '临时邮箱分组不支持刷新'})

    def generate():
        conn = sqlite3.connect(DATABASE, timeout=10)
        configure_sqlite(conn)
        conn.row_factory = sqlite3.Row

        try:
            # 清理超过半年的刷新记录
            try:
                conn.execute("DELETE FROM account_refresh_logs WHERE created_at < datetime('now', '-6 months')")
                conn.commit()
            except Exception as e:
                print(f"清理旧记录失败: {str(e)}")

            cursor = conn.execute(
                "SELECT id, email, client_id, refresh_token FROM accounts "
                "WHERE status = 'active' AND group_id = ? ORDER BY id",
                (group_id,)
            )
            accounts = cursor.fetchall()
            config = _resolve_refresh_config(conn, len(accounts), mode='full')

            for event in _refresh_accounts_generator(
                conn=conn,
                accounts=accounts,
                refresh_type='group',
                delay_seconds=config['delay_seconds'],
                resume=resume,
                max_workers=config['max_workers'],
                batch_size=config['batch_size'],
                group_id=group_id,
                group_name=group.get('name'),
                resume_key=f"group_{group_id}",
                scope_label='group'
            ):
                yield f"data: {json.dumps(event)}\n\n"

        finally:
            conn.close()

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/accounts/export')
@login_required
def api_export_all_accounts():
    """导出所有邮箱账号为 TXT 文件（需要二次验证）"""
    # 检查二次验证token
    verify_token = request.args.get('verify_token')
    if not verify_token or not session.get('export_verify_token') or verify_token != session.get('export_verify_token'):
        return jsonify({'success': False, 'error': '需要二次验证', 'need_verify': True})

    # 清除验证token（一次性使用）
    session.pop('export_verify_token', None)

    # 使用 load_accounts 获取所有账号（自动解密）
    accounts = load_accounts()

    if not accounts:
        return jsonify({'success': False, 'error': '没有邮箱账号'})

    # 记录审计日志
    log_audit('export', 'all_accounts', None, f"导出所有账号，共 {len(accounts)} 个")

    # 生成导出内容（格式：email----password----client_id----refresh_token）
    lines = []
    for acc in accounts:
        line = f"{acc['email']}----{acc.get('password', '')}----{acc['client_id']}----{acc['refresh_token']}"
        lines.append(line)

    content = '\n'.join(lines)

    # 生成文件名（使用 URL 编码处理中文）
    filename = f"all_accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    encoded_filename = quote(filename)

    # 返回文件下载响应
    return Response(
        content,
        mimetype='text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )


@app.route('/api/external/checkout', methods=['POST'])
@external_api_required
def api_external_checkout_account():
    """外部领取邮箱（简化版）"""
    data = request.json or {}
    group_id = data.get('group_id')
    owner = data.get('owner')
    ttl_seconds = data.get('ttl_seconds', 900)
    try:
        ttl_seconds = int(ttl_seconds)
    except Exception:
        ttl_seconds = 900
    ttl_seconds = max(60, min(ttl_seconds, 3600))

    if group_id is not None and group_id != '':
        try:
            group_id = int(group_id)
        except Exception:
            return jsonify({'success': False, 'error': 'group_id 无效'}), 400

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        # 清理过期租约
        db.execute("DELETE FROM account_leases WHERE expires_at <= CURRENT_TIMESTAMP")

        params = []
        group_sql = ""
        if group_id:
            group_sql = "AND a.group_id = ?"
            params.append(group_id)

        row = db.execute(f'''
            SELECT a.id, a.email
            FROM accounts a
            LEFT JOIN account_leases l ON a.id = l.account_id
            WHERE a.status = 'active'
            {group_sql}
            AND l.account_id IS NULL
            ORDER BY a.id ASC
            LIMIT 1
        ''', params).fetchone()

        if not row:
            db.commit()
            return jsonify({'success': False, 'error': '没有可用邮箱'}), 404

        lease_id = uuid.uuid4().hex
        expires_at = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).strftime('%Y-%m-%d %H:%M:%S')
        db.execute('''
            INSERT INTO account_leases (lease_id, account_id, owner, expires_at)
            VALUES (?, ?, ?, ?)
        ''', (lease_id, row['id'], owner, expires_at))
        db.commit()

        log_audit('checkout', 'account', str(row['id']), f"lease_id={lease_id}, owner={owner}")
        return jsonify({
            'success': True,
            'lease_id': lease_id,
            'account_id': row['id'],
            'email': row['email'],
            'expires_at': expires_at
        })
    except Exception:
        db.execute("ROLLBACK")
        return jsonify({'success': False, 'error': '领取失败'}), 500


@app.route('/api/external/checkout/complete', methods=['POST'])
@external_api_required
def api_external_checkout_complete():
    """外部完成/释放邮箱"""
    data = request.json or {}
    lease_id = data.get('lease_id', '')
    result = data.get('result', '')
    if not lease_id:
        return jsonify({'success': False, 'error': '参数不完整'}), 400

    db = get_db()
    try:
        row = db.execute('SELECT account_id FROM account_leases WHERE lease_id = ?', (lease_id,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '租约不存在'}), 404
        db.execute('DELETE FROM account_leases WHERE lease_id = ?', (lease_id,))
        db.commit()
        log_audit('checkout_complete', 'account', str(row['account_id']), f"lease_id={lease_id}, result={result}")
        return jsonify({'success': True})
    except Exception:
        return jsonify({'success': False, 'error': '释放失败'}), 500


@app.route('/api/accounts/export-selected', methods=['POST'])
@login_required
def api_export_selected_accounts():
    """导出选中分组的邮箱账号为 TXT 文件（需要二次验证）"""
    data = request.json
    group_ids = data.get('group_ids', [])
    verify_token = data.get('verify_token')

    # 检查二次验证token
    if not verify_token or not session.get('export_verify_token') or verify_token != session.get('export_verify_token'):
        return jsonify({'success': False, 'error': '需要二次验证', 'need_verify': True})

    # 清除验证token（一次性使用）
    session.pop('export_verify_token', None)

    if not group_ids:
        return jsonify({'success': False, 'error': '请选择要导出的分组'})

    # 获取选中分组下的所有账号（使用 load_accounts 自动解密）
    all_accounts = []
    for group_id in group_ids:
        accounts = load_accounts(group_id)
        all_accounts.extend(accounts)

    if not all_accounts:
        return jsonify({'success': False, 'error': '选中的分组下没有邮箱账号'})

    # 记录审计日志
    log_audit('export', 'selected_groups', ','.join(map(str, group_ids)), f"导出选中分组的 {len(all_accounts)} 个账号")

    # 生成导出内容
    lines = []
    for acc in all_accounts:
        line = f"{acc['email']}----{acc.get('password', '')}----{acc['client_id']}----{acc['refresh_token']}"
        lines.append(line)

    content = '\n'.join(lines)

    # 生成文件名
    filename = f"selected_accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    encoded_filename = quote(filename)

    # 返回文件下载响应
    return Response(
        content,
        mimetype='text/plain; charset=utf-8',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )


@app.route('/api/export/verify', methods=['POST'])
@login_required
def api_generate_export_verify_token():
    """生成导出验证token（二次验证）"""
    data = request.json
    password = data.get('password', '')

    # 验证密码
    db = get_db()
    cursor = db.execute("SELECT value FROM settings WHERE key = 'login_password'")
    result = cursor.fetchone()

    if not result:
        return jsonify({'success': False, 'error': '系统配置错误'})

    stored_password = result[0]
    if not verify_password(password, stored_password):
        return jsonify({'success': False, 'error': '密码错误'})

    # 生成一次性验证token
    verify_token = secrets.token_urlsafe(32)
    session['export_verify_token'] = verify_token

    return jsonify({'success': True, 'verify_token': verify_token})


# ==================== 邮箱账号 API ====================

@app.route('/api/accounts', methods=['GET'])
@login_required
def api_get_accounts():
    """获取所有账号"""
    group_id = request.args.get('group_id', type=int)
    limit = request.args.get('limit', default=100, type=int)
    offset = request.args.get('offset', default=0, type=int)
    sort_by = request.args.get('sort_by', default='refresh_time')
    sort_order = request.args.get('sort_order', default='asc')
    tag_ids_param = request.args.get('tag_ids', '').strip()
    refresh_status = request.args.get('refresh_status', '').strip().lower()

    if limit is None:
        limit = 100
    if limit < 0:
        limit = 100
    max_limit = 500
    if limit > max_limit:
        limit = max_limit

    if offset is None or offset < 0:
        offset = 0

    sort_fields = {
        'refresh_time': "COALESCE(a.last_refresh_at, '1970-01-01 00:00:00')",
        'email': "LOWER(a.email)",
        'created_at': "a.created_at"
    }
    sort_field = sort_fields.get(sort_by, sort_fields['refresh_time'])
    sort_dir = 'DESC' if str(sort_order).lower() == 'desc' else 'ASC'

    tag_ids = []
    if tag_ids_param:
        for part in tag_ids_param.split(','):
            part = part.strip()
            if part.isdigit():
                tag_ids.append(int(part))

    db = get_db()

    # 组装 WHERE 条件
    where_clauses = []
    params = []
    if group_id:
        where_clauses.append("a.group_id = ?")
        params.append(group_id)
    if tag_ids:
        placeholders = ",".join(["?"] * len(tag_ids))
        where_clauses.append(
            f"a.id IN (SELECT account_id FROM account_tags WHERE tag_id IN ({placeholders}))"
        )
        params.extend(tag_ids)
    if refresh_status in ('success', 'failed'):
        where_clauses.append("l.status = ?")
        params.append(refresh_status)
    elif refresh_status == 'unknown':
        where_clauses.append("l.status IS NULL")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    # 统计总数
    count_sql = f'''
        SELECT COUNT(DISTINCT a.id) as total
        FROM accounts a
        LEFT JOIN (
            SELECT l1.account_id, l1.status, l1.error_message, l1.created_at
            FROM account_refresh_logs l1
            INNER JOIN (
                SELECT account_id, MAX(created_at) as max_created
                FROM account_refresh_logs
                GROUP BY account_id
            ) latest ON l1.account_id = latest.account_id AND l1.created_at = latest.max_created
        ) l ON a.id = l.account_id
        {where_sql}
    '''
    total = db.execute(count_sql, params).fetchone()['total']

    # 获取当前页数据（批量获取最后刷新状态 + 分组信息）
    accounts_sql = f'''
        SELECT DISTINCT a.*, g.name as group_name, g.color as group_color,
               l.status as last_refresh_status, l.error_message as last_refresh_error
        FROM accounts a
        LEFT JOIN groups g ON a.group_id = g.id
        LEFT JOIN (
            SELECT l1.account_id, l1.status, l1.error_message, l1.created_at
            FROM account_refresh_logs l1
            INNER JOIN (
                SELECT account_id, MAX(created_at) as max_created
                FROM account_refresh_logs
                GROUP BY account_id
            ) latest ON l1.account_id = latest.account_id AND l1.created_at = latest.max_created
        ) l ON a.id = l.account_id
        {where_sql}
        ORDER BY {sort_field} {sort_dir}, a.id ASC
        LIMIT ? OFFSET ?
    '''
    accounts = db.execute(accounts_sql, params + [limit, offset]).fetchall()

    account_ids = [row['id'] for row in accounts]
    tags_map = {}
    if account_ids:
        placeholders = ",".join(["?"] * len(account_ids))
        tag_rows = db.execute(
            f'''
                SELECT at.account_id, t.id, t.name, t.color
                FROM account_tags at
                JOIN tags t ON at.tag_id = t.id
                WHERE at.account_id IN ({placeholders})
                ORDER BY t.created_at DESC
            ''',
            account_ids
        ).fetchall()
        for row in tag_rows:
            tags_map.setdefault(row['account_id'], []).append({
                'id': row['id'],
                'name': row['name'],
                'color': row['color']
            })

    safe_accounts = []
    for acc in accounts:
        safe_accounts.append({
            'id': acc['id'],
            'email': acc['email'],
            'client_id': acc['client_id'][:8] + '...' if len(acc['client_id']) > 8 else acc['client_id'],
            'group_id': acc['group_id'],
            'group_name': acc['group_name'] if acc['group_name'] else '默认分组',
            'group_color': acc['group_color'] if acc['group_color'] else '#666666',
            'remark': acc['remark'] if acc['remark'] else '',
            'status': acc['status'] if acc['status'] else 'active',
            'last_refresh_at': acc['last_refresh_at'] if acc['last_refresh_at'] else '',
            'last_refresh_status': acc['last_refresh_status'],
            'last_refresh_error': acc['last_refresh_error'],
            'created_at': acc['created_at'] if acc['created_at'] else '',
            'updated_at': acc['updated_at'] if acc['updated_at'] else '',
            'tags': tags_map.get(acc['id'], [])
        })

    return jsonify({
        'success': True,
        'accounts': safe_accounts,
        'total': total,
        'limit': limit,
        'offset': offset
    })


# ==================== 标签 API ====================

@app.route('/api/tags', methods=['GET'])
@login_required
def api_get_tags():
    """获取所有标签"""
    return jsonify({'success': True, 'tags': get_tags()})


@app.route('/api/tags', methods=['POST'])
@login_required
def api_add_tag():
    """添加标签"""
    data = request.json
    name = sanitize_input(data.get('name', '').strip(), max_length=50)
    color = data.get('color', '#1a1a1a')

    if not name:
        return jsonify({'success': False, 'error': '标签名称不能为空'})

    tag_id = add_tag(name, color)
    if tag_id:
        return jsonify({'success': True, 'tag': {'id': tag_id, 'name': name, 'color': color}})
    else:
        return jsonify({'success': False, 'error': '标签名称已存在'})


@app.route('/api/tags/<int:tag_id>', methods=['DELETE'])
@login_required
def api_delete_tag(tag_id):
    """删除标签"""
    if delete_tag(tag_id):
        return jsonify({'success': True, 'message': '标签已删除'})
    else:
        return jsonify({'success': False, 'error': '删除失败'})


@app.route('/api/accounts/tags', methods=['POST'])
@login_required
def api_batch_manage_tags():
    """批量管理账号标签"""
    data = request.json
    account_ids = data.get('account_ids', [])
    tag_id = data.get('tag_id')
    action = data.get('action')  # add, remove

    if not account_ids or not tag_id or not action:
        return jsonify({'success': False, 'error': '参数不完整'})

    count = 0
    for acc_id in account_ids:
        if action == 'add':
            if add_account_tag(acc_id, tag_id):
                count += 1
        elif action == 'remove':
            if remove_account_tag(acc_id, tag_id):
                count += 1

    return jsonify({'success': True, 'message': f'成功处理 {count} 个账号'})



@app.route('/api/accounts/search', methods=['GET'])
@login_required
def api_search_accounts():
    """全局搜索账号"""
    query = request.args.get('q', '').strip()
    limit = request.args.get('limit', default=100, type=int)
    offset = request.args.get('offset', default=0, type=int)
    sort_by = request.args.get('sort_by', default='created_at')
    sort_order = request.args.get('sort_order', default='desc')
    tag_ids_param = request.args.get('tag_ids', '').strip()
    group_id = request.args.get('group_id', type=int)
    refresh_status = request.args.get('refresh_status', '').strip().lower()

    if not query:
        return jsonify({'success': True, 'accounts': [], 'total': 0, 'limit': limit, 'offset': offset})

    if limit is None:
        limit = 100
    if limit < 0:
        limit = 100
    max_limit = 500
    if limit > max_limit:
        limit = max_limit
    if offset is None or offset < 0:
        offset = 0

    sort_fields = {
        'refresh_time': "COALESCE(a.last_refresh_at, '1970-01-01 00:00:00')",
        'email': "LOWER(a.email)",
        'created_at': "a.created_at"
    }
    sort_field = sort_fields.get(sort_by, sort_fields['created_at'])
    sort_dir = 'DESC' if str(sort_order).lower() == 'desc' else 'ASC'

    db = get_db()
    like_query = f'%{query}%'
    tag_ids = []
    if tag_ids_param:
        for part in tag_ids_param.split(','):
            part = part.strip()
            if part.isdigit():
                tag_ids.append(int(part))
    group_filter_sql = ""
    group_filter_params = []
    if group_id:
        group_filter_sql = " AND a.group_id = ?"
        group_filter_params = [group_id]

    tag_filter_sql = ""
    tag_filter_params = []
    if tag_ids:
        placeholders = ",".join(["?"] * len(tag_ids))
        tag_filter_sql = f" AND a.id IN (SELECT account_id FROM account_tags WHERE tag_id IN ({placeholders}))"
        tag_filter_params = tag_ids

    status_filter_sql = ""
    status_filter_params = []
    if refresh_status in ('success', 'failed'):
        status_filter_sql = " AND l.status = ?"
        status_filter_params = [refresh_status]
    elif refresh_status == 'unknown':
        status_filter_sql = " AND l.status IS NULL"

    count_row = db.execute(f'''
        WITH matched AS (
            SELECT DISTINCT a.id
            FROM accounts a
            LEFT JOIN account_tags at ON a.id = at.account_id
            LEFT JOIN tags t ON at.tag_id = t.id
            LEFT JOIN (
                SELECT l1.account_id, l1.status, l1.error_message, l1.created_at
                FROM account_refresh_logs l1
                INNER JOIN (
                    SELECT account_id, MAX(created_at) as max_created
                    FROM account_refresh_logs
                    GROUP BY account_id
                ) latest ON l1.account_id = latest.account_id AND l1.created_at = latest.max_created
            ) l ON a.id = l.account_id
            WHERE (a.email LIKE ? OR a.remark LIKE ? OR t.name LIKE ?)
            {tag_filter_sql}
            {group_filter_sql}
            {status_filter_sql}
        )
        SELECT COUNT(*) as total FROM matched
    ''', (like_query, like_query, like_query, *tag_filter_params, *group_filter_params, *status_filter_params)).fetchone()
    total = count_row['total'] if count_row else 0

    rows = db.execute(f'''
        WITH matched AS (
            SELECT DISTINCT a.id
            FROM accounts a
            LEFT JOIN account_tags at ON a.id = at.account_id
            LEFT JOIN tags t ON at.tag_id = t.id
            LEFT JOIN (
                SELECT l1.account_id, l1.status, l1.error_message, l1.created_at
                FROM account_refresh_logs l1
                INNER JOIN (
                    SELECT account_id, MAX(created_at) as max_created
                    FROM account_refresh_logs
                    GROUP BY account_id
                ) latest ON l1.account_id = latest.account_id AND l1.created_at = latest.max_created
            ) l ON a.id = l.account_id
            WHERE (a.email LIKE ? OR a.remark LIKE ? OR t.name LIKE ?)
            {tag_filter_sql}
            {group_filter_sql}
            {status_filter_sql}
        )
        SELECT a.*, g.name as group_name, g.color as group_color,
               l.status as last_refresh_status, l.error_message as last_refresh_error
        FROM accounts a
        JOIN matched m ON a.id = m.id
        LEFT JOIN groups g ON a.group_id = g.id
        LEFT JOIN (
            SELECT l1.account_id, l1.status, l1.error_message, l1.created_at
            FROM account_refresh_logs l1
            INNER JOIN (
                SELECT account_id, MAX(created_at) as max_created
                FROM account_refresh_logs
                GROUP BY account_id
            ) latest ON l1.account_id = latest.account_id AND l1.created_at = latest.max_created
        ) l ON a.id = l.account_id
        ORDER BY {sort_field} {sort_dir}, a.id ASC
        LIMIT ? OFFSET ?
    ''', (like_query, like_query, like_query, *tag_filter_params, *group_filter_params, *status_filter_params, limit, offset)).fetchall()

    account_ids = [row['id'] for row in rows]
    tags_map = {}
    if account_ids:
        placeholders = ",".join(["?"] * len(account_ids))
        tag_rows = db.execute(
            f'''
                SELECT at.account_id, t.id, t.name, t.color
                FROM account_tags at
                JOIN tags t ON at.tag_id = t.id
                WHERE at.account_id IN ({placeholders})
                ORDER BY t.created_at DESC
            ''',
            account_ids
        ).fetchall()
        for row in tag_rows:
            tags_map.setdefault(row['account_id'], []).append({
                'id': row['id'],
                'name': row['name'],
                'color': row['color']
            })

    safe_accounts = []
    for acc in rows:
        safe_accounts.append({
            'id': acc['id'],
            'email': acc['email'],
            'client_id': acc['client_id'][:8] + '...' if len(acc['client_id']) > 8 else acc['client_id'],
            'group_id': acc['group_id'],
            'group_name': acc['group_name'] if acc['group_name'] else '默认分组',
            'group_color': acc['group_color'] if acc['group_color'] else '#666666',
            'remark': acc['remark'] if acc['remark'] else '',
            'status': acc['status'] if acc['status'] else 'active',
            'created_at': acc['created_at'] if acc['created_at'] else '',
            'updated_at': acc['updated_at'] if acc['updated_at'] else '',
            'tags': tags_map.get(acc['id'], []),
            'last_refresh_at': acc['last_refresh_at'] if acc['last_refresh_at'] else '',
            'last_refresh_status': acc['last_refresh_status'],
            'last_refresh_error': acc['last_refresh_error']
        })

    return jsonify({
        'success': True,
        'accounts': safe_accounts,
        'total': total,
        'limit': limit,
        'offset': offset
    })


@app.route('/api/accounts/refresh-resume/clear', methods=['POST'])
@login_required
def api_clear_refresh_resume_state():
    """清空刷新断点状态"""
    db = get_db()
    group_id = None
    try:
        if request.is_json:
            group_id = request.json.get('group_id')
        else:
            group_id = request.form.get('group_id')
    except Exception:
        group_id = None
    try:
        keys = ["refresh_resume_state_manual", "refresh_resume_state_scheduled"]
        if group_id:
            keys.append(f"refresh_resume_state_group_{int(group_id)}")
        placeholders = ",".join(["?"] * len(keys))
        db.execute(
            f"DELETE FROM settings WHERE key IN ({placeholders})",
            tuple(keys)
        )
        db.commit()
        return jsonify({'success': True, 'message': '已清空刷新断点状态'})
    except Exception:
        return jsonify({'success': False, 'error': '清空失败'})


@app.route('/api/accounts/refresh-resume/status', methods=['GET'])
@login_required
def api_get_refresh_resume_status():
    """获取刷新断点状态详情"""
    db = get_db()
    group_id = request.args.get('group_id', type=int)
    manual_state = _load_resume_state_any(db, 'manual')
    scheduled_state = _load_resume_state_any(db, 'scheduled')
    group_state = None
    group_name = None
    if group_id:
        group = get_group_by_id(group_id)
        if group:
            group_name = group.get('name')
            group_state = _load_resume_state_any(db, f"group_{group_id}")
    history_rates = _get_recent_refresh_rates(db, limit=5, refresh_types=['manual', 'scheduled'])
    return jsonify({
        'success': True,
        'manual': manual_state,
        'scheduled': scheduled_state,
        'group': group_state,
        'group_name': group_name,
        'history_rates': history_rates
    })


@app.route('/api/accounts/<int:account_id>', methods=['GET'])
@login_required
def api_get_account(account_id):
    """获取单个账号详情"""
    account = get_account_by_id(account_id)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})
    
    return jsonify({
        'success': True,
        'account': {
            'id': account['id'],
            'email': account['email'],
            'password': account['password'],
            'client_id': account['client_id'],
            'refresh_token': account['refresh_token'],
            'group_id': account.get('group_id'),
            'group_name': account.get('group_name', '默认分组'),
            'remark': account.get('remark', ''),
            'status': account.get('status', 'active'),
            'created_at': account.get('created_at', ''),
            'updated_at': account.get('updated_at', '')
        }
    })


@app.route('/api/accounts', methods=['POST'])
@login_required
def api_add_account():
    """添加账号"""
    data = request.json
    account_str = data.get('account_string', '')
    group_id = data.get('group_id', 1)
    
    if not account_str:
        return jsonify({'success': False, 'error': '请输入账号信息'})
    
    # 支持批量导入（多行）
    lines = account_str.strip().split('\n')
    added = 0
    skipped = 0
    invalid = 0
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        parsed = parse_account_string(line)
        if not parsed:
            invalid += 1
            continue

        if add_account(parsed['email'], parsed['password'],
                       parsed['client_id'], parsed['refresh_token'], group_id):
            added += 1
        else:
            skipped += 1
    
    if added > 0:
        details = []
        if skipped > 0:
            details.append(f'已存在 {skipped} 个')
        if invalid > 0:
            details.append(f'格式错误 {invalid} 个')
        suffix = f'（{", ".join(details)}）' if details else ''
        return jsonify({
            'success': True,
            'message': f'成功添加 {added} 个账号{suffix}',
            'added': added,
            'skipped': skipped,
            'invalid': invalid
        })

    if skipped > 0:
        details = f'，格式错误 {invalid} 个' if invalid > 0 else ''
        return jsonify({
            'success': True,
            'message': f'已存在 {skipped} 个账号，已跳过{details}',
            'added': added,
            'skipped': skipped,
            'invalid': invalid
        })

    return jsonify({
        'success': False,
        'error': '没有有效账号被添加（可能格式错误）',
        'added': added,
        'skipped': skipped,
        'invalid': invalid
    })


@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
@login_required
def api_update_account(account_id):
    """更新账号"""
    data = request.json

    # 检查是否只更新状态
    if 'status' in data and len(data) == 1:
        # 只更新状态
        return api_update_account_status(account_id, data['status'])

    email_addr = data.get('email', '')
    password = data.get('password', '')
    client_id = data.get('client_id', '')
    refresh_token = data.get('refresh_token', '')
    group_id = data.get('group_id', 1)
    remark = sanitize_input(data.get('remark', ''), max_length=200)
    status = data.get('status', 'active')

    if not email_addr or not client_id or not refresh_token:
        return jsonify({'success': False, 'error': '邮箱、Client ID 和 Refresh Token 不能为空'})

    if update_account(account_id, email_addr, password, client_id, refresh_token, group_id, remark, status):
        return jsonify({'success': True, 'message': '账号更新成功'})
    else:
        return jsonify({'success': False, 'error': '更新失败'})


def api_update_account_status(account_id: int, status: str):
    """只更新账号状态"""
    db = get_db()
    try:
        db.execute('''
            UPDATE accounts
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (status, account_id))
        db.commit()
        return jsonify({'success': True, 'message': '状态更新成功'})
    except Exception:
        return jsonify({'success': False, 'error': '更新失败'})


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
@login_required
def api_delete_account(account_id):
    """删除账号"""
    if delete_account_by_id(account_id):
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': '删除失败'})


@app.route('/api/accounts/email/<email_addr>', methods=['DELETE'])
@login_required
def api_delete_account_by_email(email_addr):
    """根据邮箱地址删除账号"""
    if delete_account_by_email(email_addr):
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': '删除失败'})


@app.route('/api/accounts/batch-delete', methods=['POST'])
@login_required
def api_batch_delete_accounts():
    """批量删除账号"""
    data = request.json or {}
    account_ids = data.get('account_ids', [])
    if not account_ids or not isinstance(account_ids, list):
        return jsonify({'success': False, 'error': '参数不完整'})

    # 去重 + 过滤非法值
    clean_ids = []
    for acc_id in account_ids:
        try:
            acc_id = int(acc_id)
        except Exception:
            continue
        if acc_id > 0:
            clean_ids.append(acc_id)
    clean_ids = list(set(clean_ids))

    if not clean_ids:
        return jsonify({'success': False, 'error': '参数不完整'})

    db = get_db()
    try:
        placeholders = ",".join(["?"] * len(clean_ids))
        db.execute(f'DELETE FROM account_leases WHERE account_id IN ({placeholders})', clean_ids)
        db.execute(f'DELETE FROM account_tags WHERE account_id IN ({placeholders})', clean_ids)
        db.execute(f'DELETE FROM account_refresh_logs WHERE account_id IN ({placeholders})', clean_ids)
        db.execute(f'DELETE FROM accounts WHERE id IN ({placeholders})', clean_ids)
        db.commit()
        return jsonify({'success': True, 'deleted': len(clean_ids)})
    except Exception:
        return jsonify({'success': False, 'error': '删除失败'})


# ==================== 账号刷新 API ====================

def log_refresh_result(account_id: int, account_email: str, refresh_type: str, status: str, error_message: str = None):
    """记录刷新结果到数据库"""
    db = get_db()
    try:
        db.execute('''
            INSERT INTO account_refresh_logs (account_id, account_email, refresh_type, status, error_message)
            VALUES (?, ?, ?, ?, ?)
        ''', (account_id, account_email, refresh_type, status, error_message))

        # 更新账号的最后刷新时间
        if status == 'success':
            db.execute('''
                UPDATE accounts
                SET last_refresh_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (account_id,))

        db.commit()
        return True
    except Exception as e:
        print(f"记录刷新结果失败: {str(e)}")
        return False


def _compute_backoff_delay(attempt: int, retry_after: Optional[str] = None) -> float:
    if retry_after:
        try:
            return max(0.5, min(REFRESH_BACKOFF_MAX, float(retry_after)))
        except Exception:
            pass
    base = min(REFRESH_BACKOFF_MAX, REFRESH_BACKOFF_BASE * (2 ** attempt))
    return base + random.uniform(0, 0.3)


def post_with_backoff(url: str, data: Dict[str, Any], timeout: int = 30) -> requests.Response:
    """带退避重试的 POST 请求（用于刷新任务）"""
    retry_status = {429, 500, 502, 503, 504}
    last_exc = None
    for attempt in range(REFRESH_BACKOFF_RETRIES + 1):
        try:
            response = requests.post(url, data=data, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            if attempt >= REFRESH_BACKOFF_RETRIES:
                raise
            time.sleep(_compute_backoff_delay(attempt))
            continue

        if response.status_code in retry_status and attempt < REFRESH_BACKOFF_RETRIES:
            retry_after = response.headers.get('Retry-After')
            time.sleep(_compute_backoff_delay(attempt, retry_after))
            continue

        return response

    if last_exc:
        raise last_exc
    return response


def _get_setting_conn(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    cursor = conn.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cursor.fetchone()
    return row[0] if row else default


def _set_setting_conn(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute('''
        INSERT OR REPLACE INTO settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    ''', (key, value))


def _get_setting_int_conn(conn: sqlite3.Connection, key: str, default: int) -> int:
    raw = _get_setting_conn(conn, key, '')
    try:
        return int(raw)
    except Exception:
        return default


def _resolve_refresh_config(conn: sqlite3.Connection, total: int, mode: str = 'default') -> Dict[str, int]:
    delay_seconds = _get_setting_int_conn(conn, 'refresh_delay_seconds', 5)
    max_workers = _get_setting_int_conn(conn, 'refresh_max_workers', REFRESH_MAX_WORKERS)
    batch_size = _get_setting_int_conn(conn, 'refresh_batch_size', REFRESH_BATCH_SIZE)

    delay_seconds = max(0, min(delay_seconds, 60))
    max_workers = max(1, min(max_workers, 20))
    batch_size = max(1, min(batch_size, 100))

    if mode == 'full':
        # 全量刷新：保守策略，降低并发，保证稳定完成
        max_workers = min(max_workers, 6)
        batch_size = min(batch_size, 30)
        delay_seconds = max(delay_seconds, 2)
    else:
        # 自动调优：根据规模调节并发/批次/间隔（偏性能但保留稳定性余量）
        if total >= 2000:
            max_workers = max(max_workers, 14)
            batch_size = max(batch_size, 100)
            delay_seconds = min(delay_seconds, 1)
        elif total >= 1000:
            max_workers = max(max_workers, 12)
            batch_size = max(batch_size, 80)
            delay_seconds = min(delay_seconds, 2)
        elif total >= 500:
            max_workers = max(max_workers, 10)
            batch_size = max(batch_size, 60)
            delay_seconds = min(delay_seconds, 3)

    if total > 0:
        if total < max_workers:
            max_workers = max(1, total)
        if total < batch_size:
            batch_size = max(1, total)
        if batch_size < max_workers:
            batch_size = max_workers

    return {
        'delay_seconds': delay_seconds,
        'max_workers': max_workers,
        'batch_size': batch_size
    }


def _resume_state_key(resume_key: str) -> str:
    return f"refresh_resume_state_{resume_key}"


def _get_resume_state(conn: sqlite3.Connection, resume_key: str) -> Optional[Dict[str, Any]]:
    key = _resume_state_key(resume_key)
    raw = _get_setting_conn(conn, key, '')
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except Exception:
        return None
    if state.get('status') != 'running':
        return None
    updated_at = state.get('updated_at')
    if updated_at:
        try:
            updated_time = datetime.fromisoformat(updated_at)
            if (datetime.now() - updated_time).total_seconds() > REFRESH_RESUME_TTL_SECONDS:
                return None
        except Exception:
            return None
    return state


def _load_resume_state_any(conn: sqlite3.Connection, resume_key: str) -> Optional[Dict[str, Any]]:
    key = _resume_state_key(resume_key)
    raw = _get_setting_conn(conn, key, '')
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except Exception:
        return None
    total = state.get('total')
    processed = state.get('processed')
    remaining = None
    if isinstance(total, int) and isinstance(processed, int):
        remaining = max(0, total - processed)
    updated_at = state.get('updated_at')
    stale = False
    if updated_at:
        try:
            updated_time = datetime.fromisoformat(updated_at)
            stale = (datetime.now() - updated_time).total_seconds() > REFRESH_RESUME_TTL_SECONDS
        except Exception:
            stale = False
    return {
        'status': state.get('status'),
        'started_at': state.get('started_at'),
        'updated_at': updated_at,
        'finished_at': state.get('finished_at'),
        'last_id': state.get('last_id'),
        'total': total,
        'processed': processed,
        'remaining': remaining,
        'stale': stale,
        'duration_seconds': state.get('duration_seconds'),
        'avg_rate': state.get('avg_rate'),
        'group_id': state.get('group_id')
    }


def _get_recent_refresh_rates(
    conn: sqlite3.Connection,
    limit: int = 3,
    refresh_types: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """获取最近 N 次刷新均速统计（基于刷新运行记录）"""
    where_sql = ''
    params: List[Any] = []
    if refresh_types:
        placeholders = ",".join(["?"] * len(refresh_types))
        where_sql = f"WHERE refresh_type IN ({placeholders})"
        params.extend(refresh_types)
    params.append(limit)
    runs = conn.execute(f'''
        SELECT run_id, refresh_type, started_at, finished_at, total, success_count, failed_count
        FROM refresh_runs
        {where_sql}
        ORDER BY started_at DESC
        LIMIT ?
    ''', params).fetchall()
    results = []
    for row in runs:
        run_time = row['started_at']
        total = row['total'] or 0
        if not run_time or total == 0:
            continue

        duration_seconds = None
        if row['started_at'] and row['finished_at']:
            try:
                start = datetime.fromisoformat(row['started_at'])
                end = datetime.fromisoformat(row['finished_at'])
                duration_seconds = max(1, int((end - start).total_seconds()))
            except Exception:
                duration_seconds = None

        avg_rate = None
        if duration_seconds:
            avg_rate = total / duration_seconds

        results.append({
            'run_id': row['run_id'],
            'run_time': run_time,
            'refresh_type': row['refresh_type'],
            'total': total,
            'success_count': row['success_count'] or 0,
            'failed_count': row['failed_count'] or 0,
            'duration_seconds': duration_seconds,
            'avg_rate': avg_rate
        })

    return results


def _save_resume_state(conn: sqlite3.Connection, resume_key: str, state: Dict[str, Any]) -> None:
    key = _resume_state_key(resume_key)
    state['updated_at'] = datetime.now().isoformat()
    _set_setting_conn(conn, key, json.dumps(state, ensure_ascii=True))


def _complete_resume_state(conn: sqlite3.Connection, resume_key: str, state: Dict[str, Any]) -> None:
    state['status'] = 'completed'
    state['finished_at'] = datetime.now().isoformat()
    try:
        started_at = state.get('started_at')
        processed = state.get('processed')
        if started_at:
            started_time = datetime.fromisoformat(started_at)
            finished_time = datetime.fromisoformat(state['finished_at'])
            duration_seconds = max(1, int((finished_time - started_time).total_seconds()))
            state['duration_seconds'] = duration_seconds
            if isinstance(processed, int) and processed > 0:
                state['avg_rate'] = processed / duration_seconds
    except Exception:
        pass
    _save_resume_state(conn, resume_key, state)


def _is_throttle_error(error_msg: Optional[str]) -> bool:
    if not error_msg:
        return False
    text = str(error_msg).lower()
    keywords = [
        'too many requests',
        'temporarily_unavailable',
        'throttle',
        'rate limit',
        '429',
        'retry-after'
    ]
    return any(keyword in text for keyword in keywords)


def _refresh_account_worker(account: sqlite3.Row) -> Dict[str, Any]:
    account_id = account['id']
    account_email = account['email']
    client_id = account['client_id']
    encrypted_refresh_token = account['refresh_token']

    try:
        refresh_token = decrypt_data(encrypted_refresh_token) if encrypted_refresh_token else encrypted_refresh_token
    except Exception as exc:
        return {
            'id': account_id,
            'email': account_email,
            'success': False,
            'error': f"解密 token 失败: {str(exc)}"
        }

    success, error_msg = test_refresh_token(client_id, refresh_token)
    return {
        'id': account_id,
        'email': account_email,
        'success': success,
        'error': error_msg
    }


def _refresh_accounts_generator(
    conn: sqlite3.Connection,
    accounts: List[sqlite3.Row],
    refresh_type: str,
    delay_seconds: int,
    resume: bool,
    max_workers: Optional[int] = None,
    batch_size: Optional[int] = None,
    group_id: Optional[int] = None,
    group_name: Optional[str] = None,
    resume_key: Optional[str] = None,
    scope_label: Optional[str] = None
) -> Any:
    """刷新账号，支持并发、退避和断点续跑（生成事件）"""
    resume_key = resume_key or refresh_type
    scope_label = scope_label or refresh_type
    max_workers = max_workers if max_workers is not None else max(1, min(REFRESH_MAX_WORKERS, 20))
    batch_size = batch_size if batch_size is not None else max(1, min(REFRESH_BATCH_SIZE, 100))

    total_all = len(accounts)
    resumed = False
    resume_state = _get_resume_state(conn, resume_key) if resume else None
    start_from_id = None

    if resume_state and resume_state.get('last_id'):
        start_from_id = int(resume_state['last_id'])
        resumed = True

    if start_from_id:
        accounts = [acc for acc in accounts if acc['id'] > start_from_id]

    total = len(accounts)
    now_iso = datetime.now().isoformat()
    state = {
        'status': 'running',
        'started_at': resume_state.get('started_at') if resume_state else now_iso,
        'last_id': start_from_id or 0,
        'total': total_all,
        'processed': total_all - total,
        'group_id': group_id
    }
    _save_resume_state(conn, resume_key, state)

    skipped_count = total_all - total
    run_id = uuid.uuid4().hex
    conn.execute('''
        INSERT INTO refresh_runs (
            run_id, refresh_type, started_at, total, total_all,
            resumed, skipped, group_id, max_workers, batch_size, delay_seconds, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        run_id,
        refresh_type,
        now_iso,
        total,
        total_all,
        1 if resumed else 0,
        skipped_count,
        group_id,
        max_workers,
        batch_size,
        delay_seconds,
        'running'
    ))
    conn.commit()

    success_count = 0
    failed_count = 0
    failed_list = []
    processed = 0
    run_started_ts = time.time()
    current_delay = delay_seconds

    yield {
        'type': 'start',
        'total': total,
        'total_all': total_all,
        'delay_seconds': delay_seconds,
        'max_workers': max_workers,
        'batch_size': batch_size,
        'refresh_type': refresh_type,
        'resumed': resumed,
        'skipped': skipped_count,
        'run_id': run_id,
        'scope': scope_label,
        'group_id': group_id,
        'group_name': group_name
    }

    if total == 0:
        conn.execute('''
            UPDATE refresh_runs
            SET finished_at = CURRENT_TIMESTAMP,
                success_count = 0,
                failed_count = 0,
                status = 'completed'
            WHERE run_id = ?
        ''', (run_id,))
        conn.commit()
        _complete_resume_state(conn, resume_key, state)
        yield {
            'type': 'complete',
            'total': total,
            'success_count': 0,
            'failed_count': 0,
            'failed_list': [],
            'run_id': run_id,
            'duration_seconds': 0,
            'avg_rate': None
        }
        return

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        for batch_start in range(0, total, batch_size):
            batch = accounts[batch_start:batch_start + batch_size]
            future_map = {executor.submit(_refresh_account_worker, acc): acc for acc in batch}
            throttle_hits = 0
            for future in as_completed(future_map):
                acc = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        'id': acc['id'],
                        'email': acc['email'],
                        'success': False,
                        'error': f"刷新异常: {str(exc)}"
                    }

                # 写入刷新日志
                conn.execute('''
                    INSERT INTO account_refresh_logs (account_id, account_email, refresh_type, status, error_message)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    result['id'],
                    result['email'],
                    refresh_type,
                    'success' if result['success'] else 'failed',
                    result['error']
                ))

                if result['success']:
                    conn.execute('''
                        UPDATE accounts
                        SET last_refresh_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (result['id'],))
                    success_count += 1
                else:
                    failed_count += 1
                    failed_list.append({
                        'id': result['id'],
                        'email': result['email'],
                        'error': result['error']
                    })
                    if _is_throttle_error(result.get('error')):
                        throttle_hits += 1

                processed += 1
                elapsed = max(1.0, time.time() - run_started_ts)
                rate_per_min = (processed / elapsed) * 60
                eta_seconds = None
                if processed > 0 and processed < total:
                    eta_seconds = int(((total - processed) / (processed / elapsed)) or 0)

                yield {
                    'type': 'progress',
                    'email': result['email'],
                    'current': processed,
                    'total': total,
                    'success_count': success_count,
                    'failed_count': failed_count,
                    'rate_per_min': rate_per_min,
                    'eta_seconds': eta_seconds,
                    'elapsed_seconds': int(elapsed),
                    'run_id': run_id
                }

            # 批次提交 + 断点记录
            last_id = batch[-1]['id']
            state['last_id'] = last_id
            state['processed'] = total_all - (total - (batch_start + len(batch)))
            _save_resume_state(conn, resume_key, state)
            conn.commit()

            if throttle_hits > 0:
                current_delay = min(
                    REFRESH_BACKOFF_MAX,
                    max(current_delay, 1) + min(3, throttle_hits)
                )
            elif current_delay > delay_seconds:
                current_delay = max(delay_seconds, current_delay - 1)

            if current_delay > 0 and (batch_start + batch_size) < total:
                yield {'type': 'delay', 'seconds': current_delay, 'run_id': run_id}
                time.sleep(current_delay)
    finally:
        executor.shutdown(wait=True)

    _complete_resume_state(conn, resume_key, state)
    duration_seconds = max(1, int(time.time() - run_started_ts))
    avg_rate = (success_count + failed_count) / duration_seconds if duration_seconds > 0 else None
    conn.execute('''
        UPDATE refresh_runs
        SET finished_at = CURRENT_TIMESTAMP,
            success_count = ?,
            failed_count = ?,
            status = 'completed'
        WHERE run_id = ?
    ''', (success_count, failed_count, run_id))
    conn.commit()
    yield {
        'type': 'complete',
        'total': total,
        'success_count': success_count,
        'failed_count': failed_count,
        'failed_list': failed_list,
        'run_id': run_id,
        'duration_seconds': duration_seconds,
        'avg_rate': avg_rate
    }


def test_refresh_token(client_id: str, refresh_token: str) -> tuple[bool, str]:
    """测试 refresh token 是否有效，返回 (是否成功, 错误信息)"""
    try:
        # 尝试使用 Graph API 获取 access token
        # 使用与 get_access_token_graph 相同的 scope，确保一致性
        res = post_with_backoff(
            TOKEN_URL_GRAPH,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "https://graph.microsoft.com/.default"
            },
            timeout=30
        )

        if res.status_code == 200:
            return True, None
        else:
            error_data = res.json()
            error_msg = error_data.get('error_description', error_data.get('error', '未知错误'))
            return False, error_msg
    except Exception as e:
        return False, f"请求异常: {str(e)}"


@app.route('/api/accounts/<int:account_id>/refresh', methods=['POST'])
@login_required
def api_refresh_account(account_id):
    """刷新单个账号的 token"""
    db = get_db()
    cursor = db.execute('SELECT id, email, client_id, refresh_token FROM accounts WHERE id = ?', (account_id,))
    account = cursor.fetchone()

    if not account:
        error_payload = build_error_payload(
            "ACCOUNT_NOT_FOUND",
            "账号不存在",
            "NotFoundError",
            404,
            f"account_id={account_id}"
        )
        return jsonify({'success': False, 'error': error_payload})

    account_id = account['id']
    account_email = account['email']
    client_id = account['client_id']
    encrypted_refresh_token = account['refresh_token']

    # 解密 refresh_token
    try:
        refresh_token = decrypt_data(encrypted_refresh_token) if encrypted_refresh_token else encrypted_refresh_token
    except Exception as e:
        error_msg = f"解密 token 失败: {str(e)}"
        log_refresh_result(account_id, account_email, 'manual', 'failed', error_msg)
        error_payload = build_error_payload(
            "TOKEN_DECRYPT_FAILED",
            "Token 解密失败",
            "DecryptionError",
            500,
            error_msg
        )
        return jsonify({'success': False, 'error': error_payload})

    # 测试 refresh token
    success, error_msg = test_refresh_token(client_id, refresh_token)

    # 记录刷新结果
    log_refresh_result(account_id, account_email, 'manual', 'success' if success else 'failed', error_msg)

    if success:
        return jsonify({'success': True, 'message': 'Token 刷新成功'})

    error_payload = build_error_payload(
        "TOKEN_REFRESH_FAILED",
        "Token 刷新失败",
        "RefreshTokenError",
        400,
        error_msg or "未知错误"
    )
    return jsonify({'success': False, 'error': error_payload})


@app.route('/api/accounts/refresh-all', methods=['GET'])
@login_required
def api_refresh_all_accounts():
    """刷新所有账号的 token（流式响应，实时返回进度）"""
    import json

    force = request.args.get('force', 'false').lower() == 'true'
    resume = request.args.get('resume', 'true').lower() == 'true'

    def generate():
        # 在生成器内部直接创建数据库连接
        conn = sqlite3.connect(DATABASE, timeout=10)
        configure_sqlite(conn)
        conn.row_factory = sqlite3.Row

        try:
            # 清理超过半年的刷新记录
            try:
                conn.execute("DELETE FROM account_refresh_logs WHERE created_at < datetime('now', '-6 months')")
                conn.commit()
            except Exception as e:
                print(f"清理旧记录失败: {str(e)}")

            cursor = conn.execute(
                "SELECT id, email, client_id, refresh_token FROM accounts WHERE status = 'active' ORDER BY id"
            )
            accounts = cursor.fetchall()
            config = _resolve_refresh_config(conn, len(accounts), mode='full')

            for event in _refresh_accounts_generator(
                conn=conn,
                accounts=accounts,
                refresh_type='manual',
                delay_seconds=config['delay_seconds'],
                resume=resume,
                max_workers=config['max_workers'],
                batch_size=config['batch_size'],
                scope_label='manual'
            ):
                yield f"data: {json.dumps(event)}\n\n"

        finally:
            conn.close()

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/accounts/<int:account_id>/retry-refresh', methods=['POST'])
@login_required
def api_retry_refresh_account(account_id):
    """重试单个失败账号的刷新"""
    return api_refresh_account(account_id)


@app.route('/api/accounts/refresh-failed', methods=['POST'])
@login_required
def api_refresh_failed_accounts():
    """重试所有失败的账号"""
    db = get_db()

    # 获取最近一次刷新失败的账号列表
    cursor = db.execute('''
        SELECT DISTINCT a.id, a.email, a.client_id, a.refresh_token
        FROM accounts a
        INNER JOIN (
            SELECT account_id, MAX(created_at) as last_refresh
            FROM account_refresh_logs
            GROUP BY account_id
        ) latest ON a.id = latest.account_id
        INNER JOIN account_refresh_logs l ON a.id = l.account_id AND l.created_at = latest.last_refresh
        WHERE l.status = 'failed' AND a.status = 'active'
    ''')
    accounts = cursor.fetchall()

    success_count = 0
    failed_count = 0
    failed_list = []

    for account in accounts:
        account_id = account['id']
        account_email = account['email']
        client_id = account['client_id']
        encrypted_refresh_token = account['refresh_token']

        # 解密 refresh_token
        try:
            refresh_token = decrypt_data(encrypted_refresh_token) if encrypted_refresh_token else encrypted_refresh_token
        except Exception as e:
            # 解密失败，记录错误
            failed_count += 1
            error_msg = f"解密 token 失败: {str(e)}"
            failed_list.append({
                'id': account_id,
                'email': account_email,
                'error': error_msg
            })
            log_refresh_result(account_id, account_email, 'retry', 'failed', error_msg)
            continue

        # 测试 refresh token
        success, error_msg = test_refresh_token(client_id, refresh_token)

        # 记录刷新结果
        log_refresh_result(account_id, account_email, 'retry', 'success' if success else 'failed', error_msg)

        if success:
            success_count += 1
        else:
            failed_count += 1
            failed_list.append({
                'id': account_id,
                'email': account_email,
                'error': error_msg
            })

    return jsonify({
        'success': True,
        'total': len(accounts),
        'success_count': success_count,
        'failed_count': failed_count,
        'failed_list': failed_list
    })


@app.route('/api/accounts/trigger-scheduled-refresh', methods=['GET'])
@login_required
def api_trigger_scheduled_refresh():
    """手动触发定时刷新（支持强制刷新）"""
    import json
    from datetime import datetime, timedelta

    force = request.args.get('force', 'false').lower() == 'true'
    resume = request.args.get('resume', 'true').lower() == 'true'

    # 获取配置
    refresh_interval_days = int(get_setting('refresh_interval_days', '30'))

    # 检查上次刷新时间
    db = get_db()
    cursor = db.execute('''
        SELECT MAX(created_at) as last_refresh
        FROM account_refresh_logs
        WHERE refresh_type = 'scheduled'
    ''')
    row = cursor.fetchone()
    last_refresh = row['last_refresh'] if row and row['last_refresh'] else None

    # 判断是否需要刷新（force=true 时跳过检查）
    if not force and last_refresh:
        last_refresh_time = datetime.fromisoformat(last_refresh)
        next_refresh_time = last_refresh_time + timedelta(days=refresh_interval_days)
        if datetime.now() < next_refresh_time:
            return jsonify({
                'success': False,
                'message': f'距离上次刷新未满 {refresh_interval_days} 天，下次刷新时间：{next_refresh_time.strftime("%Y-%m-%d %H:%M:%S")}',
                'last_refresh': last_refresh,
                'next_refresh': next_refresh_time.isoformat()
            })

    # 执行刷新（使用流式响应）
    def generate():
        conn = sqlite3.connect(DATABASE, timeout=10)
        configure_sqlite(conn)
        conn.row_factory = sqlite3.Row

        try:
            # 清理超过半年的刷新记录
            try:
                conn.execute("DELETE FROM account_refresh_logs WHERE created_at < datetime('now', '-6 months')")
                conn.commit()
            except Exception as e:
                print(f"清理旧记录失败: {str(e)}")

            cursor = conn.execute(
                "SELECT id, email, client_id, refresh_token FROM accounts WHERE status = 'active' ORDER BY id"
            )
            accounts = cursor.fetchall()
            config = _resolve_refresh_config(conn, len(accounts), mode='group')

            for event in _refresh_accounts_generator(
                conn=conn,
                accounts=accounts,
                refresh_type='scheduled',
                delay_seconds=config['delay_seconds'],
                resume=resume,
                max_workers=config['max_workers'],
                batch_size=config['batch_size'],
                scope_label='scheduled'
            ):
                yield f"data: {json.dumps(event)}\n\n"

        finally:
            conn.close()

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/accounts/refresh-logs', methods=['GET'])
@login_required
def api_get_refresh_logs():
    """获取所有账号的刷新历史（只返回全量刷新：manual 和 scheduled，近半年）"""
    db = get_db()
    limit = int(request.args.get('limit', 1000))
    offset = int(request.args.get('offset', 0))

    cursor = db.execute('''
        SELECT l.*, a.email as account_email
        FROM account_refresh_logs l
        LEFT JOIN accounts a ON l.account_id = a.id
        WHERE l.refresh_type IN ('manual', 'scheduled', 'group')
        AND l.created_at >= datetime('now', '-6 months')
        ORDER BY l.created_at DESC
        LIMIT ? OFFSET ?
    ''', (limit, offset))

    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row['id'],
            'account_id': row['account_id'],
            'account_email': row['account_email'] or row['account_email'],
            'refresh_type': row['refresh_type'],
            'status': row['status'],
            'error_message': row['error_message'],
            'created_at': row['created_at']
        })

    return jsonify({'success': True, 'logs': logs})


@app.route('/api/accounts/<int:account_id>/refresh-logs', methods=['GET'])
@login_required
def api_get_account_refresh_logs(account_id):
    """获取单个账号的刷新历史"""
    db = get_db()
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))

    cursor = db.execute('''
        SELECT * FROM account_refresh_logs
        WHERE account_id = ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    ''', (account_id, limit, offset))

    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row['id'],
            'account_id': row['account_id'],
            'account_email': row['account_email'],
            'refresh_type': row['refresh_type'],
            'status': row['status'],
            'error_message': row['error_message'],
            'created_at': row['created_at']
        })

    return jsonify({'success': True, 'logs': logs})


@app.route('/api/accounts/refresh-logs/failed', methods=['GET'])
@login_required
def api_get_failed_refresh_logs():
    """获取所有失败的刷新记录"""
    db = get_db()

    # 获取每个账号最近一次失败的刷新记录
    cursor = db.execute('''
        SELECT l.*, a.email as account_email, a.status as account_status
        FROM account_refresh_logs l
        INNER JOIN (
            SELECT account_id, MAX(created_at) as last_refresh
            FROM account_refresh_logs
            GROUP BY account_id
        ) latest ON l.account_id = latest.account_id AND l.created_at = latest.last_refresh
        LEFT JOIN accounts a ON l.account_id = a.id
        WHERE l.status = 'failed'
        ORDER BY l.created_at DESC
    ''')

    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row['id'],
            'account_id': row['account_id'],
            'account_email': row['account_email'] or row['account_email'],
            'account_status': row['account_status'],
            'refresh_type': row['refresh_type'],
            'status': row['status'],
            'error_message': row['error_message'],
            'created_at': row['created_at']
        })

    return jsonify({'success': True, 'logs': logs})


@app.route('/api/accounts/refresh-stats', methods=['GET'])
@login_required
def api_get_refresh_stats():
    """获取刷新统计信息（统计当前失败状态的邮箱数量）"""
    db = get_db()

    cursor = db.execute('''
        SELECT MAX(created_at) as last_refresh_time
        FROM account_refresh_logs
        WHERE refresh_type IN ('manual', 'scheduled')
    ''')
    row = cursor.fetchone()
    last_refresh_time = row['last_refresh_time'] if row else None

    cursor = db.execute('''
        SELECT COUNT(*) as total_accounts
        FROM accounts
        WHERE status = 'active'
    ''')
    total_accounts = cursor.fetchone()['total_accounts']

    cursor = db.execute('''
        SELECT COUNT(DISTINCT l.account_id) as failed_count
        FROM account_refresh_logs l
        INNER JOIN (
            SELECT account_id, MAX(created_at) as last_refresh
            FROM account_refresh_logs
            GROUP BY account_id
        ) latest ON l.account_id = latest.account_id AND l.created_at = latest.last_refresh
        INNER JOIN accounts a ON l.account_id = a.id
        WHERE l.status = 'failed' AND a.status = 'active'
    ''')
    failed_count = cursor.fetchone()['failed_count']

    return jsonify({
        'success': True,
        'stats': {
            'total': total_accounts,
            'success_count': total_accounts - failed_count,
            'failed_count': failed_count,
            'last_refresh_time': last_refresh_time
        }
    })


# ==================== 邮件 API ====================



# ==================== Email Deletion Helpers ====================

def delete_emails_graph(client_id: str, refresh_token: str, message_ids: List[str]) -> Dict[str, Any]:
    """通过 Graph API 批量删除邮件（永久删除）"""
    access_token = get_access_token_graph(client_id, refresh_token)
    if not access_token:
        return {"success": False, "error": "获取 Access Token 失败"}

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    # Graph API 不支持一次性批量删除所有邮件，需要逐个删除
    # 但可以使用 batch 请求来优化
    # https://learn.microsoft.com/en-us/graph/json-batching
    
    # 限制每批次请求数量（Graph API 限制为 20）
    BATCH_SIZE = 20
    success_count = 0
    failed_count = 0
    errors = []

    for i in range(0, len(message_ids), BATCH_SIZE):
        batch = message_ids[i:i + BATCH_SIZE]
        
        # 构造 batch 请求 body
        batch_requests = []
        for idx, msg_id in enumerate(batch):
            batch_requests.append({
                "id": str(idx),
                "method": "DELETE",
                "url": f"/me/messages/{msg_id}"
            })
        
        try:
            response = requests.post(
                "https://graph.microsoft.com/v1.0/$batch",
                headers=headers,
                json={"requests": batch_requests},
                timeout=30
            )
            
            if response.status_code == 200:
                results = response.json().get("responses", [])
                for res in results:
                    if res.get("status") in [200, 204]:
                        success_count += 1
                    else:
                        failed_count += 1
                        # 记录具体错误
                        errors.append(f"Msg ID: {batch[int(res['id'])]}, Status: {res.get('status')}")
            else:
                failed_count += len(batch)
                errors.append(f"Batch request failed: {response.text}")
                
        except Exception as e:
            failed_count += len(batch)
            errors.append(f"Network error: {str(e)}")

    return {
        "success": failed_count == 0,
        "success_count": success_count,
        "failed_count": failed_count,
        "errors": errors
    }

def delete_emails_imap(email_addr: str, client_id: str, refresh_token: str, message_ids: List[str], server: str) -> Dict[str, Any]:
    """通过 IMAP 删除邮件（永久删除）"""
    access_token = get_access_token_graph(client_id, refresh_token)
    if not access_token:
        return {"success": False, "error": "获取 Access Token 失败"}
        
    try:
        # 生成 OAuth2 认证字符串
        auth_string = 'user=%s\x01auth=Bearer %s\x01\x01' % (email_addr, access_token)
        
        # 连接 IMAP
        imap = imaplib.IMAP4_SSL(server, IMAP_PORT)
        imap.authenticate('XOAUTH2', lambda x: auth_string.encode('utf-8'))
        
        # 选择文件夹
        imap.select('INBOX')
        
        # IMAP 删除需要 UID。如果我们没有 UID，这很难。
        # 鉴于我们只实现了 Graph 删除，并且 fallback 到 IMAP 比较复杂，
        # 这里暂时返回不支持，或仅做简单的尝试（如果 ID 恰好是 UID）
        # 但通常 Graph ID 不是 UID。
        
        return {"success": False, "error": "IMAP 删除暂不支持 (ID 格式不兼容)"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.route('/api/emails/<email_addr>')
@login_required
def api_get_emails(email_addr):
    """获取邮件列表（支持分页，不使用缓存）"""
    account = get_account_by_email(email_addr)

    if not account:
        error_payload = build_error_payload(
            "ACCOUNT_NOT_FOUND",
            "账号不存在",
            "NotFoundError",
            404,
            f"email={email_addr}"
        )
        return jsonify({'success': False, 'error': error_payload})

    folder = request.args.get('folder', 'inbox')  # inbox, junkemail, deleteditems
    skip = int(request.args.get('skip', 0))
    top = int(request.args.get('top', 20))

    # 收集所有错误信息
    all_errors = {}

    # 1. 尝试 Graph API
    graph_result = get_emails_graph(account['client_id'], account['refresh_token'], folder, skip, top)
    if graph_result.get("success"):
        emails = graph_result.get("emails", [])
        # 更新刷新时间
        db = get_db()
        db.execute('''
            UPDATE accounts
            SET last_refresh_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE email = ?
        ''', (email_addr,))
        db.commit()

        # 格式化 Graph API 返回的数据
        formatted = []
        for e in emails:
            formatted.append({
                'id': e.get('id'),
                'subject': e.get('subject', '无主题'),
                'from': e.get('from', {}).get('emailAddress', {}).get('address', '未知'),
                'date': e.get('receivedDateTime', ''),
                'is_read': e.get('isRead', False),
                'has_attachments': e.get('hasAttachments', False),
                'body_preview': e.get('bodyPreview', '')
            })

        return jsonify({
            'success': True,
            'emails': formatted,
            'method': 'Graph API',
            'has_more': len(formatted) >= top
        })
    else:
        all_errors["graph"] = graph_result.get("error")

    imap_new_result = get_emails_imap_with_server(
        account['email'], account['client_id'], account['refresh_token'],
        folder, skip, top, IMAP_SERVER_NEW
    )
    if imap_new_result.get("success"):
        return jsonify({
            'success': True,
            'emails': imap_new_result.get("emails", []),
            'method': 'IMAP (New)',
            'has_more': False # IMAP 分页暂未完全实现，视情况
        })
    else:
        all_errors["imap_new"] = imap_new_result.get("error")

    # 3. 尝试旧版 IMAP (outlook.office365.com)
    imap_old_result = get_emails_imap_with_server(
        account['email'], account['client_id'], account['refresh_token'],
        folder, skip, top, IMAP_SERVER_OLD
    )
    if imap_old_result.get("success"):
        return jsonify({
            'success': True,
            'emails': imap_old_result.get("emails", []),
            'method': 'IMAP (Old)',
            'has_more': False
        })
    else:
        all_errors["imap_old"] = imap_old_result.get("error")

    return jsonify({
        'success': False, 
        'error': '无法获取邮件，所有方式均失败',
        'details': all_errors
    })

@app.route('/api/emails/delete', methods=['POST'])
@login_required
def api_delete_emails():
    """批量删除邮件（永久删除）"""
    data = request.json
    email_addr = data.get('email', '')
    message_ids = data.get('ids', [])
    
    if not email_addr or not message_ids:
        return jsonify({'success': False, 'error': '参数不完整'})

    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})

    # 1. 优先尝试 Graph API
    graph_res = delete_emails_graph(account['client_id'], account['refresh_token'], message_ids)
    if graph_res['success']:
        return jsonify(graph_res)
    
    # 2. 如果 Graph API 失败，目前暂不支持 IMAP 自动回退
    return jsonify(graph_res)



@app.route('/api/email/<email_addr>/<path:message_id>')
@login_required
def api_get_email_detail(email_addr, message_id):
    """获取邮件详情"""
    account = get_account_by_email(email_addr)

    if not account:
        return jsonify({'success': False, 'error': '账号不存在'})

    method = request.args.get('method', 'graph')
    folder = request.args.get('folder', 'inbox')

    if method == 'graph':
        detail = get_email_detail_graph(account['client_id'], account['refresh_token'], message_id)
        if detail:
            return jsonify({
                'success': True,
                'email': {
                    'id': detail.get('id'),
                    'subject': detail.get('subject', '无主题'),
                    'from': detail.get('from', {}).get('emailAddress', {}).get('address', '未知'),
                    'to': ', '.join([r.get('emailAddress', {}).get('address', '') for r in detail.get('toRecipients', [])]),
                    'cc': ', '.join([r.get('emailAddress', {}).get('address', '') for r in detail.get('ccRecipients', [])]),
                    'date': detail.get('receivedDateTime', ''),
                    'body': detail.get('body', {}).get('content', ''),
                    'body_type': detail.get('body', {}).get('contentType', 'text')
                }
            })

    # 如果 Graph API 失败，尝试 IMAP
    detail = get_email_detail_imap(account['email'], account['client_id'], account['refresh_token'], message_id, folder)
    if detail:
        return jsonify({'success': True, 'email': detail})

    return jsonify({'success': False, 'error': '获取邮件详情失败'})


# ==================== GPTMail 临时邮箱 API ====================

def gptmail_request(method: str, endpoint: str, params: dict = None, json_data: dict = None) -> Optional[Dict]:
    """发送 GPTMail API 请求"""
    try:
        url = f"{GPTMAIL_BASE_URL}{endpoint}"
        # 从数据库获取 API Key
        api_key = get_gptmail_api_key()
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json"
        }
        
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, params=params, timeout=30)
        elif method.upper() == 'POST':
            response = requests.post(url, headers=headers, json=json_data, timeout=30)
        elif method.upper() == 'DELETE':
            response = requests.delete(url, headers=headers, params=params, timeout=30)
        else:
            return None
        
        if response.status_code == 200:
            return response.json()
        else:
            return {'success': False, 'error': f'API 请求失败: {response.status_code}'}
    except Exception as e:
        return {'success': False, 'error': f'请求异常: {str(e)}'}


def generate_temp_email(prefix: str = None, domain: str = None) -> Optional[str]:
    """生成临时邮箱地址"""
    json_data = {}
    if prefix:
        json_data['prefix'] = prefix
    if domain:
        json_data['domain'] = domain
    
    if json_data:
        result = gptmail_request('POST', '/api/generate-email', json_data=json_data)
    else:
        result = gptmail_request('GET', '/api/generate-email')
    
    if result and result.get('success'):
        return result.get('data', {}).get('email')
    return None


def get_temp_emails_from_api(email_addr: str) -> Optional[List[Dict]]:
    """从 GPTMail API 获取邮件列表"""
    result = gptmail_request('GET', '/api/emails', params={'email': email_addr})
    
    if result and result.get('success'):
        return result.get('data', {}).get('emails', [])
    return None


def get_temp_email_detail_from_api(message_id: str) -> Optional[Dict]:
    """从 GPTMail API 获取邮件详情"""
    result = gptmail_request('GET', f'/api/email/{message_id}')
    
    if result and result.get('success'):
        return result.get('data')
    return None


def delete_temp_email_from_api(message_id: str) -> bool:
    """从 GPTMail API 删除邮件"""
    result = gptmail_request('DELETE', f'/api/email/{message_id}')
    return result and result.get('success', False)


def clear_temp_emails_from_api(email_addr: str) -> bool:
    """清空 GPTMail 邮箱的所有邮件"""
    result = gptmail_request('DELETE', '/api/emails/clear', params={'email': email_addr})
    return result and result.get('success', False)


# ==================== 临时邮箱数据库操作 ====================

def get_temp_email_group_id() -> int:
    """获取临时邮箱分组的 ID"""
    db = get_db()
    cursor = db.execute("SELECT id FROM groups WHERE name = '临时邮箱'")
    row = cursor.fetchone()
    return row['id'] if row else 2


def load_temp_emails() -> List[Dict]:
    """加载所有临时邮箱"""
    db = get_db()
    cursor = db.execute('SELECT * FROM temp_emails ORDER BY created_at DESC')
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def get_temp_email_by_address(email_addr: str) -> Optional[Dict]:
    """根据邮箱地址获取临时邮箱"""
    db = get_db()
    cursor = db.execute('SELECT * FROM temp_emails WHERE email = ?', (email_addr,))
    row = cursor.fetchone()
    return dict(row) if row else None


def add_temp_email(email_addr: str) -> bool:
    """添加临时邮箱"""
    db = get_db()
    try:
        db.execute('INSERT INTO temp_emails (email) VALUES (?)', (email_addr,))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def delete_temp_email(email_addr: str) -> bool:
    """删除临时邮箱及其所有邮件"""
    db = get_db()
    try:
        db.execute('DELETE FROM temp_email_messages WHERE email_address = ?', (email_addr,))
        db.execute('DELETE FROM temp_emails WHERE email = ?', (email_addr,))
        db.commit()
        return True
    except Exception:
        return False


def save_temp_email_messages(email_addr: str, messages: List[Dict]) -> int:
    """保存临时邮件到数据库"""
    db = get_db()
    saved = 0
    for msg in messages:
        try:
            db.execute('''
                INSERT OR REPLACE INTO temp_email_messages
                (message_id, email_address, from_address, subject, content, html_content, has_html, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                msg.get('id'),
                email_addr,
                msg.get('from_address', ''),
                msg.get('subject', ''),
                msg.get('content', ''),
                msg.get('html_content', ''),
                1 if msg.get('has_html') else 0,
                msg.get('timestamp', 0)
            ))
            saved += 1
        except Exception:
            continue
    db.commit()
    return saved


def get_temp_email_messages(email_addr: str) -> List[Dict]:
    """获取临时邮箱的所有邮件（从数据库）"""
    db = get_db()
    cursor = db.execute('''
        SELECT * FROM temp_email_messages
        WHERE email_address = ?
        ORDER BY timestamp DESC
    ''', (email_addr,))
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def get_temp_email_message_by_id(message_id: str) -> Optional[Dict]:
    """根据 ID 获取临时邮件"""
    db = get_db()
    cursor = db.execute('SELECT * FROM temp_email_messages WHERE message_id = ?', (message_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def delete_temp_email_message(message_id: str) -> bool:
    """删除临时邮件"""
    db = get_db()
    try:
        db.execute('DELETE FROM temp_email_messages WHERE message_id = ?', (message_id,))
        db.commit()
        return True
    except Exception:
        return False


def get_temp_email_count() -> int:
    """获取临时邮箱数量"""
    db = get_db()
    cursor = db.execute('SELECT COUNT(*) as count FROM temp_emails')
    row = cursor.fetchone()
    return row['count'] if row else 0


# ==================== 临时邮箱 API 路由 ====================

@app.route('/api/temp-emails', methods=['GET'])
@login_required
def api_get_temp_emails():
    """获取所有临时邮箱"""
    emails = load_temp_emails()
    return jsonify({'success': True, 'emails': emails})


@app.route('/api/temp-emails/generate', methods=['POST'])
@login_required
def api_generate_temp_email():
    """生成新的临时邮箱"""
    data = request.json or {}
    prefix = data.get('prefix')
    domain = data.get('domain')
    
    email_addr = generate_temp_email(prefix, domain)
    
    if email_addr:
        if add_temp_email(email_addr):
            return jsonify({'success': True, 'email': email_addr, 'message': '临时邮箱创建成功'})
        else:
            return jsonify({'success': False, 'error': '邮箱已存在'})
    else:
        return jsonify({'success': False, 'error': '生成临时邮箱失败，请稍后重试'})


@app.route('/api/temp-emails/<path:email_addr>', methods=['DELETE'])
@login_required
def api_delete_temp_email(email_addr):
    """删除临时邮箱"""
    if delete_temp_email(email_addr):
        return jsonify({'success': True, 'message': '临时邮箱已删除'})
    else:
        return jsonify({'success': False, 'error': '删除失败'})


@app.route('/api/temp-emails/<path:email_addr>/messages', methods=['GET'])
@login_required
def api_get_temp_email_messages(email_addr):
    """获取临时邮箱的邮件列表"""
    api_messages = get_temp_emails_from_api(email_addr)
    
    if api_messages:
        save_temp_email_messages(email_addr, api_messages)
    
    messages = get_temp_email_messages(email_addr)
    
    formatted = []
    for msg in messages:
        formatted.append({
            'id': msg.get('message_id'),
            'from': msg.get('from_address', '未知'),
            'subject': msg.get('subject', '无主题'),
            'body_preview': (msg.get('content', '') or '')[:200],
            'date': msg.get('created_at', ''),
            'timestamp': msg.get('timestamp', 0),
            'has_html': msg.get('has_html', 0)
        })
    
    return jsonify({
        'success': True,
        'emails': formatted,
        'count': len(formatted),
        'method': 'GPTMail'
    })


@app.route('/api/temp-emails/<path:email_addr>/messages/<path:message_id>', methods=['GET'])
@login_required
def api_get_temp_email_message_detail(email_addr, message_id):
    """获取临时邮件详情"""
    msg = get_temp_email_message_by_id(message_id)
    
    if not msg:
        api_msg = get_temp_email_detail_from_api(message_id)
        if api_msg:
            save_temp_email_messages(email_addr, [api_msg])
            msg = get_temp_email_message_by_id(message_id)
    
    if msg:
        return jsonify({
            'success': True,
            'email': {
                'id': msg.get('message_id'),
                'from': msg.get('from_address', '未知'),
                'to': email_addr,
                'subject': msg.get('subject', '无主题'),
                'body': msg.get('html_content') if msg.get('has_html') else msg.get('content', ''),
                'body_type': 'html' if msg.get('has_html') else 'text',
                'date': msg.get('created_at', ''),
                'timestamp': msg.get('timestamp', 0)
            }
        })
    else:
        return jsonify({'success': False, 'error': '邮件不存在'})


@app.route('/api/temp-emails/<path:email_addr>/messages/<path:message_id>', methods=['DELETE'])
@login_required
def api_delete_temp_email_message(email_addr, message_id):
    """删除临时邮件"""
    delete_temp_email_from_api(message_id)
    if delete_temp_email_message(message_id):
        return jsonify({'success': True, 'message': '邮件已删除'})
    else:
        return jsonify({'success': False, 'error': '删除失败'})


@app.route('/api/temp-emails/<path:email_addr>/clear', methods=['DELETE'])
@login_required
def api_clear_temp_email_messages(email_addr):
    """清空临时邮箱的所有邮件"""
    clear_temp_emails_from_api(email_addr)
    db = get_db()
    try:
        db.execute('DELETE FROM temp_email_messages WHERE email_address = ?', (email_addr,))
        db.commit()
        return jsonify({'success': True, 'message': '邮件已清空'})
    except Exception:
        return jsonify({'success': False, 'error': '清空失败'})


@app.route('/api/temp-emails/<path:email_addr>/refresh', methods=['POST'])
@login_required
def api_refresh_temp_email_messages(email_addr):
    """刷新临时邮箱的邮件"""
    api_messages = get_temp_emails_from_api(email_addr)
    
    if api_messages is not None:
        saved = save_temp_email_messages(email_addr, api_messages)
        messages = get_temp_email_messages(email_addr)
        
        formatted = []
        for msg in messages:
            formatted.append({
                'id': msg.get('message_id'),
                'from': msg.get('from_address', '未知'),
                'subject': msg.get('subject', '无主题'),
                'body_preview': (msg.get('content', '') or '')[:200],
                'date': msg.get('created_at', ''),
                'timestamp': msg.get('timestamp', 0),
                'has_html': msg.get('has_html', 0)
            })
        
        return jsonify({
            'success': True,
            'emails': formatted,
            'count': len(formatted),
            'new_count': saved,
            'method': 'GPTMail'
        })
    else:
        return jsonify({'success': False, 'error': '获取邮件失败'})


# ==================== OAuth Token API ====================

@app.route('/api/oauth/auth-url', methods=['GET'])
@login_required
def api_get_oauth_auth_url():
    """生成 OAuth 授权 URL"""
    import urllib.parse

    base_auth_url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_mode": "query",
        "scope": " ".join(OAUTH_SCOPES),
        "state": "12345"
    }
    auth_url = f"{base_auth_url}?{urllib.parse.urlencode(params)}"

    return jsonify({
        'success': True,
        'auth_url': auth_url,
        'client_id': OAUTH_CLIENT_ID,
        'redirect_uri': OAUTH_REDIRECT_URI
    })


@app.route('/api/oauth/exchange-token', methods=['POST'])
@login_required
def api_exchange_oauth_token():
    """使用授权码换取 Refresh Token"""
    import urllib.parse

    data = request.json
    redirected_url = data.get('redirected_url', '').strip()

    if not redirected_url:
        return jsonify({'success': False, 'error': '请提供授权后的完整 URL'})

    # 从 URL 中提取 code
    try:
        parsed_url = urllib.parse.urlparse(redirected_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        auth_code = query_params['code'][0]
    except (KeyError, IndexError):
        return jsonify({'success': False, 'error': '无法从 URL 中提取授权码，请检查 URL 是否正确'})

    # 使用 Code 换取 Token (Public Client 不需要 client_secret)
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    token_data = {
        "client_id": OAUTH_CLIENT_ID,
        "code": auth_code,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
        "scope": " ".join(OAUTH_SCOPES)
    }

    try:
        response = requests.post(token_url, data=token_data, timeout=30)
    except Exception as e:
        return jsonify({'success': False, 'error': f'请求失败: {str(e)}'})

    if response.status_code == 200:
        tokens = response.json()
        refresh_token = tokens.get('refresh_token')

        if not refresh_token:
            return jsonify({'success': False, 'error': '未能获取 Refresh Token'})

        return jsonify({
            'success': True,
            'refresh_token': refresh_token,
            'client_id': OAUTH_CLIENT_ID,
            'token_type': tokens.get('token_type'),
            'expires_in': tokens.get('expires_in'),
            'scope': tokens.get('scope')
        })
    else:
        error_data = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
        error_msg = error_data.get('error_description', response.text)
        return jsonify({'success': False, 'error': f'获取令牌失败: {error_msg}'})


# ==================== 设置 API ====================

@app.route('/api/settings/validate-cron', methods=['POST'])
@login_required
def api_validate_cron():
    """验证 Cron 表达式"""
    try:
        from croniter import croniter
        from datetime import datetime
    except ImportError:
        return jsonify({'success': False, 'error': 'croniter 库未安装，请运行: pip install croniter'})

    data = request.json
    cron_expr = data.get('cron_expression', '').strip()

    if not cron_expr:
        return jsonify({'success': False, 'error': 'Cron 表达式不能为空'})

    try:
        base_time = datetime.now()
        cron = croniter(cron_expr, base_time)

        next_run = cron.get_next(datetime)

        future_runs = []
        temp_cron = croniter(cron_expr, base_time)
        for _ in range(5):
            future_runs.append(temp_cron.get_next(datetime).isoformat())

        return jsonify({
            'success': True,
            'valid': True,
            'next_run': next_run.isoformat(),
            'future_runs': future_runs
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'valid': False,
            'error': f'Cron 表达式无效: {str(e)}'
        })


@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    """获取所有设置"""
    settings = get_all_settings()
    # 隐藏密码的部分字符
    if 'login_password' in settings:
        pwd = settings['login_password']
        if len(pwd) > 2:
            settings['login_password_masked'] = pwd[0] + '*' * (len(pwd) - 2) + pwd[-1]
        else:
            settings['login_password_masked'] = '*' * len(pwd)
    return jsonify({'success': True, 'settings': settings})


@app.route('/api/settings', methods=['PUT'])
@login_required
def api_update_settings():
    """更新设置"""
    data = request.json
    updated = []
    errors = []

    # 更新登录密码
    if 'login_password' in data:
        new_password = data['login_password'].strip()
        if new_password:
            if len(new_password) < 8:
                errors.append('密码长度至少为 8 位')
            else:
                # 哈希新密码
                hashed_password = hash_password(new_password)
                if set_setting('login_password', hashed_password):
                    updated.append('登录密码')
                else:
                    errors.append('更新登录密码失败')

    # 更新 GPTMail API Key
    if 'gptmail_api_key' in data:
        new_api_key = data['gptmail_api_key'].strip()
        if new_api_key:
            if set_setting('gptmail_api_key', new_api_key):
                updated.append('GPTMail API Key')
            else:
                errors.append('更新 GPTMail API Key 失败')

    # 更新刷新周期
    if 'refresh_interval_days' in data:
        try:
            days = int(data['refresh_interval_days'])
            if days < 1 or days > 90:
                errors.append('刷新周期必须在 1-90 天之间')
            elif set_setting('refresh_interval_days', str(days)):
                updated.append('刷新周期')
            else:
                errors.append('更新刷新周期失败')
        except ValueError:
            errors.append('刷新周期必须是数字')

    # 更新刷新间隔
    if 'refresh_delay_seconds' in data:
        try:
            seconds = int(data['refresh_delay_seconds'])
            if seconds < 0 or seconds > 60:
                errors.append('刷新间隔必须在 0-60 秒之间')
            elif set_setting('refresh_delay_seconds', str(seconds)):
                updated.append('刷新间隔')
            else:
                errors.append('更新刷新间隔失败')
        except ValueError:
            errors.append('刷新间隔必须是数字')

    # 更新 Cron 表达式
    if 'refresh_cron' in data:
        cron_expr = data['refresh_cron'].strip()
        if cron_expr:
            try:
                from croniter import croniter
                from datetime import datetime
                croniter(cron_expr, datetime.now())
                if set_setting('refresh_cron', cron_expr):
                    updated.append('Cron 表达式')
                else:
                    errors.append('更新 Cron 表达式失败')
            except ImportError:
                errors.append('croniter 库未安装')
            except Exception as e:
                errors.append(f'Cron 表达式无效: {str(e)}')

    # 更新刷新策略
    if 'use_cron_schedule' in data:
        use_cron = str(data['use_cron_schedule']).lower()
        if use_cron in ('true', 'false'):
            if set_setting('use_cron_schedule', use_cron):
                updated.append('刷新策略')
            else:
                errors.append('更新刷新策略失败')
        else:
            errors.append('刷新策略必须是 true 或 false')

    # 更新定时刷新开关
    if 'enable_scheduled_refresh' in data:
        enable = str(data['enable_scheduled_refresh']).lower()
        if enable in ('true', 'false'):
            if set_setting('enable_scheduled_refresh', enable):
                updated.append('定时刷新开关')
            else:
                errors.append('更新定时刷新开关失败')
        else:
            errors.append('定时刷新开关必须是 true 或 false')

    if errors:
        return jsonify({'success': False, 'error': '；'.join(errors)})

    if updated:
        return jsonify({'success': True, 'message': f'已更新：{", ".join(updated)}'})
    else:
        return jsonify({'success': False, 'error': '没有需要更新的设置'})


# ==================== 定时任务调度器 ====================

SCHEDULER_INSTANCE_ID = uuid.uuid4().hex
SCHEDULER_LOCK_KEY = "scheduler_lock"
SCHEDULER_LOCK_TTL_SECONDS = int(os.getenv("SCHEDULER_LOCK_TTL_SECONDS", "120"))
SCHEDULER_LOCK_HEARTBEAT_SECONDS = int(os.getenv("SCHEDULER_LOCK_HEARTBEAT_SECONDS", "60"))


def _load_scheduler_lock(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (SCHEDULER_LOCK_KEY,))
    row = cursor.fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _write_scheduler_lock(conn: sqlite3.Connection, owner_id: str) -> None:
    payload = {
        "owner": owner_id,
        "updated_at": datetime.now().isoformat()
    }
    conn.execute('''
        INSERT OR REPLACE INTO settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    ''', (SCHEDULER_LOCK_KEY, json.dumps(payload, ensure_ascii=True)))


def try_acquire_scheduler_lock() -> bool:
    """抢占调度器锁，避免多进程重复调度"""
    conn = sqlite3.connect(DATABASE, timeout=10)
    configure_sqlite(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        lock = _load_scheduler_lock(conn)
        if lock:
            updated_at = lock.get("updated_at")
            try:
                updated_time = datetime.fromisoformat(updated_at) if updated_at else None
            except Exception:
                updated_time = None
            if updated_time and (datetime.now() - updated_time).total_seconds() < SCHEDULER_LOCK_TTL_SECONDS:
                if lock.get("owner") != SCHEDULER_INSTANCE_ID:
                    conn.rollback()
                    return False
        _write_scheduler_lock(conn, SCHEDULER_INSTANCE_ID)
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def refresh_scheduler_lock() -> None:
    """刷新调度器锁心跳"""
    conn = sqlite3.connect(DATABASE, timeout=10)
    configure_sqlite(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        lock = _load_scheduler_lock(conn)
        if lock and lock.get("owner") == SCHEDULER_INSTANCE_ID:
            _write_scheduler_lock(conn, SCHEDULER_INSTANCE_ID)
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def init_scheduler():
    """初始化定时任务调度器"""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        import atexit

        scheduler = BackgroundScheduler()

        if not try_acquire_scheduler_lock():
            print("⚠ 调度器锁未获取，跳过调度器启动（可能已有实例运行）")
            return None

        def add_lock_heartbeat():
            scheduler.add_job(
                func=refresh_scheduler_lock,
                trigger='interval',
                seconds=SCHEDULER_LOCK_HEARTBEAT_SECONDS,
                id='scheduler_lock_heartbeat',
                name='Scheduler Lock Heartbeat',
                replace_existing=True
            )

        with app.app_context():
            enable_scheduled = get_setting('enable_scheduled_refresh', 'true').lower() == 'true'

            if not enable_scheduled:
                print("✓ 定时刷新已禁用")
                return None

            use_cron = get_setting('use_cron_schedule', 'false').lower() == 'true'

            if use_cron:
                cron_expr = get_setting('refresh_cron', '0 2 * * *')
                try:
                    from croniter import croniter
                    from datetime import datetime
                    croniter(cron_expr, datetime.now())

                    parts = cron_expr.split()
                    if len(parts) == 5:
                        minute, hour, day, month, day_of_week = parts
                        trigger = CronTrigger(
                            minute=minute,
                            hour=hour,
                            day=day,
                            month=month,
                            day_of_week=day_of_week
                        )
                        scheduler.add_job(
                            func=scheduled_refresh_task,
                            trigger=trigger,
                            id='token_refresh',
                            name='Token 定时刷新',
                            replace_existing=True
                        )
                        add_lock_heartbeat()
                        scheduler.start()
                        print(f"✓ 定时任务已启动：Cron 表达式 '{cron_expr}'")
                        atexit.register(lambda: scheduler.shutdown())
                        return scheduler
                    else:
                        print(f"⚠ Cron 表达式格式错误，回退到默认配置")
                except Exception as e:
                    print(f"⚠ Cron 表达式解析失败: {str(e)}，回退到默认配置")

            refresh_interval_days = int(get_setting('refresh_interval_days', '30'))
            scheduler.add_job(
                func=scheduled_refresh_task,
                trigger=CronTrigger(hour=2, minute=0),
                id='token_refresh',
                name='Token 定时刷新',
                replace_existing=True
            )

            add_lock_heartbeat()
            scheduler.start()
            print(f"✓ 定时任务已启动：每天凌晨 2:00 检查刷新（周期：{refresh_interval_days} 天）")

        atexit.register(lambda: scheduler.shutdown())

        return scheduler
    except ImportError:
        print("⚠ APScheduler 未安装，定时任务功能不可用")
        print("  安装命令：pip install APScheduler>=3.10.0")
        return None
    except Exception as e:
        print(f"⚠ 定时任务初始化失败：{str(e)}")
        return None


def scheduled_refresh_task():
    """定时刷新任务（由调度器调用）"""
    from datetime import datetime, timedelta

    try:
        with app.app_context():
            enable_scheduled = get_setting('enable_scheduled_refresh', 'true').lower() == 'true'

            if not enable_scheduled:
                print(f"[定时任务] 定时刷新已禁用，跳过执行")
                return

            use_cron = get_setting('use_cron_schedule', 'false').lower() == 'true'

            if use_cron:
                print(f"[定时任务] 使用 Cron 调度，直接执行刷新...")
                trigger_refresh_internal()
                print(f"[定时任务] Token 刷新完成")
                return

            refresh_interval_days = int(get_setting('refresh_interval_days', '30'))

        conn = sqlite3.connect(DATABASE, timeout=10)
        configure_sqlite(conn)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute('''
            SELECT MAX(created_at) as last_refresh
            FROM account_refresh_logs
            WHERE refresh_type = 'scheduled'
        ''')
        row = cursor.fetchone()
        conn.close()

        last_refresh = row['last_refresh'] if row and row['last_refresh'] else None

        if last_refresh:
            last_refresh_time = datetime.fromisoformat(last_refresh)
            next_refresh_time = last_refresh_time + timedelta(days=refresh_interval_days)
            if datetime.now() < next_refresh_time:
                print(f"[定时任务] 距离上次刷新未满 {refresh_interval_days} 天，跳过本次刷新")
                return

        print(f"[定时任务] 开始执行 Token 刷新...")
        trigger_refresh_internal()
        print(f"[定时任务] Token 刷新完成")

    except Exception as e:
        print(f"[定时任务] 执行失败：{str(e)}")


def trigger_refresh_internal():
    """内部触发刷新（不通过 HTTP）"""
    conn = sqlite3.connect(DATABASE, timeout=10)
    configure_sqlite(conn)
    conn.row_factory = sqlite3.Row

    try:
        # 获取刷新间隔配置
        cursor_settings = conn.execute("SELECT value FROM settings WHERE key = 'refresh_delay_seconds'")
        delay_row = cursor_settings.fetchone()
        delay_seconds = int(delay_row['value']) if delay_row else 5

        # 清理超过半年的刷新记录
        conn.execute("DELETE FROM account_refresh_logs WHERE created_at < datetime('now', '-6 months')")
        conn.commit()

        cursor = conn.execute(
            "SELECT id, email, client_id, refresh_token FROM accounts WHERE status = 'active' ORDER BY id"
        )
        accounts = cursor.fetchall()

        summary = None
        for event in _refresh_accounts_generator(
            conn=conn,
            accounts=accounts,
            refresh_type='scheduled',
            delay_seconds=delay_seconds,
            resume=True
        ):
            if event.get('type') == 'complete':
                summary = event

        if summary:
            print(f"[定时任务] 刷新结果：总计 {summary.get('total')}，成功 {summary.get('success_count')}，失败 {summary.get('failed_count')}")

    finally:
        conn.close()


# 模块加载时初始化调度器（适配 Gunicorn）
SCHEDULER = None
if os.getenv("ENABLE_SCHEDULER", "true").lower() == "true":
    SCHEDULER = init_scheduler()
else:
    print("✓ ENABLE_SCHEDULER=false，已跳过调度器启动")


# ==================== 错误处理 ====================

@app.errorhandler(400)
def bad_request(error):
    """处理400错误"""
    print(f"400 Bad Request: {error}")
    return jsonify({'success': False, 'error': '请求格式错误'}), 400


@app.errorhandler(Exception)
def handle_exception(error):
    """处理未捕获的异常"""
    print(f"Unhandled exception: {error}")
    import traceback
    traceback.print_exc()
    return jsonify({'success': False, 'error': str(error)}), 500


# ==================== 主程序 ====================

if __name__ == '__main__':
    # 从环境变量获取配置
    port = int(os.getenv('PORT', 5000))
    host = os.getenv('HOST', '0.0.0.0')
    debug = os.getenv('FLASK_ENV', 'production') != 'production'

    print("=" * 60)
    print("Outlook 邮件 Web 应用")
    print("=" * 60)
    print(f"访问地址: http://{host}:{port}")
    print(f"运行模式: {'开发' if debug else '生产'}")
    print("=" * 60)

    app.run(debug=debug, host=host, port=port)
