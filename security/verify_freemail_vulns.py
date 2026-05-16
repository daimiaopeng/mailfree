#!/usr/bin/env python3
"""
Freemail 已知访问控制问题的只读验证脚本。

默认动态检查只执行登录和 GET 请求，不会删除邮件、修改设置、发送邮件、
取消邮件，也不会主动请求会把邮件标记为已读的详情接口。
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import re
import ssl
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_USER_IDS = "1-5"


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    text: str
    json_data: Any = None
    error: str = ""


@dataclass
class Finding:
    check_id: str
    title: str
    severity: str
    status: str
    evidence: list[str] = field(default_factory=list)
    recommendation: str = ""


class FreemailClient:
    def __init__(self, base_url: str, insecure: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.cookies)]
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{query}"

        data = None
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Cache-Control": "no-cache",
            "User-Agent": "freemail-vuln-verifier/1.0",
        }
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with self.opener.open(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return make_result(resp.status, dict(resp.headers), body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return make_result(exc.code, dict(exc.headers), body)
        except Exception as exc:  # noqa: BLE001 - 命令行验证脚本需要记录错误并继续执行。
            return HttpResult(status=0, headers={}, text="", error=str(exc))

    def jwt_payload(self) -> dict[str, Any]:
        for cookie in self.cookies:
            if cookie.name == "iding-session":
                parts = cookie.value.split(".")
                if len(parts) != 3:
                    return {}
                raw = parts[1] + "=" * (-len(parts[1]) % 4)
                try:
                    decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
                    return json.loads(decoded.decode("utf-8"))
                except Exception:
                    return {}
        return {}


def make_result(status: int, headers: dict[str, str], body: str) -> HttpResult:
    parsed = None
    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = None
    return HttpResult(status=status, headers=headers, text=body, json_data=parsed)


def parse_user_ids(raw: str) -> list[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            ids.update(range(min(start, end), max(start, end) + 1))
        else:
            ids.add(int(part))
    return sorted(i for i in ids if i > 0)


def mask_email(value: str, show_sensitive: bool = False) -> str:
    if show_sensitive:
        return value
    if "@" not in value:
        return mask_token(value, show_sensitive)
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "***"
    elif len(local) <= 6:
        masked_local = local[:1] + "***" + local[-1:]
    else:
        masked_local = local[:2] + "***" + local[-2:]
    return f"{masked_local}@{domain}"


def mask_token(value: Any, show_sensitive: bool = False) -> str:
    s = str(value or "")
    if show_sensitive or len(s) <= 8:
        return s
    return f"{s[:4]}...{s[-4:]}"


def summarize_body(data: Any, show_sensitive: bool) -> str:
    if isinstance(data, list):
        return f"array[{len(data)}]"
    if isinstance(data, dict):
        safe = {}
        for key, value in data.items():
            if key in {"address", "from_addr"} and isinstance(value, str):
                safe[key] = mask_email(value, show_sensitive)
            elif key in {"forward_to", "recipients", "to_addrs"} and isinstance(value, str):
                safe[key] = ",".join(mask_email(x.strip(), show_sensitive) for x in value.split(",") if x.strip())
            elif key in {"resend_id"}:
                safe[key] = mask_token(value, show_sensitive)
            else:
                safe[key] = value
        return json.dumps(safe, ensure_ascii=False)
    return str(data)[:200]


def is_json_list(result: HttpResult) -> bool:
    return result.status == 200 and isinstance(result.json_data, list)


def is_json_object(result: HttpResult) -> bool:
    return result.status == 200 and isinstance(result.json_data, dict)


def add_finding(findings: list[Finding], finding: Finding) -> None:
    findings.append(finding)


def login(client: FreemailClient, username: str, password: str) -> tuple[Finding, dict[str, Any]]:
    result = client.request("POST", "/api/login", json_body={"username": username, "password": password})
    payload = client.jwt_payload()
    evidence = [f"POST /api/login -> HTTP {result.status}"]
    if result.json_data is not None:
        evidence.append(f"响应={summarize_body(result.json_data, show_sensitive=False)}")
    if payload:
        evidence.append(f"JWT 载荷 role={payload.get('role')} userId={payload.get('userId')}")
    status = "OK" if result.status == 200 and payload else "FAILED"
    return (
        Finding(
            "AUTH-BASELINE",
            "登录基线检查",
            "info",
            status,
            evidence,
            "如果这里失败，请先确认目标 URL 和普通用户凭据，再解读后续检查结果。",
        ),
        payload,
    )


def check_session(client: FreemailClient) -> Finding:
    result = client.request("GET", "/api/session")
    evidence = [f"GET /api/session -> HTTP {result.status}"]
    if result.json_data is not None:
        evidence.append(f"响应={summarize_body(result.json_data, show_sensitive=False)}")
    return Finding("AUTH-SESSION", "会话接口基线检查", "info", "OK" if result.status == 200 else "FAILED", evidence)


def check_own_mailboxes(client: FreemailClient, show_sensitive: bool) -> tuple[Finding, set[str]]:
    result = client.request("GET", "/api/mailboxes", params={"limit": 100})
    owned: set[str] = set()
    evidence = [f"GET /api/mailboxes?limit=100 -> HTTP {result.status}"]
    if is_json_object(result):
        items = result.json_data.get("list") or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("address"):
                    owned.add(str(item["address"]).lower())
            evidence.append(f"自有邮箱数={len(owned)} total={result.json_data.get('total')}")
            sample = [mask_email(a, show_sensitive) for a in sorted(owned)[:3]]
            if sample:
                evidence.append(f"自有邮箱样本={sample}")
    return Finding("AUTH-OWNED-MAILBOXES", "当前用户可见邮箱列表", "info", "OK", evidence), owned


def check_user_mailboxes_idor(
    client: FreemailClient,
    current_user_id: int | None,
    user_ids: list[int],
    show_sensitive: bool,
) -> tuple[Finding, set[str]]:
    leaked_addresses: set[str] = set()
    evidence: list[str] = []
    vulnerable = False
    exposed_empty = False

    for uid in user_ids:
        result = client.request("GET", f"/api/users/{uid}/mailboxes")
        label = f"GET /api/users/{uid}/mailboxes -> HTTP {result.status}"
        if result.status == 403:
            evidence.append(f"{label}（已拒绝）")
            continue
        if is_json_list(result):
            rows = result.json_data
            is_self = current_user_id is not None and uid == current_user_id
            if not is_self and rows:
                vulnerable = True
                for row in rows:
                    if isinstance(row, dict) and row.get("address"):
                        leaked_addresses.add(str(row["address"]).lower())
                sample = [
                    mask_email(str(row.get("address", "")), show_sensitive)
                    for row in rows[:5]
                    if isinstance(row, dict) and row.get("address")
                ]
                evidence.append(f"{label} 泄露数量={len(rows)} 样本={sample}")
            elif not is_self:
                exposed_empty = True
                evidence.append(f"{label} 非本人用户返回空数组")
            else:
                evidence.append(f"{label} 当前用户数组[{len(rows)}]")
        else:
            evidence.append(f"{label} 响应体={result.text[:80]!r}")

    if vulnerable:
        status = "VULNERABLE"
    elif exposed_empty:
        status = "EXPOSED"
    else:
        status = "NOT_CONFIRMED"

    return (
        Finding(
            "IDOR-USER-MAILBOXES",
            "普通用户可查询其他用户绑定的邮箱",
            "high",
            status,
            evidence,
            "要求严格管理员权限，或要求路径中的用户 ID 与当前登录用户 ID 一致。",
        ),
        leaked_addresses,
    )


def check_mailbox_info_leak(
    client: FreemailClient,
    addresses: set[str],
    owned_addresses: set[str],
    show_sensitive: bool,
) -> Finding:
    evidence: list[str] = []
    vulnerable = False
    exposed = False

    for address in sorted(addresses)[:20]:
        result = client.request("GET", "/api/mailbox/info", params={"address": address})
        label = f"GET /api/mailbox/info?address={mask_email(address, show_sensitive)} -> HTTP {result.status}"
        if not is_json_object(result):
            evidence.append(f"{label} 响应体={result.text[:80]!r}")
            continue
        is_owned = address.lower() in owned_addresses
        info = result.json_data
        if not is_owned and info.get("id") is not None:
            vulnerable = True
            evidence.append(
                f"{label} id={info.get('id')} can_login={info.get('can_login')} "
                f"forward_to={mask_email(str(info.get('forward_to') or ''), show_sensitive)}"
            )
        elif not is_owned:
            exposed = True
            evidence.append(f"{label} non-owned lookup allowed but mailbox does not exist")

    return Finding(
        "INFO-MAILBOX-CONFIG",
        "邮箱配置接口泄露非本人邮箱元数据",
        "medium",
        "VULNERABLE" if vulnerable else ("EXPOSED" if exposed else "NOT_CONFIRMED"),
        evidence or ["没有可用于探测的泄露邮箱地址。"],
        "只有严格管理员或邮箱所有者才能读取 forward_to、can_login 等邮箱元数据。",
    )


def check_email_list_idor(
    client: FreemailClient,
    addresses: set[str],
    owned_addresses: set[str],
    show_sensitive: bool,
) -> Finding:
    evidence: list[str] = []
    vulnerable = False
    exposed = False

    for address in sorted(addresses)[:20]:
        if address.lower() in owned_addresses:
            continue
        result = client.request("GET", "/api/emails", params={"mailbox": address, "limit": 3})
        label = f"GET /api/emails?mailbox={mask_email(address, show_sensitive)}&limit=3 -> HTTP {result.status}"
        if result.status == 403:
            evidence.append(f"{label} 已拒绝")
            continue
        if is_json_list(result):
            if result.json_data:
                vulnerable = True
                rows = []
                for row in result.json_data[:3]:
                    if not isinstance(row, dict):
                        continue
                    rows.append(
                        {
                            "id": row.get("id"),
                            "sender": mask_email(str(row.get("sender") or ""), show_sensitive),
                            "subject": str(row.get("subject") or "")[:40],
                            "has_code": bool(row.get("verification_code")),
                        }
                    )
                evidence.append(f"{label} 泄露邮件={json.dumps(rows, ensure_ascii=False)}")
            else:
                exposed = True
                evidence.append(f"{label} 返回空数组而不是 403")
        else:
            evidence.append(f"{label} 响应体={result.text[:80]!r}")

    return Finding(
        "IDOR-EMAIL-LIST",
        "普通用户可查询非本人邮箱的邮件列表",
        "high",
        "VULNERABLE" if vulnerable else ("EXPOSED" if exposed else "NOT_CONFIRMED"),
        evidence or ["没有可用于探测的非本人邮箱地址。"],
        "返回邮件前应校验当前用户拥有该邮箱，或当前用户是严格管理员。",
    )


def check_sent_records_idor(
    client: FreemailClient,
    addresses: set[str],
    owned_addresses: set[str],
    show_sensitive: bool,
) -> tuple[Finding, set[int]]:
    evidence: list[str] = []
    vulnerable = False
    exposed = False
    leaked_ids: set[int] = set()

    for address in sorted(addresses)[:20]:
        if address.lower() in owned_addresses:
            continue
        result = client.request("GET", "/api/sent", params={"from": address, "limit": 5})
        label = f"GET /api/sent?from={mask_email(address, show_sensitive)}&limit=5 -> HTTP {result.status}"
        if result.status == 403:
            evidence.append(f"{label} 已拒绝")
            continue
        if is_json_list(result):
            if result.json_data:
                vulnerable = True
                rows = []
                for row in result.json_data[:5]:
                    if not isinstance(row, dict):
                        continue
                    try:
                        leaked_ids.add(int(row.get("id")))
                    except Exception:
                        pass
                    rows.append(
                        {
                            "id": row.get("id"),
                            "resend_id": mask_token(row.get("resend_id"), show_sensitive),
                            "recipients": ",".join(
                                mask_email(x.strip(), show_sensitive)
                                for x in str(row.get("recipients") or "").split(",")
                                if x.strip()
                            ),
                            "subject": str(row.get("subject") or "")[:40],
                            "status": row.get("status"),
                        }
                    )
                evidence.append(f"{label} 泄露发件记录={json.dumps(rows, ensure_ascii=False)}")
            else:
                exposed = True
                evidence.append(f"{label} 返回空数组而不是 403")
        else:
            evidence.append(f"{label} 响应体={result.text[:80]!r}")

    return (
        Finding(
            "IDOR-SENT-RECORDS",
            "普通用户可查询非本人邮箱的发件记录",
            "high",
            "VULNERABLE" if vulnerable else ("EXPOSED" if exposed else "NOT_CONFIRMED"),
            evidence or ["没有可用于探测的非本人邮箱地址。"],
            "应按当前登录用户的邮箱归属过滤 sent_emails，并拒绝非本人 from_addr、resend_id、id 请求。",
        ),
        leaked_ids,
    )


def check_users_list_baseline(client: FreemailClient) -> Finding:
    result = client.request("GET", "/api/users")
    return Finding(
        "AUTH-USERS-LIST",
        "用户列表接口基线检查",
        "info",
        "OK" if result.status == 403 else "UNEXPECTED",
        [f"GET /api/users -> HTTP {result.status}"],
        "该接口应继续只允许严格管理员访问。",
    )


def check_root_override(client: FreemailClient, jwt_token: str | None) -> Finding:
    if not jwt_token:
        return Finding(
            "AUTH-ROOT-HEADER",
            "JWT 签名密钥可作为 root 管理员请求头令牌",
            "high",
            "SKIPPED",
            ["传入 --jwt-token 后可执行该非破坏性在线检查。"],
            "移除 Authorization/X-Admin-Token 与 JWT_TOKEN 的直接比较；如需 API Token，请使用单独的低权限密钥。",
        )
    result = client.request("GET", "/api/session", extra_headers={"Authorization": f"Bearer {jwt_token}"})
    evidence = [f"GET /api/session with Authorization: Bearer <JWT_TOKEN> -> HTTP {result.status}"]
    if result.json_data is not None:
        evidence.append(f"响应={summarize_body(result.json_data, show_sensitive=False)}")
    vulnerable = is_json_object(result) and result.json_data.get("role") == "admin"
    return Finding(
        "AUTH-ROOT-HEADER",
        "JWT 签名密钥可作为 root 管理员请求头令牌",
        "high",
        "VULNERABLE" if vulnerable else "NOT_CONFIRMED",
        evidence,
        "移除 Authorization/X-Admin-Token 与 JWT_TOKEN 的直接比较；如需 API Token，请使用单独的低权限密钥。",
    )


def check_security_headers(client: FreemailClient) -> Finding:
    result = client.request("GET", "/")
    required = [
        "content-security-policy",
        "strict-transport-security",
        "x-content-type-options",
        "referrer-policy",
        "permissions-policy",
    ]
    lower_headers = {k.lower(): v for k, v in result.headers.items()}
    missing = [h for h in required if h not in lower_headers]
    evidence = [f"GET / -> HTTP {result.status}", f"缺失响应头={missing}"]
    return Finding(
        "HARDENING-HEADERS",
        "缺少常见浏览器安全响应头",
        "low",
        "WEAK" if missing else "OK",
        evidence,
        "在 Worker 响应层添加 CSP、HSTS、X-Content-Type-Options、Referrer-Policy 和 Permissions-Policy。",
    )


def read_file(source_dir: Path, rel: str) -> str:
    path = source_dir / rel
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def static_checks(source_dir: Path) -> list[Finding]:
    findings: list[Finding] = []
    auth_js = read_file(source_dir, "src/middleware/auth.js")
    app_js = read_file(source_dir, "src/middleware/app.js")
    users_js = read_file(source_dir, "src/api/users.js")
    send_js = read_file(source_dir, "src/api/send.js")
    emails_js = read_file(source_dir, "src/api/emails.js")
    detail_js = read_file(source_dir, "public/js/modules/mailbox/email-detail.js")

    findings.append(
        Finding(
            "STATIC-ROOT-HEADER",
            "源码包含 JWT_TOKEN 请求头 root 覆盖逻辑",
            "high",
            "PRESENT" if "X-Admin-Token" in auth_js and "bearer === JWT_TOKEN" in auth_js else "NOT_FOUND",
            ["src/middleware/auth.js 会将 Authorization/X-Admin-Token 与 JWT_TOKEN 直接比较。"],
            "不要复用 JWT 签名密钥作为 Bearer 管理员令牌。",
        )
    )
    findings.append(
        Finding(
            "STATIC-SHA256-PASSWORDS",
            "源码使用无盐 SHA-256 保存密码哈希",
            "medium",
            "PRESENT" if "sha256Hex" in auth_js else "NOT_FOUND",
            ["src/middleware/auth.js 通过 sha256Hex(rawPassword) 校验密码。"],
            "改用 Argon2id、bcrypt 或 PBKDF2 等带盐 KDF，并为旧哈希设计迁移路径。",
        )
    )
    findings.append(
        Finding(
            "STATIC-MEMORY-RATELIMIT",
            "登录限速使用单实例内存",
            "medium",
            "PRESENT" if "const store = new Map()" in app_js and "rateLimiter" in app_js else "NOT_FOUND",
            ["src/middleware/app.js 将计数器存放在本地 Map 中。"],
            "使用 Cloudflare Turnstile、Durable Object、KV/D1 计数器或 WAF 限速。",
        )
    )
    has_user_mailboxes_route = all(
        marker in users_js
        for marker in [
            "path.startsWith('/api/users/')",
            "path.endsWith('/mailboxes')",
            "getUserMailboxes(db, id)",
        ]
    )
    findings.append(
        Finding(
            "STATIC-USERS-MAILBOXES-IDOR",
            "源码中 /api/users/:id/mailboxes 缺少本地鉴权检查",
            "high",
            "PRESENT" if has_user_mailboxes_route else "NOT_FOUND",
            ["src/api/users.js 直接用 getUserMailboxes(db, id) 处理 GET /api/users/:id/mailboxes。"],
            "要求严格管理员权限，或要求 id === authPayload.userId。",
        )
    )
    sent_patterns = [
        "path === '/api/sent'",
        "path.startsWith('/api/sent/')",
        "path.startsWith('/api/send/')",
    ]
    findings.append(
        Finding(
            "STATIC-SENT-IDOR",
            "源码中发件记录 API 缺少归属校验",
            "high",
            "PRESENT" if all(p in send_js for p in sent_patterns) and "checkSendPermission" in send_js else "REVIEW",
            ["src/api/send.js 只在 POST /api/send 检查发信权限，读取/更新/删除路径仍基于 id/from 查询。"],
            "读取、更新、删除和取消前，应将 sent_emails 与当前用户拥有的邮箱或用户上下文关联校验。",
        )
    )
    findings.append(
        Finding(
            "STATIC-EMAIL-IDOR",
            "源码中邮件 API 依赖调用者传入的 mailbox/id",
            "high",
            "PRESENT" if "url.searchParams.get('mailbox')" in emails_js and "DELETE FROM messages WHERE id = ?" in emails_js else "REVIEW",
            ["src/api/emails.js 会按 mailbox 或 id 返回/删除邮件，普通用户归属校验没有集中覆盖。"],
            "为所有邮件端点和方法集中接入 requireMailboxAccess/requireMessageAccess。",
        )
    )
    findings.append(
        Finding(
            "STATIC-XSS-SANITIZER",
            "客户端邮件 HTML 净化逻辑不完整",
            "medium",
            "PRESENT" if "dangerousAttrs" in detail_js and "onerror" in detail_js else "NOT_FOUND",
            ["public/js/modules/mailbox/email-detail.js 只移除少量固定事件属性。"],
            "使用 DOMPurify 严格白名单，或在不允许 allow-same-origin 的 sandbox iframe 中渲染 HTML。",
        )
    )
    return findings


def print_findings(findings: list[Finding]) -> None:
    print("\n=== Freemail 漏洞验证报告 ===\n")
    for finding in findings:
        print(f"[{finding.status}] {finding.check_id} ({finding.severity})")
        print(f"  {finding.title}")
        for evidence in finding.evidence:
            print(f"  - {evidence}")
        if finding.recommendation:
            print(f"  修复建议：{finding.recommendation}")
        print()

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.status] = counts.get(finding.status, 0) + 1
    print("汇总：", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def save_json(path: Path, findings: list[Finding]) -> None:
    data = [
        {
            "check_id": f.check_id,
            "title": f.title,
            "severity": f.severity,
            "status": f.status,
            "evidence": f.evidence,
            "recommendation": f.recommendation,
        }
        for f in findings
    ]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freemail IDOR/信息泄露问题的只读验证脚本。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            示例：
              python security/verify_freemail_vulns.py --base-url https://mail.123741.xyz --username test --password 12345678
              python security/verify_freemail_vulns.py --base-url https://mail.123741.xyz --username test --password 12345678 --probe-user-ids 1-20
              python security/verify_freemail_vulns.py --base-url https://mail.123741.xyz --username test --password 12345678 --source-dir .
            """
        ),
    )
    parser.add_argument("--base-url", required=True, help="目标站点根地址，例如 https://mail.123741.xyz")
    parser.add_argument("--username", default="test", help="普通用户用户名")
    parser.add_argument("--password", default="12345678", help="普通用户密码")
    parser.add_argument("--probe-user-ids", default=DEFAULT_USER_IDS, help="要探测的用户 ID，例如 1-5,8,10")
    parser.add_argument("--mailbox", action="append", default=[], help="额外指定要探测的邮箱地址")
    parser.add_argument("--jwt-token", default="", help="可选 JWT_TOKEN，用于非破坏性验证 root 请求头问题")
    parser.add_argument("--source-dir", default="", help="可选 Freemail 源码目录，用于静态检查")
    parser.add_argument("--json-out", default="", help="可选 JSON 报告输出路径")
    parser.add_argument("--show-sensitive", action="store_true", help="输出时不脱敏邮箱和令牌")
    parser.add_argument("--insecure", action="store_true", help="禁用 TLS 证书校验")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    findings: list[Finding] = []

    client = FreemailClient(args.base_url, insecure=args.insecure)
    login_finding, payload = login(client, args.username, args.password)
    findings.append(login_finding)
    if login_finding.status != "OK":
        print_findings(findings)
        return 2

    current_user_id = payload.get("userId")
    current_user_id = int(current_user_id) if isinstance(current_user_id, int) or str(current_user_id).isdigit() else None

    findings.append(check_session(client))
    own_finding, owned_addresses = check_own_mailboxes(client, args.show_sensitive)
    findings.append(own_finding)
    findings.append(check_users_list_baseline(client))

    user_ids = parse_user_ids(args.probe_user_ids)
    user_mailboxes_finding, leaked_addresses = check_user_mailboxes_idor(
        client, current_user_id, user_ids, args.show_sensitive
    )
    findings.append(user_mailboxes_finding)

    probe_addresses = {a.lower() for a in leaked_addresses}
    probe_addresses.update(a.strip().lower() for a in args.mailbox if a.strip())
    if not probe_addresses:
        probe_addresses.update(owned_addresses)

    findings.append(check_mailbox_info_leak(client, probe_addresses, owned_addresses, args.show_sensitive))
    findings.append(check_email_list_idor(client, probe_addresses, owned_addresses, args.show_sensitive))
    sent_finding, _ = check_sent_records_idor(client, probe_addresses, owned_addresses, args.show_sensitive)
    findings.append(sent_finding)
    findings.append(check_root_override(client, args.jwt_token or None))
    findings.append(check_security_headers(client))

    if args.source_dir:
        findings.extend(static_checks(Path(args.source_dir)))

    print_findings(findings)
    if args.json_out:
        save_json(Path(args.json_out), findings)
        print(f"\nJSON 报告已写入 {args.json_out}")

    if any(f.status == "VULNERABLE" for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
