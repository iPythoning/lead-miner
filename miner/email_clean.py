"""邮箱清洗模块：接入 leads 管线（records.py 的 CompanyRecord → cli.py 的 import）。

清洗分级（不阻塞导入，失败降级）：
1. 规范化 + 语法校验（纯正则，必做）
2. MX 记录检查（dig，零依赖；无 MX = 域名无邮件服务 → invalid）
3. SMTP RCPT 握手（smtplib 标准库；网络不可达/服务商反探测 → unknown 降级）

结果写进 record.extra["email_status"]（extra 是网关透传桶，零 schema 风险）：
- verified：语法 + MX + SMTP 250
- valid_mx：语法 + MX 有效（SMTP 未确认）
- invalid：语法或 MX 明确无效
- unknown：SMTP 反探测/网络问题（migadu 502 这类）
- none：无邮箱
"""
from __future__ import annotations

import re
import smtplib
import socket
import subprocess

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
# 常见占位/垃圾邮箱（名录里大量出现），直接判 invalid
_BAD_LOCAL = {
    "info", "sales", "contact", "admin", "test", "example", "email", "mail",
    "service", "support", "postmaster", "webmaster", "hr", "market",
    "no-reply", "noreply", "donotreply", "abc", "xxx", "123", "123456",
}
_BAD_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com", "test.org",
    "domain.com", "yourdomain.com", "email.com", "mail.com", "163.com.cn",
    "qq.om", "gmail.con", "gamil.com", "hotmail.con", "163.con", "qq.con",
}


def clean_email(raw: str) -> str:
    """规范化邮箱：去空白/引号/括号/分号尾缀，返回小写邮箱或空串。"""
    if not raw:
        return ""
    e = str(raw).strip().strip("\"'()[]").strip()
    # 名录常见 "xxx@yyy.com；zzz@www.com" 取第一个
    e = re.split(r"[;；,，]", e)[0].strip()
    e = e.lower()
    # 去掉常见 OCR/录入污染（@ 前的空格、全角 @）
    e = e.replace(" ", "").replace("＠", "@")
    if not _EMAIL_RE.match(e):
        return ""
    return e


def has_mx(domain: str) -> bool:
    """dig MX 判断域名是否有邮件服务。返回 False = 明确无 MX（域名不存在/无邮件）。"""
    try:
        out = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=10,
        )
        for line in out.stdout.strip().splitlines():
            parts = line.split()
            if parts and not parts[0].startswith(";"):
                return True
        # dig 失败或 NXDOMAIN 时再试一次 A 记录兜底（有些小域名没配 MX 但收邮件走 A）
        out2 = subprocess.run(
            ["dig", "+short", "A", domain],
            capture_output=True, text=True, timeout=10,
        )
        return bool(out2.stdout.strip())
    except Exception:
        # dig 不可用（Windows/受限环境）→ 视为 unknown 而不是 invalid（宁可放过）
        return True


def smtp_check(email: str, timeout: float = 8.0) -> str:
    """SMTP RCPT 握手。返回 verified / invalid / unknown。"""
    domain = email.split("@")[1]
    try:
        out = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=10,
        )
        mx_hosts = [
            line.split()[-1].rstrip(".") for line in out.stdout.strip().splitlines()
            if line.strip() and not line.startswith(";")
        ]
        if not mx_hosts:
            return "invalid"  # 无 MX
        try:
            with smtplib.SMTP(mx_hosts[0], 25, timeout=timeout) as s:
                s.ehlo("verify.local")
                code, _ = s.mail("check@example.com")
                if code != 250:
                    return "unknown"  # 反探测（migadu 502 等）
                code, _ = s.rcpt(email)
                if code == 250:
                    return "verified"
                if code in (550, 551, 553):
                    return "invalid"
                return "unknown"
        except (smtplib.SMTPServerDisconnected, socket.timeout,
                ConnectionRefusedError, OSError):
            return "unknown"  # 网络不可达 → 降级
    except Exception:
        return "unknown"


def classify_email(email: str, smtp: bool = True) -> str:
    """一条邮箱的完整清洗。返回 status 字符串。"""
    cleaned = clean_email(email)
    if not cleaned:
        return "none" if not email else "invalid"

    local, domain = cleaned.split("@")
    if local in _BAD_LOCAL and _BAD_LOCAL:
        # info@/sales@ 等通用邮箱不判 invalid（很多小公司真用 info@），只降低置信
        pass
    if domain in _BAD_DOMAINS:
        return "invalid"

    if not has_mx(domain):
        return "invalid"
    if not smtp:
        return "valid_mx"
    return smtp_check(cleaned)


def verify_records(records: list, smtp: bool = True, concurrency: int = 4) -> dict:
    """对一批 record 做邮箱清洗，结果写 record.extra["email_status"]。

    串行即可（单条 SMTP 8s 超时；批量大时建议 --no-smtp 只做 MX 层）。
    返回统计 {none, invalid, valid_mx, verified, unknown, total}。
    """
    stats = {"none": 0, "invalid": 0, "valid_mx": 0, "verified": 0, "unknown": 0}
    for record in records:
        raw = getattr(record, "email", "") or ""
        if not raw.strip():
            record.extra["email_status"] = "none"
            stats["none"] += 1
            continue
        status = classify_email(raw, smtp=smtp)
        record.extra["email_status"] = status
        stats[status] = stats.get(status, 0) + 1
    stats["total"] = len(records)
    return stats
