#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量注册模板（并发池 + 重试 + 失败回收）

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
  APP_BASE_URL=http://localhost:8080
  APP_GROUP_ID=1
  APP_TASK_COUNT=10
  APP_CONCURRENCY=3
  APP_MAX_RETRIES=2
  APP_POLL_INTERVAL=3
  APP_POLL_TIMEOUT=120
  APP_SIMULATE_REGISTER=1  # 1=模拟注册流程
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
from typing import Optional, Tuple
from urllib.parse import quote

import requests


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


BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("APP_EXTERNAL_API_KEY", "")
LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "")

GROUP_ID = int(os.getenv("APP_GROUP_ID", "1"))
TASK_COUNT = int(os.getenv("APP_TASK_COUNT", "10"))
CONCURRENCY = int(os.getenv("APP_CONCURRENCY", "3"))
MAX_RETRIES = int(os.getenv("APP_MAX_RETRIES", "2"))
POLL_INTERVAL = float(os.getenv("APP_POLL_INTERVAL", "3"))
POLL_TIMEOUT = int(os.getenv("APP_POLL_TIMEOUT", "120"))
SIMULATE_REGISTER = os.getenv("APP_SIMULATE_REGISTER", "1") == "1"

CODE_RE = re.compile(r"\b(\d{4,8})\b")


@dataclass
class Task:
    task_id: int
    attempts: int = 0


def require_env():
    if not API_KEY:
        raise RuntimeError("APP_EXTERNAL_API_KEY is required (or set SECRET_KEY in .env)")
    if not LOGIN_PASSWORD:
        raise RuntimeError("APP_LOGIN_PASSWORD is required for reading emails (or set LOGIN_PASSWORD in .env)")


def login(session: requests.Session) -> None:
    resp = session.post(f"{BASE_URL}/login", json={"password": LOGIN_PASSWORD}, timeout=15)
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Login failed: {data}")


def checkout_email(session: requests.Session, owner: str) -> Optional[dict]:
    headers = {"X-API-Key": API_KEY}
    payload = {
        "group_id": GROUP_ID,
        "owner": owner,
        "ttl_seconds": max(POLL_TIMEOUT + 60, 300)
    }
    resp = session.post(f"{BASE_URL}/api/external/checkout", json=payload, headers=headers, timeout=15)
    data = resp.json()
    if not data.get("success"):
        return None
    return data


def complete_checkout(session: requests.Session, lease_id: str, result: str) -> None:
    headers = {"X-API-Key": API_KEY}
    payload = {"lease_id": lease_id, "result": result}
    session.post(f"{BASE_URL}/api/external/checkout/complete", json=payload, headers=headers, timeout=10)


def extract_code(text: str) -> Optional[str]:
    if not text:
        return None
    match = CODE_RE.search(text)
    return match.group(1) if match else None


def poll_for_code(session: requests.Session, email: str) -> Optional[str]:
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        resp = session.get(
            f"{BASE_URL}/api/emails/{quote(email)}",
            params={"folder": "inbox", "top": 20, "skip": 0},
            timeout=20
        )
        data = resp.json()
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
                detail = session.get(
                    f"{BASE_URL}/api/email/{quote(email)}/{quote(msg_id, safe='')}",
                    params={"method": "graph", "folder": "inbox"},
                    timeout=20
                ).json()
                if detail.get("success"):
                    body = detail.get("email", {}).get("body", "")
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


def process_task(session: requests.Session, task: Task, worker_id: str) -> bool:
    lease = None
    try:
        lease = checkout_email(session, owner=worker_id)
        if not lease:
            logging.info("task=%s no available email", task.task_id)
            return False

        email = lease["email"]
        lease_id = lease["lease_id"]

        if SIMULATE_REGISTER:
            ok = simulate_register(email)
        else:
            raise RuntimeError("Please implement register call in simulate_register()")

        if not ok:
            complete_checkout(session, lease_id, "failed")
            return False

        code = poll_for_code(session, email)
        if not code:
            complete_checkout(session, lease_id, "failed")
            return False

        verify_ok = submit_verification_code(email, code)
        complete_checkout(session, lease_id, "success" if verify_ok else "failed")
        return verify_ok
    except Exception as exc:
        if lease and lease.get("lease_id"):
            complete_checkout(session, lease["lease_id"], "failed")
        logging.exception("task=%s failed: %s", task.task_id, exc)
        return False


def worker_loop(task_queue: queue.Queue, results: dict, worker_id: str):
    session = requests.Session()
    login(session)

    while True:
        try:
            task: Task = task_queue.get(timeout=1)
        except queue.Empty:
            return

        ok = process_task(session, task, worker_id)
        if ok:
            results["success"] += 1
        else:
            task.attempts += 1
            if task.attempts <= MAX_RETRIES:
                time.sleep(min(2 ** task.attempts, 10))
                task_queue.put(task)
            else:
                results["failed"] += 1
        task_queue.task_done()


def main():
    require_env()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    task_queue: queue.Queue = queue.Queue()
    for i in range(TASK_COUNT):
        task_queue.put(Task(task_id=i + 1))

    results = {"success": 0, "failed": 0}
    threads = []
    for i in range(CONCURRENCY):
        t = threading.Thread(target=worker_loop, args=(task_queue, results, f"worker-{i+1}"), daemon=True)
        t.start()
        threads.append(t)

    task_queue.join()
    for t in threads:
        t.join(timeout=0.1)

    logging.info("done: success=%s failed=%s", results["success"], results["failed"])


if __name__ == "__main__":
    main()
