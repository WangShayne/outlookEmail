#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量注册模板（并发池 + 重试 + 失败回收 + 领号等待优化）

依赖：
  pip install requests

最简方式：
  在 .env 中设置：
    SECRET_KEY=...
    LOGIN_PASSWORD=...

脚本会读取 .env 并自动映射：
  APP_EXTERNAL_API_KEY = SECRET_KEY
  APP_LOGIN_PASSWORD  = LOGIN_PASSWORD

可选环境变量（均有默认值）：
  APP_BASE_URL=http://localhost:5001
  APP_TASK_COUNT=10
  APP_CONCURRENCY=3
  APP_MAX_RETRIES=2
  APP_POLL_INTERVAL=3
  APP_POLL_TIMEOUT=120
  APP_SIMULATE_REGISTER=1  # 1=模拟注册流程

优化项（可选）：
  APP_LOG_LEVEL=INFO
  APP_REQUEST_TIMEOUT=20
  APP_EMAIL_FOLDER=inbox
  APP_CODE_REGEX=\\b(\\d{4,8})\\b
  APP_NO_EMAIL_BACKOFF=5
  APP_NO_EMAIL_MAX_WAIT=300
  APP_FAILED_DUMP=failed_tasks.json
"""

import os
import time
import json
import queue
import random
import threading
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def load_dotenv_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # .env 读取失败时保持静默
        return


load_dotenv_file()

# 从 .env 兼容读取
if "APP_EXTERNAL_API_KEY" not in os.environ and os.getenv("SECRET_KEY"):
    os.environ["APP_EXTERNAL_API_KEY"] = os.getenv("SECRET_KEY", "")
if "APP_LOGIN_PASSWORD" not in os.environ and os.getenv("LOGIN_PASSWORD"):
    os.environ["APP_LOGIN_PASSWORD"] = os.getenv("LOGIN_PASSWORD", "")


BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5001").rstrip("/")
API_KEY = os.getenv("APP_EXTERNAL_API_KEY", "")
LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "")

TASK_COUNT = int(os.getenv("APP_TASK_COUNT", "10"))
CONCURRENCY = int(os.getenv("APP_CONCURRENCY", "3"))
MAX_RETRIES = int(os.getenv("APP_MAX_RETRIES", "2"))
POLL_INTERVAL = float(os.getenv("APP_POLL_INTERVAL", "3"))
POLL_TIMEOUT = int(os.getenv("APP_POLL_TIMEOUT", "120"))
SIMULATE_REGISTER = os.getenv("APP_SIMULATE_REGISTER", "1") == "1"
LOG_LEVEL = os.getenv("APP_LOG_LEVEL", "INFO").upper()
REQUEST_TIMEOUT = int(os.getenv("APP_REQUEST_TIMEOUT", "20"))
EMAIL_FOLDER = os.getenv("APP_EMAIL_FOLDER", "inbox")
NO_EMAIL_BACKOFF = float(os.getenv("APP_NO_EMAIL_BACKOFF", "5"))
NO_EMAIL_MAX_WAIT = int(os.getenv("APP_NO_EMAIL_MAX_WAIT", "300"))
FAILED_DUMP = os.getenv("APP_FAILED_DUMP", "failed_tasks.json")

code_pattern = os.getenv("APP_CODE_REGEX", r"\b(\d{4,8})\b")
try:
    CODE_RE = re.compile(code_pattern)
except re.error:
    CODE_RE = re.compile(r"\b(\d{4,8})\b")


@dataclass
class Task:
    task_id: int
    attempts: int = 0
    no_email_wait: float = 0.0


def require_env():
    if not API_KEY:
        raise RuntimeError("APP_EXTERNAL_API_KEY is required (or set SECRET_KEY in .env)")
    if not LOGIN_PASSWORD:
        raise RuntimeError("APP_LOGIN_PASSWORD is required for reading emails (or set LOGIN_PASSWORD in .env)")


def build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PUT", "DELETE")
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def login(session: requests.Session) -> None:
    resp = session.post(
        f"{BASE_URL}/login",
        json={"password": LOGIN_PASSWORD},
        timeout=REQUEST_TIMEOUT
    )
    data = safe_json(resp)
    if not data.get("success"):
        raise RuntimeError(f"Login failed: {data}")


def checkout_email(session: requests.Session, owner: str) -> Tuple[Optional[dict], Optional[str]]:
    headers = {"X-API-Key": API_KEY}
    payload = {
        "owner": owner,
        "ttl_seconds": max(POLL_TIMEOUT + 60, 300)
    }
    resp = session.post(
        f"{BASE_URL}/api/external/checkout",
        json=payload,
        headers=headers,
        timeout=REQUEST_TIMEOUT
    )
    data = safe_json(resp)
    if resp.status_code == 401:
        raise RuntimeError("Unauthorized: check APP_EXTERNAL_API_KEY (SECRET_KEY)")
    if resp.status_code == 404 and data.get("error") == "没有可用邮箱":
        return None, "no_available"
    if not data.get("success"):
        return None, data.get("error") or f"HTTP {resp.status_code}"
    return data, None


def complete_checkout(session: requests.Session, lease_id: str, result: str) -> None:
    headers = {"X-API-Key": API_KEY}
    payload = {"lease_id": lease_id, "result": result}
    session.post(
        f"{BASE_URL}/api/external/checkout/complete",
        json=payload,
        headers=headers,
        timeout=REQUEST_TIMEOUT
    )


def extract_code(text: str) -> Optional[str]:
    if not text:
        return None
    match = CODE_RE.search(text)
    return match.group(1) if match else None


def poll_for_code(session: requests.Session, email: str) -> Optional[str]:
    deadline = time.time() + POLL_TIMEOUT
    checked_ids = set()
    while time.time() < deadline:
        resp = session.get(
            f"{BASE_URL}/api/emails/{quote(email)}",
            params={"folder": EMAIL_FOLDER, "top": 20, "skip": 0},
            timeout=REQUEST_TIMEOUT
        )
        data = safe_json(resp)
        if resp.status_code == 401 or data.get("need_login"):
            login(session)
            continue
        if data.get("success"):
            emails = data.get("emails", [])
            # 先尝试预览
            for msg in emails:
                text = f"{msg.get('subject', '')} {msg.get('body_preview', '')}"
                code = extract_code(text)
                if code:
                    return code
            # 再尝试详情
            for msg in emails:
                msg_id = msg.get("id")
                if not msg_id:
                    continue
                if msg_id in checked_ids:
                    continue
                checked_ids.add(msg_id)
                detail_resp = session.get(
                    f"{BASE_URL}/api/email/{quote(email)}/{quote(msg_id, safe='')}",
                    params={"folder": EMAIL_FOLDER},
                    timeout=REQUEST_TIMEOUT
                )
                detail_data = safe_json(detail_resp)
                if detail_resp.status_code == 401 or detail_data.get("need_login"):
                    login(session)
                    continue
                if detail_data.get("success"):
                    body = detail_data.get("email", {}).get("body", "")
                    code = extract_code(body)
                    if code:
                        return code
        time.sleep(POLL_INTERVAL)
    return None


def simulate_register(email: str) -> bool:
    # 模拟注册请求耗时
    time.sleep(random.uniform(0.3, 1.2))
    return True


def submit_verification_code(email: str, code: str) -> bool:
    # TODO: 在这里调用你的外部系统完成验证码提交
    # 返回 True 表示注册成功
    time.sleep(random.uniform(0.2, 0.6))
    return True


def process_task(session: requests.Session, task: Task, worker_id: str) -> Tuple[str, str, Optional[str]]:
    lease = None
    try:
        lease, err = checkout_email(session, owner=worker_id)
        if not lease:
            if err == "no_available":
                logging.info("task=%s no available email", task.task_id)
                return "no_email", "no_available", None
            return "retry", err or "checkout_failed", None

        email = lease["email"]
        lease_id = lease["lease_id"]

        if SIMULATE_REGISTER:
            ok = simulate_register(email)
        else:
            raise RuntimeError("Please implement register call in simulate_register()")

        if not ok:
            complete_checkout(session, lease_id, "failed")
            return "retry", "register_failed", email

        code = poll_for_code(session, email)
        if not code:
            complete_checkout(session, lease_id, "failed")
            return "retry", "no_code", email

        verify_ok = submit_verification_code(email, code)
        complete_checkout(session, lease_id, "success" if verify_ok else "failed")
        return ("success" if verify_ok else "retry"), ("verify_failed" if not verify_ok else "ok"), email
    except Exception as exc:
        if lease and lease.get("lease_id"):
            complete_checkout(session, lease["lease_id"], "failed")
        logging.exception("task=%s failed: %s", task.task_id, exc)
        return "retry", str(exc), None


def worker_loop(task_queue: queue.Queue, results: dict, lock: threading.Lock, failed_list: list, worker_id: str):
    session = build_session()
    try:
        login(session)
    except Exception as exc:
        logging.error("worker=%s login failed: %s", worker_id, exc)
        return

    while True:
        try:
            task: Task = task_queue.get(timeout=1)
        except queue.Empty:
            return

        status, reason, email = process_task(session, task, worker_id)
        if status == "success":
            with lock:
                results["success"] += 1
        elif status == "no_email":
            wait_s = min(NO_EMAIL_BACKOFF * (1 + task.no_email_wait / max(1, NO_EMAIL_BACKOFF)), 30)
            task.no_email_wait += wait_s
            if task.no_email_wait >= NO_EMAIL_MAX_WAIT:
                with lock:
                    results["failed"] += 1
                    failed_list.append({
                        "task_id": task.task_id,
                        "reason": "no_available_email",
                        "email": email
                    })
            else:
                time.sleep(wait_s)
                task_queue.put(task)
        else:
            task.attempts += 1
            if task.attempts <= MAX_RETRIES:
                time.sleep(min(2 ** task.attempts + random.uniform(0, 0.5), 10))
                task_queue.put(task)
            else:
                with lock:
                    results["failed"] += 1
                    failed_list.append({
                        "task_id": task.task_id,
                        "reason": reason,
                        "email": email
                    })
        task_queue.task_done()


def main():
    require_env()

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s"
    )

    if TASK_COUNT <= 0:
        logging.info("no tasks to run")
        return

    concurrency = max(1, min(CONCURRENCY, TASK_COUNT))

    task_queue: queue.Queue = queue.Queue()
    for i in range(TASK_COUNT):
        task_queue.put(Task(task_id=i + 1))

    results = {"success": 0, "failed": 0}
    failed_list = []
    lock = threading.Lock()
    threads = []
    for i in range(concurrency):
        t = threading.Thread(
            target=worker_loop,
            args=(task_queue, results, lock, failed_list, f"worker-{i+1}"),
            daemon=True
        )
        t.start()
        threads.append(t)

    task_queue.join()
    for t in threads:
        t.join(timeout=0.1)

    if failed_list:
        try:
            payload = {
                "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "failed": failed_list
            }
            with open(FAILED_DUMP, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logging.warning("failed to write %s: %s", FAILED_DUMP, exc)

    logging.info("done: success=%s failed=%s", results["success"], results["failed"])


if __name__ == "__main__":
    main()
