# 邮箱验证器：MX 查询 + SMTP 握手（零 API 依赖，纯标准库）
# 用法: python3 email-verify.py <email> [email...]
import smtplib
import socket
import sys

def get_mx(domain):
    """解析 MX 记录（用 dig 命令，macOS/Linux 都有）"""
    import subprocess
    try:
        out = subprocess.run(["dig", "+short", "MX", domain], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
        mx = []
        for l in lines:
            parts = l.split()
            if parts and not parts[0].startswith(";"):
                mx.append(parts[-1].rstrip("."))
        return mx
    except Exception:
        return []

def smtp_verify(email, mx_host, timeout=8):
    """SMTP RCPT TO 握手验证邮箱是否存在。返回 (verified, reason)"""
    try:
        with smtplib.SMTP(mx_host, 25, timeout=timeout) as s:
            s.ehlo("verify.local")
            code, _ = s.mail("check@example.com")
            if code != 250:
                return False, f"MAIL rejected {code}"
            code, _ = s.rcpt(email)
            if code == 250:
                return True, "RCPT 250"
            if code in (550, 551, 553):
                return False, f"RCPT {code} (邮箱不存在)"
            return False, f"RCPT {code} (不确定)"
    except (smtplib.SMTPServerDisconnected, socket.timeout, ConnectionRefusedError, OSError) as e:
        return False, f"SMTP 不可达 ({type(e).__name__})"

def guess_emails(domain):
    """常见模式猜测"""
    return [f"info@{domain}", f"sales@{domain}", f"purchase@{domain}", f"contact@{domain}", f"admin@{domain}"]

def main():
    emails = sys.argv[1:]
    if not emails:
        print("用法: python3 email-verify.py info@example.com sales@example.com")
        return
    for email in emails:
        domain = email.split("@")[1].lower()
        mxs = get_mx(domain)
        if not mxs:
            print(f"{email}: NO MX (域名无邮件服务)")
            continue
        ok, reason = smtp_verify(email, mxs[0])
        print(f"{email}: {'✓ 有效' if ok else '✗ ' + reason} (MX: {mxs[0]})")

if __name__ == "__main__":
    main()
