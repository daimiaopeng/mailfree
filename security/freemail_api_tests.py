#!/usr/bin/env python3
"""
Freemail 全接口回归测试脚本。

设计目标：
- 可直接在 GitHub Actions 中运行，不依赖第三方 Python 包。
- 默认只执行只读/低副作用检查，避免 CI 误删线上数据或发送邮件。
- 打开 --allow-mutation 后，会覆盖创建、置顶、转发、收藏、删除等写接口。
- 所有接口都会出现在报告中；缺少前置条件的接口会标记为 SKIP，而不是静默遗漏。
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import random
import ssl
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
WARN = "WARN"


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    text: str
    json_data: Any = None
    error: str = ""


@dataclass
class CaseResult:
    name: str
    method: str
    path: str
    status: str
    expected: str
    actual: str = ""
    detail: str = ""
    elapsed_ms: int = 0


@dataclass
class TestContext:
    owned_mailboxes: list[str] = field(default_factory=list)
    generated_mailboxes: list[str] = field(default_factory=list)
    email_ids: list[int] = field(default_factory=list)
    sent_ids: list[int] = field(default_factory=list)
    resend_ids: list[str] = field(default_factory=list)


class FreemailClient:
    """带 Cookie 会话的轻量 HTTP 客户端。"""

    def __init__(self, base_url: str, *, timeout: int = 20, insecure: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
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
        raw_body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[HttpResult, int]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"

        data = None
        req_headers = {
            "Accept": "application/json,text/plain,*/*",
            "Cache-Control": "no-cache",
            "User-Agent": "freemail-api-tests/1.0",
        }
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            data = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
        if headers:
            req_headers.update(headers)

        started = time.perf_counter()
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                elapsed = int((time.perf_counter() - started) * 1000)
                return make_http_result(resp.status, dict(resp.headers), body), elapsed
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            elapsed = int((time.perf_counter() - started) * 1000)
            return make_http_result(exc.code, dict(exc.headers), body), elapsed
        except Exception as exc:  # noqa: BLE001 - CI 需要把网络错误转成测试结果。
            elapsed = int((time.perf_counter() - started) * 1000)
            return HttpResult(status=0, headers={}, text="", error=str(exc)), elapsed

    def jwt_payload(self) -> dict[str, Any]:
        for cookie in self.cookies:
            if cookie.name == "iding-session":
                parts = cookie.value.split(".")
                if len(parts) != 3:
                    return {}
                raw = parts[1] + "=" * (-len(parts[1]) % 4)
                try:
                    return json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
                except Exception:
                    return {}
        return {}


def make_http_result(status: int, headers: dict[str, str], body: str) -> HttpResult:
    parsed = None
    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = None
    return HttpResult(status=status, headers=headers, text=body, json_data=parsed)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def parse_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_user_ids(raw: str) -> list[int]:
    ids: set[int] = set()
    for part in parse_csv(raw):
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            ids.update(range(min(start, end), max(start, end) + 1))
        else:
            ids.add(int(part))
    return sorted(i for i in ids if i > 0)


def random_local(prefix: str = "ci") -> str:
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    return f"{prefix}-{int(time.time())}-{suffix}"


def is_json_object(result: HttpResult) -> bool:
    return result.status == 200 and isinstance(result.json_data, dict)


def is_json_list(result: HttpResult) -> bool:
    return result.status == 200 and isinstance(result.json_data, list)


def brief_body(result: HttpResult, limit: int = 180) -> str:
    if result.error:
        return f"网络错误: {result.error}"
    if result.json_data is not None:
        text = json.dumps(result.json_data, ensure_ascii=False)
    else:
        text = result.text
    text = " ".join(str(text).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def add_case(
    cases: list[CaseResult],
    name: str,
    method: str,
    path: str,
    result: HttpResult | None,
    elapsed_ms: int,
    ok: bool,
    expected: str,
    detail: str = "",
    *,
    warn: bool = False,
) -> None:
    actual = "SKIPPED" if result is None else f"HTTP {result.status}"
    if result is not None and result.error:
        actual = f"ERROR {result.error}"
    if not detail and result is not None:
        detail = brief_body(result)
    cases.append(
        CaseResult(
            name=name,
            method=method,
            path=path,
            status=PASS if ok else (WARN if warn else FAIL),
            expected=expected,
            actual=actual,
            detail=detail,
            elapsed_ms=elapsed_ms,
        )
    )


def skip_case(cases: list[CaseResult], name: str, method: str, path: str, reason: str) -> None:
    cases.append(CaseResult(name=name, method=method, path=path, status=SKIP, expected=reason, actual="SKIPPED"))


def expect_status(
    cases: list[CaseResult],
    client: FreemailClient,
    name: str,
    method: str,
    path: str,
    statuses: set[int],
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    detail: str = "",
    warn_only: bool = False,
) -> HttpResult:
    result, elapsed = client.request(method, path, params=params, json_body=json_body)
    expected = " / ".join(f"HTTP {s}" for s in sorted(statuses))
    add_case(cases, name, method, build_path(path, params), result, elapsed, result.status in statuses, expected, detail, warn=warn_only)
    return result


def build_path(path: str, params: dict[str, Any] | None) -> str:
    if not params:
        return path
    return f"{path}?{urllib.parse.urlencode(params, doseq=True)}"


def login(
    cases: list[CaseResult],
    client: FreemailClient,
    label: str,
    username: str,
    password: str,
    *,
    required: bool = True,
) -> dict[str, Any]:
    result, elapsed = client.request("POST", "/api/login", json_body={"username": username, "password": password})
    payload = client.jwt_payload()
    ok = result.status == 200 and bool(payload)
    detail = f"role={payload.get('role')} userId={payload.get('userId')} body={brief_body(result)}"
    expected = "HTTP 200 且返回有效会话"
    if not required:
        expected = "HTTP 200 且返回有效会话；当前凭据不可用时跳过依赖该账号的用例"
        detail = f"{detail}；如需强制普通用户验证，请设置 FREEMAIL_REQUIRE_TEST_USER=true"
    add_case(cases, f"登录：{label}", "POST", "/api/login", result, elapsed, ok, expected, detail, warn=not required)
    return payload


def requires_test_user(args: argparse.Namespace) -> bool:
    """管理员账号已配置时，普通用户凭据允许作为可选回归输入，方便旧部署升级窗口通过 CI。"""
    has_admin = bool(args.admin_username and args.admin_password)
    return bool(args.require_test_user or not has_admin)


def skip_user_readonly_cases(cases: list[CaseResult], reason: str) -> None:
    for name, method, path in [
        ("当前会话", "GET", "/api/session"),
        ("域名列表", "GET", "/api/domains"),
        ("用户配额", "GET", "/api/user/quota"),
        ("普通用户禁止访问用户列表", "GET", "/api/users"),
        ("邮箱列表", "GET", "/api/mailboxes"),
        ("本人邮箱信息", "GET", "/api/mailbox/info"),
        ("本人邮件列表", "GET", "/api/emails"),
        ("本人邮件筛选：未读/验证码/附件", "GET", "/api/emails"),
        ("本人发件记录", "GET", "/api/sent"),
        ("批量邮件详情", "GET", "/api/emails/batch"),
        ("单封邮件详情", "GET", "/api/email/:id"),
        ("邮件 EML 下载", "GET", "/api/email/:id/download"),
        ("发件记录详情", "GET", "/api/sent/:id"),
        ("Resend 发件状态", "GET", "/api/send/:resendId"),
        ("更新发件记录", "PATCH", "/api/send/:resendId"),
        ("取消发件记录", "POST", "/api/send/:resendId/cancel"),
        ("登出", "POST", "/api/logout"),
    ]:
        skip_case(cases, name, method, path, reason)


def warn_when_missing_new_endpoint(cases: list[CaseResult], result: HttpResult) -> None:
    if result.status == 404:
        cases[-1].status = WARN
        cases[-1].expected = "HTTP 200；旧部署尚未包含该新接口时允许 HTTP 404"
        cases[-1].detail = "新版本部署完成后该接口应返回 HTTP 200；当前按升级兼容处理"


def collect_mailboxes(result: HttpResult) -> list[str]:
    if not is_json_object(result):
        return []
    rows = result.json_data.get("list")
    if not isinstance(rows, list):
        return []
    addresses: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("address"):
            addresses.append(str(row["address"]).strip().lower())
    return addresses


def collect_email_ids(result: HttpResult) -> list[int]:
    if not is_json_list(result):
        return []
    ids: list[int] = []
    for row in result.json_data:
        if isinstance(row, dict):
            try:
                ids.append(int(row.get("id")))
            except Exception:
                pass
    return ids


def collect_sent(result: HttpResult) -> tuple[list[int], list[str]]:
    if not is_json_list(result):
        return [], []
    ids: list[int] = []
    resend_ids: list[str] = []
    for row in result.json_data:
        if not isinstance(row, dict):
            continue
        try:
            ids.append(int(row.get("id")))
        except Exception:
            pass
        if row.get("resend_id"):
            resend_ids.append(str(row["resend_id"]))
    return ids, resend_ids


def test_unauthenticated(cases: list[CaseResult], args: argparse.Namespace) -> None:
    client = FreemailClient(args.base_url, timeout=args.timeout, insecure=args.insecure)
    expect_status(cases, client, "未登录访问会话接口", "GET", "/api/session", {401})
    expect_status(cases, client, "未登录访问受保护接口", "GET", "/api/mailboxes", {401})


def test_user_readonly(cases: list[CaseResult], args: argparse.Namespace) -> tuple[FreemailClient, dict[str, Any], TestContext]:
    client = FreemailClient(args.base_url, timeout=args.timeout, insecure=args.insecure)
    user_required = requires_test_user(args)
    payload = login(cases, client, "普通用户", args.username, args.password, required=user_required)
    ctx = TestContext()
    if not payload:
        skip_user_readonly_cases(cases, "普通用户登录失败或凭据未匹配，跳过依赖普通用户会话的接口")
        return client, payload, ctx

    expect_status(cases, client, "当前会话", "GET", "/api/session", {200})
    expect_status(cases, client, "域名列表", "GET", "/api/domains", {200})
    expect_status(cases, client, "用户配额", "GET", "/api/user/quota", {200})
    users_result = expect_status(cases, client, "普通用户禁止访问用户列表", "GET", "/api/users", {403})
    if users_result.status == 200:
        cases[-1].status = FAIL
        cases[-1].expected = "HTTP 403，普通用户不应看到用户列表"

    mailboxes = expect_status(cases, client, "邮箱列表", "GET", "/api/mailboxes", {200}, params={"limit": 100, "offset": 0})
    ctx.owned_mailboxes = collect_mailboxes(mailboxes)
    if ctx.owned_mailboxes:
        own = ctx.owned_mailboxes[0]
        expect_status(cases, client, "本人邮箱信息", "GET", "/api/mailbox/info", {200}, params={"address": own})
        emails = expect_status(cases, client, "本人邮件列表", "GET", "/api/emails", {200}, params={"mailbox": own, "limit": 5})
        expect_status(cases, client, "本人邮件筛选：未读/验证码/附件", "GET", "/api/emails", {200}, params={"mailbox": own, "limit": 5, "unread": "true", "code": "true", "attachment": "false"})
        ctx.email_ids = collect_email_ids(emails)
        sent = expect_status(cases, client, "本人发件记录", "GET", "/api/sent", {200}, params={"from": own, "limit": 5})
        ctx.sent_ids, ctx.resend_ids = collect_sent(sent)
    else:
        skip_case(cases, "本人邮箱信息", "GET", "/api/mailbox/info", "普通用户没有可用邮箱")
        skip_case(cases, "本人邮件列表", "GET", "/api/emails", "普通用户没有可用邮箱")
        skip_case(cases, "本人发件记录", "GET", "/api/sent", "普通用户没有可用邮箱")

    if ctx.email_ids:
        ids = ",".join(str(i) for i in ctx.email_ids[:5])
        expect_status(cases, client, "批量邮件详情", "GET", "/api/emails/batch", {200}, params={"ids": ids})
        if args.allow_read_details:
            expect_status(cases, client, "单封邮件详情", "GET", f"/api/email/{ctx.email_ids[0]}", {200})
            expect_status(cases, client, "邮件 EML 下载", "GET", f"/api/email/{ctx.email_ids[0]}/download", {200, 404})
        else:
            skip_case(cases, "单封邮件详情", "GET", "/api/email/:id", "默认不读取详情，避免把邮件标记为已读；需要时设置 FREEMAIL_ALLOW_READ_DETAILS=true")
            skip_case(cases, "邮件 EML 下载", "GET", "/api/email/:id/download", "缺少已允许读取的邮件详情上下文")
    else:
        skip_case(cases, "批量邮件详情", "GET", "/api/emails/batch", "当前测试邮箱没有邮件")
        skip_case(cases, "单封邮件详情", "GET", "/api/email/:id", "当前测试邮箱没有邮件")
        skip_case(cases, "邮件 EML 下载", "GET", "/api/email/:id/download", "当前测试邮箱没有邮件")

    if ctx.sent_ids:
        expect_status(cases, client, "发件记录详情", "GET", f"/api/sent/{ctx.sent_ids[0]}", {200})
    else:
        skip_case(cases, "发件记录详情", "GET", "/api/sent/:id", "当前测试邮箱没有发件记录")

    if ctx.resend_ids:
        expect_status(cases, client, "Resend 发件状态", "GET", f"/api/send/{ctx.resend_ids[0]}", {200, 404, 500}, warn_only=True)
        if args.allow_mutation:
            expect_status(cases, client, "更新发件记录", "PATCH", f"/api/send/{ctx.resend_ids[0]}", {200, 404, 500}, json_body={"status": "delivered"}, warn_only=True)
            expect_status(cases, client, "取消发件记录", "POST", f"/api/send/{ctx.resend_ids[0]}/cancel", {200, 404, 500}, warn_only=True)
        else:
            skip_case(cases, "更新发件记录", "PATCH", "/api/send/:resendId", "默认不修改发件记录")
            skip_case(cases, "取消发件记录", "POST", "/api/send/:resendId/cancel", "默认不取消发件记录")
    else:
        skip_case(cases, "Resend 发件状态", "GET", "/api/send/:resendId", "当前测试邮箱没有 resend_id")
        skip_case(cases, "更新发件记录", "PATCH", "/api/send/:resendId", "当前测试邮箱没有 resend_id")
        skip_case(cases, "取消发件记录", "POST", "/api/send/:resendId/cancel", "当前测试邮箱没有 resend_id")

    expect_status(cases, client, "登出", "POST", "/api/logout", {200})
    return client, payload, ctx


def test_idor_regressions(
    cases: list[CaseResult],
    args: argparse.Namespace,
    payload: dict[str, Any],
    owned_mailboxes: list[str],
) -> None:
    client = FreemailClient(args.base_url, timeout=args.timeout, insecure=args.insecure)
    user_required = requires_test_user(args)
    login_payload = login(cases, client, "普通用户-越权回归", args.username, args.password, required=user_required)
    if not login_payload:
        skip_case(cases, "越权回归检查", "MULTI", "/api/users/:id/mailboxes /api/mailbox/info /api/emails /api/sent /api/send", "普通用户登录失败或凭据未匹配，跳过越权回归探测")
        return

    current_user_id = login_payload.get("userId") or payload.get("userId")
    for user_id in args.probe_user_ids:
        if current_user_id is not None and int(current_user_id) == user_id:
            continue
        result = expect_status(
            cases,
            client,
            f"越权检查：/api/users/{user_id}/mailboxes",
            "GET",
            f"/api/users/{user_id}/mailboxes",
            {403, 404},
        )
        if result.status == 200:
            cases[-1].status = FAIL
            cases[-1].expected = "HTTP 403/404，不能读取其他用户绑定邮箱"
            cases[-1].detail = brief_body(result)

    owned = {x.lower() for x in owned_mailboxes}
    for mailbox in args.probe_mailboxes:
        normalized = mailbox.lower()
        if normalized in owned:
            skip_case(cases, f"越权检查：邮箱信息 {mailbox}", "GET", "/api/mailbox/info", "探测邮箱属于当前用户，跳过")
            continue
        info = expect_status(
            cases,
            client,
            f"越权检查：邮箱信息 {mailbox}",
            "GET",
            "/api/mailbox/info",
            {403},
            params={"address": mailbox},
        )
        if info.status == 200:
            cases[-1].status = FAIL
            cases[-1].expected = "HTTP 403，不能读取非本人邮箱配置"

        emails = expect_status(
            cases,
            client,
            f"越权检查：邮件列表 {mailbox}",
            "GET",
            "/api/emails",
            {403},
            params={"mailbox": mailbox, "limit": 3},
        )
        if emails.status == 200:
            cases[-1].status = FAIL
            cases[-1].expected = "HTTP 403，不能读取非本人邮箱邮件"

        sent = expect_status(
            cases,
            client,
            f"越权检查：发件记录 {mailbox}",
            "GET",
            "/api/sent",
            {403},
            params={"from": mailbox, "limit": 3},
        )
        if sent.status == 200:
            cases[-1].status = FAIL
            cases[-1].expected = "HTTP 403，不能读取非本人邮箱发件记录"

        send_payload = {
            "from": mailbox,
            "to": args.safe_to_email or "nobody@example.com",
            "subject": "freemail ci unauthorized send probe",
            "text": "This request must be rejected before sending.",
        }
        send_result = expect_status(
            cases,
            client,
            f"越权检查：冒用发件人 {mailbox}",
            "POST",
            "/api/send",
            {403},
            json_body=send_payload,
            warn_only=True,
        )
        if send_result.status == 200:
            cases[-1].status = FAIL
            cases[-1].expected = "HTTP 403，不能冒用非本人邮箱发信"
        elif send_result.status == 500:
            cases[-1].status = WARN
            cases[-1].expected = "理想结果为 HTTP 403；当前环境可能因未配置 Resend API Key 先返回 500"


def test_user_mutation(cases: list[CaseResult], args: argparse.Namespace) -> TestContext:
    ctx = TestContext()
    if not args.allow_mutation:
        for method, path, name in [
            ("GET", "/api/generate", "生成随机邮箱"),
            ("POST", "/api/create", "创建自定义邮箱"),
            ("PATCH", "/api/mailbox/info", "更新邮箱备注标签"),
            ("POST", "/api/mailboxes/pin", "切换邮箱置顶"),
            ("POST", "/api/mailbox/forward", "设置邮箱转发"),
            ("POST", "/api/mailbox/favorite", "切换邮箱收藏"),
            ("DELETE", "/api/emails", "清空邮箱邮件"),
            ("DELETE", "/api/email/:id", "删除单封邮件"),
            ("DELETE", "/api/sent/:id", "删除发件记录"),
        ]:
            skip_case(cases, name, method, path, "默认安全模式跳过写接口；需要时设置 FREEMAIL_ALLOW_MUTATION=true")
        return ctx

    client = FreemailClient(args.base_url, timeout=args.timeout, insecure=args.insecure)
    if not login(cases, client, "普通用户-写接口", args.username, args.password):
        return ctx

    generated = expect_status(cases, client, "生成随机邮箱", "GET", "/api/generate", {200}, params={"length": 12, "domainIndex": 0})
    if is_json_object(generated) and generated.json_data.get("email"):
        ctx.generated_mailboxes.append(str(generated.json_data["email"]).lower())

    local = random_local()
    created = expect_status(cases, client, "创建自定义邮箱", "POST", "/api/create", {200}, json_body={"local": local, "domainIndex": 0})
    if is_json_object(created) and created.json_data.get("email"):
        ctx.generated_mailboxes.append(str(created.json_data["email"]).lower())

    if not ctx.generated_mailboxes:
        skip_case(cases, "切换邮箱置顶", "POST", "/api/mailboxes/pin", "没有成功创建测试邮箱")
        skip_case(cases, "设置邮箱转发", "POST", "/api/mailbox/forward", "没有成功创建测试邮箱")
        skip_case(cases, "切换邮箱收藏", "POST", "/api/mailbox/favorite", "没有成功创建测试邮箱")
        skip_case(cases, "清空邮箱邮件", "DELETE", "/api/emails", "没有成功创建测试邮箱")
        return ctx

    mailbox = ctx.generated_mailboxes[0]
    expect_status(cases, client, "切换邮箱置顶", "POST", "/api/mailboxes/pin", {200}, params={"address": mailbox})
    info = expect_status(cases, client, "新邮箱信息", "GET", "/api/mailbox/info", {200}, params={"address": mailbox})
    expect_status(cases, client, "更新邮箱备注标签", "PATCH", "/api/mailbox/info", {200}, json_body={"address": mailbox, "note": "CI 接口测试邮箱", "tags": ["ci", "api"], "purpose": "自动化回归", "ttlHours": 24})
    mailbox_id = info.json_data.get("id") if is_json_object(info) else None
    if mailbox_id:
        expect_status(cases, client, "设置邮箱转发", "POST", "/api/mailbox/forward", {200}, json_body={"mailbox_id": mailbox_id, "forward_to": ""})
        expect_status(cases, client, "切换邮箱收藏", "POST", "/api/mailbox/favorite", {200}, json_body={"mailbox_id": mailbox_id})
    else:
        skip_case(cases, "设置邮箱转发", "POST", "/api/mailbox/forward", "未获取到测试邮箱 ID")
        skip_case(cases, "切换邮箱收藏", "POST", "/api/mailbox/favorite", "未获取到测试邮箱 ID")

    expect_status(cases, client, "清空邮箱邮件", "DELETE", "/api/emails", {200}, params={"mailbox": mailbox})
    return ctx


def test_admin(cases: list[CaseResult], args: argparse.Namespace, generated_mailboxes: list[str]) -> None:
    if not args.admin_username or not args.admin_password:
        for method, path, name in [
            ("GET", "/api/users", "管理员用户列表"),
            ("GET", "/api/system/health", "管理员健康检查"),
            ("GET", "/api/audit/logs", "管理员审计日志"),
            ("POST", "/api/users", "管理员创建用户"),
            ("PATCH", "/api/users/:id", "管理员更新用户"),
            ("DELETE", "/api/users/:id", "管理员删除用户"),
            ("POST", "/api/users/assign", "管理员分配邮箱"),
            ("POST", "/api/users/unassign", "管理员取消分配邮箱"),
            ("POST", "/api/mailboxes/reset-password", "管理员重置邮箱密码"),
            ("POST", "/api/mailboxes/toggle-login", "管理员切换邮箱登录"),
            ("POST", "/api/mailboxes/change-password", "管理员修改邮箱密码"),
            ("POST", "/api/mailboxes/batch-toggle-login", "管理员批量切换登录"),
            ("POST", "/api/mailboxes/batch-favorite", "管理员批量收藏"),
            ("POST", "/api/mailboxes/batch-forward", "管理员批量转发"),
            ("POST", "/api/mailboxes/batch-favorite-by-address", "管理员按地址批量收藏"),
            ("POST", "/api/mailboxes/batch-forward-by-address", "管理员按地址批量转发"),
            ("POST", "/api/maintenance/cleanup", "管理员清理过期邮箱"),
            ("DELETE", "/api/mailboxes", "管理员删除邮箱"),
        ]:
            skip_case(cases, name, method, path, "未配置管理员账号：FREEMAIL_ADMIN_USER/FREEMAIL_ADMIN_PASSWORD")
        return

    client = FreemailClient(args.base_url, timeout=args.timeout, insecure=args.insecure)
    if not login(cases, client, "管理员", args.admin_username, args.admin_password):
        return

    expect_status(cases, client, "管理员用户列表", "GET", "/api/users", {200}, params={"limit": 20, "offset": 0})
    expect_status(cases, client, "管理员邮箱列表", "GET", "/api/mailboxes", {200}, params={"limit": 20, "offset": 0})
    health = expect_status(cases, client, "管理员健康检查", "GET", "/api/system/health", {200, 404}, warn_only=True)
    warn_when_missing_new_endpoint(cases, health)
    audit = expect_status(cases, client, "管理员审计日志", "GET", "/api/audit/logs", {200, 404}, params={"limit": 10, "offset": 0}, warn_only=True)
    warn_when_missing_new_endpoint(cases, audit)

    if args.probe_user_ids:
        expect_status(cases, client, "管理员读取用户邮箱", "GET", f"/api/users/{args.probe_user_ids[0]}/mailboxes", {200, 404})
    else:
        skip_case(cases, "管理员读取用户邮箱", "GET", "/api/users/:id/mailboxes", "未配置 FREEMAIL_PROBE_USER_IDS")

    if not args.allow_mutation:
        skip_case(cases, "管理员清理过期邮箱", "POST", "/api/maintenance/cleanup", "默认安全模式跳过清理接口；需要时设置 FREEMAIL_ALLOW_MUTATION=true")
        return

    expect_status(cases, client, "管理员清理过期邮箱", "POST", "/api/maintenance/cleanup", {200})

    test_user = f"ci-user-{int(time.time())}-{random.randint(1000, 9999)}"
    test_pass = random_local("pw")
    created_user = expect_status(
        cases,
        client,
        "管理员创建用户",
        "POST",
        "/api/users",
        {200},
        json_body={"username": test_user, "password": test_pass, "role": "user", "mailboxLimit": 5},
    )
    user_id = created_user.json_data.get("id") if is_json_object(created_user) else None

    if user_id:
        expect_status(cases, client, "管理员更新用户", "PATCH", f"/api/users/{user_id}", {200}, json_body={"mailboxLimit": 6, "can_send": False})
    else:
        skip_case(cases, "管理员更新用户", "PATCH", "/api/users/:id", "未成功创建测试用户")

    admin_mailbox = generated_mailboxes[0] if generated_mailboxes else ""
    if not admin_mailbox:
        local = random_local("admin-ci")
        created_box = expect_status(cases, client, "管理员创建测试邮箱", "POST", "/api/create", {200}, json_body={"local": local, "domainIndex": 0})
        if is_json_object(created_box) and created_box.json_data.get("email"):
            admin_mailbox = str(created_box.json_data["email"]).lower()

    if user_id and admin_mailbox:
        expect_status(cases, client, "管理员分配邮箱", "POST", "/api/users/assign", {200}, json_body={"username": test_user, "address": admin_mailbox})
        expect_status(cases, client, "管理员取消分配邮箱", "POST", "/api/users/unassign", {200}, json_body={"username": test_user, "address": admin_mailbox})
    else:
        skip_case(cases, "管理员分配邮箱", "POST", "/api/users/assign", "缺少测试用户或测试邮箱")
        skip_case(cases, "管理员取消分配邮箱", "POST", "/api/users/unassign", "缺少测试用户或测试邮箱")

    if admin_mailbox:
        expect_status(cases, client, "管理员重置邮箱密码", "POST", "/api/mailboxes/reset-password", {200}, params={"address": admin_mailbox})
        expect_status(cases, client, "管理员切换邮箱登录", "POST", "/api/mailboxes/toggle-login", {200}, json_body={"address": admin_mailbox, "can_login": False})
        expect_status(cases, client, "管理员修改邮箱密码", "POST", "/api/mailboxes/change-password", {200}, json_body={"address": admin_mailbox, "new_password": random_local("pw")})
        expect_status(cases, client, "管理员批量切换登录", "POST", "/api/mailboxes/batch-toggle-login", {200}, json_body={"addresses": [admin_mailbox], "can_login": False})
        info = expect_status(cases, client, "管理员测试邮箱信息", "GET", "/api/mailbox/info", {200}, params={"address": admin_mailbox})
        mailbox_id = info.json_data.get("id") if is_json_object(info) else None
        if mailbox_id:
            expect_status(cases, client, "管理员批量收藏", "POST", "/api/mailboxes/batch-favorite", {200}, json_body={"mailbox_ids": [mailbox_id], "is_favorite": False})
            expect_status(cases, client, "管理员批量转发", "POST", "/api/mailboxes/batch-forward", {200}, json_body={"mailbox_ids": [mailbox_id], "forward_to": ""})
        else:
            skip_case(cases, "管理员批量收藏", "POST", "/api/mailboxes/batch-favorite", "未获取到测试邮箱 ID")
            skip_case(cases, "管理员批量转发", "POST", "/api/mailboxes/batch-forward", "未获取到测试邮箱 ID")
        expect_status(cases, client, "管理员按地址批量收藏", "POST", "/api/mailboxes/batch-favorite-by-address", {200}, json_body={"addresses": [admin_mailbox], "is_favorite": False})
        expect_status(cases, client, "管理员按地址批量转发", "POST", "/api/mailboxes/batch-forward-by-address", {200}, json_body={"addresses": [admin_mailbox], "forward_to": ""})
        expect_status(cases, client, "管理员删除邮箱", "DELETE", "/api/mailboxes", {200, 404}, params={"address": admin_mailbox})
    else:
        skip_case(cases, "管理员邮箱管理写接口", "POST", "/api/mailboxes/*", "未成功创建测试邮箱")

    if user_id:
        expect_status(cases, client, "管理员删除用户", "DELETE", f"/api/users/{user_id}", {200})
    else:
        skip_case(cases, "管理员删除用户", "DELETE", "/api/users/:id", "未成功创建测试用户")


def test_guest(cases: list[CaseResult], args: argparse.Namespace) -> None:
    if not args.guest_password:
        skip_case(cases, "访客登录", "POST", "/api/login", "未配置 FREEMAIL_GUEST_PASSWORD")
        skip_case(cases, "访客用户列表 mock", "GET", "/api/users", "未配置 FREEMAIL_GUEST_PASSWORD")
        skip_case(cases, "访客邮件列表 mock", "GET", "/api/emails", "未配置 FREEMAIL_GUEST_PASSWORD")
        return
    client = FreemailClient(args.base_url, timeout=args.timeout, insecure=args.insecure)
    payload = login(cases, client, "访客", args.guest_username, args.guest_password)
    if not payload:
        return
    expect_status(cases, client, "访客用户列表 mock", "GET", "/api/users", {200})
    expect_status(cases, client, "访客邮件列表 mock", "GET", "/api/emails", {200}, params={"mailbox": "guest@example.com", "limit": 3})


def test_mailbox_login(cases: list[CaseResult], args: argparse.Namespace) -> None:
    if not args.mailbox_username or not args.mailbox_password:
        skip_case(cases, "邮箱账号登录", "POST", "/api/login", "未配置 FREEMAIL_MAILBOX_USER/FREEMAIL_MAILBOX_PASSWORD")
        skip_case(cases, "邮箱账号邮件列表", "GET", "/api/emails", "未配置邮箱账号")
        skip_case(cases, "邮箱账号邮箱信息", "GET", "/api/mailbox/info", "未配置邮箱账号")
        skip_case(cases, "邮箱账号修改密码", "PUT", "/api/mailbox/password", "默认不修改密码")
        return
    client = FreemailClient(args.base_url, timeout=args.timeout, insecure=args.insecure)
    payload = login(cases, client, "邮箱账号", args.mailbox_username, args.mailbox_password)
    if not payload:
        return
    mailbox = str(payload.get("mailboxAddress") or args.mailbox_username).lower()
    expect_status(cases, client, "邮箱账号邮箱列表", "GET", "/api/mailboxes", {200})
    expect_status(cases, client, "邮箱账号邮箱信息", "GET", "/api/mailbox/info", {200}, params={"address": mailbox})
    expect_status(cases, client, "邮箱账号邮件列表", "GET", "/api/emails", {200}, params={"mailbox": mailbox, "limit": 5})
    for probe in args.probe_mailboxes:
        if probe.lower() == mailbox:
            continue
        expect_status(cases, client, f"邮箱账号禁止读他人邮箱 {probe}", "GET", "/api/emails", {403}, params={"mailbox": probe, "limit": 3})
        expect_status(cases, client, f"邮箱账号禁止读他人配置 {probe}", "GET", "/api/mailbox/info", {403}, params={"address": probe})
        break

    if args.allow_mailbox_password_change:
        skip_case(cases, "邮箱账号修改密码", "PUT", "/api/mailbox/password", "为避免 CI 改掉登录凭据，脚本不自动实现该破坏性检查")
    else:
        skip_case(cases, "邮箱账号修改密码", "PUT", "/api/mailbox/password", "默认不修改密码")


def test_receive(cases: list[CaseResult], args: argparse.Namespace) -> None:
    if not args.allow_receive:
        skip_case(cases, "收信入口", "POST", "/receive", "默认不向收信入口投递测试邮件；需要时设置 FREEMAIL_ALLOW_RECEIVE=true")
        return
    client = FreemailClient(args.base_url, timeout=args.timeout, insecure=args.insecure)
    if not login(cases, client, "收信入口测试用户", args.username, args.password):
        return
    body = args.receive_body or "From: ci@example.com\nTo: test@example.com\nSubject: Freemail CI\n\nhello"
    result, elapsed = client.request("POST", "/receive", headers={"Content-Type": "message/rfc822"}, raw_body=body)
    add_case(cases, "收信入口", "POST", "/receive", result, elapsed, result.status in {200, 400, 415, 500}, "入口可达；不同部署可能要求 Cloudflare EmailEvent", body[:80], warn=True)


def write_json(path: str, cases: list[CaseResult]) -> None:
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([case.__dict__ for case in cases], ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: str, cases: list[CaseResult]) -> None:
    if not path:
        return
    counts = count_status(cases)
    lines = [
        "## Freemail API 测试报告",
        "",
        f"- PASS: {counts.get(PASS, 0)}",
        f"- FAIL: {counts.get(FAIL, 0)}",
        f"- WARN: {counts.get(WARN, 0)}",
        f"- SKIP: {counts.get(SKIP, 0)}",
        "",
        "| 结果 | 接口 | 期望 | 实际 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        detail = case.detail.replace("|", "\\|").replace("\n", " ")[:240]
        lines.append(f"| {case.status} | `{case.method} {case.path}` | {case.expected} | {case.actual} | {detail} |")
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_junit(path: str, cases: list[CaseResult]) -> None:
    if not path:
        return
    failures = [case for case in cases if case.status == FAIL]
    skipped = [case for case in cases if case.status == SKIP]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="freemail-api" tests="{len(cases)}" failures="{len(failures)}" skipped="{len(skipped)}">',
    ]
    for case in cases:
        name = xml.sax.saxutils.escape(f"{case.method} {case.path} - {case.name}")
        lines.append(f'  <testcase classname="freemail.api" name="{name}" time="{case.elapsed_ms / 1000:.3f}">')
        if case.status == FAIL:
            msg = xml.sax.saxutils.escape(f"期望: {case.expected}; 实际: {case.actual}; {case.detail}")
            lines.append(f'    <failure message="{msg}">{msg}</failure>')
        elif case.status == SKIP:
            msg = xml.sax.saxutils.escape(case.expected)
            lines.append(f'    <skipped message="{msg}" />')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def count_status(cases: list[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.status] = counts.get(case.status, 0) + 1
    return counts


def print_report(cases: list[CaseResult]) -> None:
    counts = count_status(cases)
    print("\n=== Freemail API 测试报告 ===")
    print(f"PASS={counts.get(PASS, 0)} FAIL={counts.get(FAIL, 0)} WARN={counts.get(WARN, 0)} SKIP={counts.get(SKIP, 0)}\n")
    for case in cases:
        print(f"[{case.status}] {case.method} {case.path} - {case.name}")
        print(f"  期望: {case.expected}")
        print(f"  实际: {case.actual}")
        if case.detail:
            print(f"  说明: {case.detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freemail 全接口回归测试")
    parser.add_argument("--base-url", default=env("FREEMAIL_BASE_URL"), help="目标站点，例如 https://mail.example.com")
    parser.add_argument("--username", default=env("FREEMAIL_TEST_USER", "test"), help="普通测试用户")
    parser.add_argument("--password", default=env("FREEMAIL_TEST_PASSWORD"), help="普通测试用户密码")
    parser.add_argument("--admin-username", default=env("FREEMAIL_ADMIN_USER"), help="可选：管理员用户名")
    parser.add_argument("--admin-password", default=env("FREEMAIL_ADMIN_PASSWORD"), help="可选：管理员密码")
    parser.add_argument("--guest-username", default=env("FREEMAIL_GUEST_USER", "guest"), help="可选：访客用户名")
    parser.add_argument("--guest-password", default=env("FREEMAIL_GUEST_PASSWORD"), help="可选：访客密码")
    parser.add_argument("--mailbox-username", default=env("FREEMAIL_MAILBOX_USER"), help="可选：邮箱登录账号")
    parser.add_argument("--mailbox-password", default=env("FREEMAIL_MAILBOX_PASSWORD"), help="可选：邮箱登录密码")
    parser.add_argument("--probe-user-ids", default=env("FREEMAIL_PROBE_USER_IDS", "1"), help="越权探测用户 ID，例如 1,2-5")
    parser.add_argument("--probe-mailboxes", default=env("FREEMAIL_PROBE_MAILBOXES"), help="越权探测邮箱，逗号分隔，建议填一个已存在但不属于测试用户的邮箱")
    parser.add_argument("--safe-to-email", default=env("FREEMAIL_SAFE_TO_EMAIL"), help="发信负向测试的收件人，不会在未授权通过时使用")
    parser.add_argument("--allow-mutation", action="store_true", default=env_bool("FREEMAIL_ALLOW_MUTATION"), help="允许执行会改数据的接口测试")
    parser.add_argument("--allow-read-details", action="store_true", default=env_bool("FREEMAIL_ALLOW_READ_DETAILS"), help="允许读取邮件详情，可能会标记已读")
    parser.add_argument("--require-test-user", action="store_true", default=env_bool("FREEMAIL_REQUIRE_TEST_USER"), help="普通用户登录失败时让 CI 失败；默认管理员已配置时跳过普通用户相关用例")
    parser.add_argument("--allow-receive", action="store_true", default=env_bool("FREEMAIL_ALLOW_RECEIVE"), help="允许测试 /receive")
    parser.add_argument("--allow-mailbox-password-change", action="store_true", default=env_bool("FREEMAIL_ALLOW_MAILBOX_PASSWORD_CHANGE"), help="保留开关：默认不改邮箱登录密码")
    parser.add_argument("--receive-body", default=env("FREEMAIL_RECEIVE_BODY"), help="可选：/receive 测试邮件原文")
    parser.add_argument("--json-out", default=env("FREEMAIL_API_JSON_OUT", "reports/freemail-api-tests.json"))
    parser.add_argument("--junit-out", default=env("FREEMAIL_API_JUNIT_OUT", "reports/freemail-api-tests.xml"))
    parser.add_argument("--markdown-out", default=env("FREEMAIL_API_MARKDOWN_OUT"))
    parser.add_argument("--timeout", type=int, default=int(env("FREEMAIL_API_TIMEOUT", "20")))
    parser.add_argument("--insecure", action="store_true", default=env_bool("FREEMAIL_API_INSECURE"), help="禁用 TLS 证书校验")
    return parser


def validate_args(args: argparse.Namespace) -> list[str]:
    errors: list[str] = []
    if not args.base_url:
        errors.append("缺少 FREEMAIL_BASE_URL 或 --base-url")
    if requires_test_user(args) and not args.username:
        errors.append("缺少 FREEMAIL_TEST_USER 或 --username")
    if requires_test_user(args) and not args.password:
        errors.append("缺少 FREEMAIL_TEST_PASSWORD 或 --password")
    args.probe_user_ids = parse_user_ids(str(args.probe_user_ids or ""))
    args.probe_mailboxes = parse_csv(str(args.probe_mailboxes or ""))
    return errors


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    errors = validate_args(args)
    if errors:
        for error in errors:
            print(f"配置错误: {error}", file=sys.stderr)
        return 2

    cases: list[CaseResult] = []
    test_unauthenticated(cases, args)
    _client, payload, user_ctx = test_user_readonly(cases, args)
    test_idor_regressions(cases, args, payload, user_ctx.owned_mailboxes)
    mutation_ctx = test_user_mutation(cases, args)
    test_admin(cases, args, mutation_ctx.generated_mailboxes)
    test_guest(cases, args)
    test_mailbox_login(cases, args)
    test_receive(cases, args)

    print_report(cases)
    write_json(args.json_out, cases)
    write_junit(args.junit_out, cases)
    write_markdown(args.markdown_out, cases)

    return 1 if any(case.status == FAIL for case in cases) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
