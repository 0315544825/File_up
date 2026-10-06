#!/usr/bin/env python3
"""
DuckDuckGo Email Protection API - Skymail 集成版
=============================================

集成功能:
  1. Duck 私有地址生成
  2. Skymail 收件箱读取（接收 Duck 转发的邮件）
  3. 自动认证流程（OTP 发送 → Skymail 等待收信 → 自动提取 OTP → 完成认证）

HTTP API 端点:
  GET  /health                 - 健康检查
  POST /auth/set-token         - 手动设置 Duck access_token
  POST /auth/auto              - 全自动认证（需提供 duck username，自动完成 OTP 流程）
  POST /generate               - 生成 Duck 私有地址
  GET  /dashboard              - Duck 账户信息
  GET  /inbox                  - 查看 Skymail 收件箱（所有 Duck 转发的邮件）
  GET  /inbox/{message_id}     - 读取某封邮件的完整内容
"""

import os
import re
import time
import requests
import argparse
import sys
import json
from typing import Optional, Dict, Any, List


# ─────────────────────────────────────────────
# Skymail 客户端
# ─────────────────────────────────────────────

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

DEFAULT_SKYMAIL_BASE = "https://skymail.ink"
OTP_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<style[^>]*>.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    import html as _html
    return _html.unescape(re.sub(r"\s+", " ", s)).strip()


class SkymailInbox:
    """Skymail.ink 收件箱客户端（使用 curl_cffi 绕过 TLS 指纹检测）"""

    def __init__(self, base_url: str = DEFAULT_SKYMAIL_BASE,
                 email: str = "", password: str = "",
                 impersonate: str = "chrome131"):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self._token: Optional[str] = None
        if curl_requests:
            self.session = curl_requests.Session(impersonate=impersonate)
        else:
            self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
        })

    # ── 认证 ──────────────────────────────────

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        r = self.session.post(
            f"{self.base_url}/api/login",
            json={"email": self.email, "password": self.password},
            timeout=20,
        )
        data = r.json()
        tok = (data.get("data") or {}).get("token")
        if not tok:
            raise RuntimeError(f"Skymail login failed: {data}")
        self._token = tok
        self.session.headers["Authorization"] = tok
        return self._token

    def login(self) -> str:
        return self._ensure_token()

    # ── 收件箱 ────────────────────────────────

    def list_messages(self, size: int = 20) -> List[Dict]:
        """列出收件箱邮件（最新的排前面）"""
        self._ensure_token()
        r = self.session.get(
            f"{self.base_url}/api/email/list",
            params={"emailId": 0, "size": size, "type": 0, "allReceive": 1},
            headers={"Authorization": self._token},
            timeout=20,
        )
        body = r.json()
        data = body.get("data") or {}
        if isinstance(data, dict):
            return data.get("list") or []
        return data if isinstance(data, list) else []

    def get_message(self, email_id: int) -> Dict:
        """获取某封邮件的完整内容"""
        self._ensure_token()
        for path in (f"/api/email/detail/{email_id}", f"/api/email/{email_id}"):
            r = self.session.get(
                self.base_url + path,
                headers={"Authorization": self._token},
                timeout=20,
            )
            try:
                body = r.json()
            except Exception:
                continue
            if body.get("code") == 200:
                return body.get("data") or {}
        raise RuntimeError(f"Cannot fetch email detail for id={email_id}")

    def wait_for_otp(
        self,
        sender_domain: str = "duckduckgo.com",
        timeout: int = 120,
        poll_interval: int = 5,
    ) -> Optional[str]:
        """
        轮询收件箱，等待来自指定域名的 OTP 邮件，
        自动提取 6 位验证码并返回。

        Args:
            sender_domain: 发件方域名过滤
            timeout: 最长等待秒数
            poll_interval: 轮询间隔秒数

        Returns:
            OTP 字符串，或 None（超时）
        """
        deadline = time.time() + timeout
        seen_ids = set()

        print(f"[Skymail] 等待 OTP 邮件（最多 {timeout}s）...")

        while time.time() < deadline:
            messages = self.list_messages()
            for msg in messages:
                msg_id = msg.get("emailId")
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)

                from_addr = msg.get("sendEmail", "") or msg.get("from", "")
                subject = msg.get("subject", "")

                if sender_domain.lower() in from_addr.lower() or sender_domain.lower() in subject.lower():
                    print(f"[Skymail] 收到邮件: {subject}")
                    # 提取 OTP - 先从列表中的 content 字段
                    content = msg.get("content", "")
                    text = _strip_html(content)
                    
                    # 尝试 6 位数字 OTP
                    otp_match = re.search(r"\b(\d{6})\b", text)
                    if otp_match:
                        otp = otp_match.group(1)
                        print(f"[Skymail] 提取到数字 OTP: {otp}")
                        return otp
                    
                    # 尝试 6 位字母数字 OTP
                    otp_match2 = OTP_RE.search(text)
                    if otp_match2:
                        otp = otp_match2.group(1)
                        print(f"[Skymail] 提取到字母数字 OTP: {otp}")
                        return otp

                    # 如果列表中没有 content，尝试获取详情
                    if not content and msg_id:
                        try:
                            detail = self.get_message(msg_id)
                            full_text = _strip_html(detail.get("content", ""))
                            otp_match = re.search(r"\b(\d{6})\b", full_text)
                            if otp_match:
                                return otp_match.group(1)
                            otp_match2 = OTP_RE.search(full_text)
                            if otp_match2:
                                return otp_match2.group(1)
                        except Exception:
                            pass

                    print(f"[Skymail] 邮件收到但未找到 OTP，正文片段: {text[:200]}")

            time.sleep(poll_interval)

        print("[Skymail] 等待超时，未收到 OTP 邮件")
        return None


# ─────────────────────────────────────────────
# Duck Email API 客户端
# ─────────────────────────────────────────────

class DuckEmailAPI:
    """DuckDuckGo Email Protection API 客户端"""

    BASE_URL = "https://quack.duckduckgo.com"
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        access_token: Optional[str] = None,
        skymail: Optional[SkymailInbox] = None,
        user_agent: Optional[str] = None,
    ):
        self.access_token = access_token
        self.skymail = skymail
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or self.DEFAULT_USER_AGENT,
        })

    def _auth_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        t = token or self.access_token
        if not t:
            raise ValueError("未设置 access_token")
        return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}

    # ── 认证流程 ──────────────────────────────

    def request_otp(self, username: str) -> Dict[str, Any]:
        """步骤1：请求向注册邮箱发送 OTP"""
        url = f"{self.BASE_URL}/api/auth/loginlink"
        resp = self.session.get(url, params={"user": username}, timeout=15)
        if resp.status_code == 429:
            return {"status": "error", "message": "请求过于频繁，请稍后再试"}
        resp.raise_for_status()
        return {"status": "success", "message": "OTP 已发送到注册邮箱"}

    def verify_otp(self, username: str, otp: str) -> Dict[str, Any]:
        """步骤2：验证 OTP，获取 access_token"""
        # 2a: otp → login token
        resp = self.session.get(
            f"{self.BASE_URL}/api/auth/login",
            params={"otp": otp, "user": username},
            timeout=15,
        )
        if resp.status_code == 429:
            return {"status": "error", "message": "请求过于频繁"}
        if resp.status_code == 400:
            err = resp.json().get("error", "")
            if err == "invalid_login_credentials":
                return {"status": "error", "message": "OTP 无效"}
            return {"status": "error", "message": f"验证失败: {err}"}
        resp.raise_for_status()
        login_token = resp.json().get("token")
        if not login_token:
            return {"status": "error", "message": "未能获取 login token"}

        # 2b: login token → access_token
        dash_resp = self.session.get(
            f"{self.BASE_URL}/api/email/dashboard",
            headers=self._auth_headers(token=login_token),
            timeout=15,
        )
        dash_resp.raise_for_status()
        access_token = dash_resp.json().get("user", {}).get("access_token")
        if not access_token:
            return {"status": "error", "message": "未能获取 access_token"}

        self.access_token = access_token
        return {
            "status": "success",
            "access_token": access_token,
            "message": "认证成功",
        }

    def auto_authenticate(self, username: str) -> Dict[str, Any]:
        """
        全自动认证：发送 OTP → 等待 Skymail 收信 → 自动提取 OTP → 完成认证。
        需要 Skymail 客户端已配置。
        """
        if not self.skymail:
            return {"status": "error", "message": "未配置 Skymail 客户端，无法自动认证"}

        print(f"[Duck] 向 {username}@duck.com 请求 OTP...")
        result = self.request_otp(username)
        if result["status"] == "error":
            return result

        otp = self.skymail.wait_for_otp(timeout=120, poll_interval=5)
        if not otp:
            return {"status": "error", "message": "等待 OTP 超时，请检查 Skymail 是否为 Duck 转发目标邮箱"}

        return self.verify_otp(username, otp)

    # ── 核心功能 ──────────────────────────────

    def generate_address(self) -> Dict[str, Any]:
        """生成一个新的 Duck 私有地址"""
        resp = self.session.post(
            f"{self.BASE_URL}/api/email/addresses",
            headers=self._auth_headers(),
            timeout=15,
        )
        if resp.status_code == 429:
            return {"status": "error", "message": "请求过于频繁，请稍后再试"}
        if resp.status_code == 401:
            return {"status": "error", "message": "access_token 无效或已过期"}
        if resp.status_code == 201:
            address = resp.json().get("address", "")
            return {
                "status": "success",
                "address": address,
                "email": f"{address}@duck.com",
            }
        return {"status": "error", "message": f"生成失败，状态码: {resp.status_code}", "detail": resp.text}

    def generate_batch(self, count: int, delay: float = 1.0) -> List[Dict]:
        """批量生成多个 Duck 地址"""
        results = []
        for i in range(count):
            result = self.generate_address()
            results.append(result)
            if result["status"] == "error" and "频繁" in result.get("message", ""):
                print("[!] 触发限流，等待 10 秒...")
                time.sleep(10)
            elif i < count - 1:
                time.sleep(delay)
        return results

    def get_dashboard(self) -> Dict[str, Any]:
        """获取 Duck 账户信息"""
        resp = self.session.get(
            f"{self.BASE_URL}/api/email/dashboard",
            headers=self._auth_headers(),
            timeout=15,
        )
        if resp.status_code == 401:
            return {"status": "error", "message": "access_token 无效或已过期"}
        resp.raise_for_status()
        return {"status": "success", "data": resp.json()}


# ─────────────────────────────────────────────
# FastAPI HTTP 服务
# ─────────────────────────────────────────────

def create_app(
    duck_token: Optional[str] = None,
    skymail_email: Optional[str] = None,
    skymail_password: Optional[str] = None,
    skymail_base: str = DEFAULT_SKYMAIL_BASE,
):
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel

    app = FastAPI(
        title="DuckDuckGo Email Protection API（Skymail 集成版）",
        description=(
            "集成 Duck 私有地址生成 + Skymail 收件箱读取。\n\n"
            "**认证方式**：提供 Duck `access_token`（通过 `/auth/set-token`），"
            "或使用 `/auth/auto` 全自动完成 OTP 认证流程。"
        ),
        version="3.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 初始化客户端
    skymail = None
    if skymail_email and skymail_password:
        skymail = SkymailInbox(
            base_url=skymail_base,
            email=skymail_email,
            password=skymail_password,
        )

    duck_api = DuckEmailAPI(access_token=duck_token, skymail=skymail)

    # ── Pydantic 模型 ────────────────────────

    class SetTokenRequest(BaseModel):
        access_token: str

    class OTPRequest(BaseModel):
        username: str

    class VerifyOTPRequest(BaseModel):
        username: str
        otp: str

    class AutoAuthRequest(BaseModel):
        username: str

    class GenerateRequest(BaseModel):
        count: int = 1

    class SkymailConfigRequest(BaseModel):
        email: str
        password: str
        base_url: str = DEFAULT_SKYMAIL_BASE

    # ── 路由 ─────────────────────────────────

    @app.get("/health", summary="健康检查")
    async def health():
        return {
            "status": "ok",
            "duck_token_set": duck_api.access_token is not None,
            "skymail_configured": duck_api.skymail is not None,
            "skymail_email": duck_api.skymail.email if duck_api.skymail else None,
        }

    @app.post("/auth/set-token", summary="设置 Duck access_token")
    async def set_token(req: SetTokenRequest):
        duck_api.access_token = req.access_token
        return {"status": "success", "message": "Duck token 已设置"}

    @app.post("/auth/set-skymail", summary="设置 Skymail 账户（运行时更新）")
    async def set_skymail(req: SkymailConfigRequest):
        duck_api.skymail = SkymailInbox(
            base_url=req.base_url,
            email=req.email,
            password=req.password,
        )
        return {"status": "success", "message": f"Skymail 已配置: {req.email}"}

    @app.post("/auth/request-otp", summary="手动请求 Duck OTP")
    async def request_otp(req: OTPRequest):
        result = duck_api.request_otp(req.username)
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.post("/auth/verify-otp", summary="手动验证 OTP 获取 token")
    async def verify_otp(req: VerifyOTPRequest):
        result = duck_api.verify_otp(req.username, req.otp)
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.post("/auth/auto", summary="全自动认证（自动等待 Skymail 收信并提取 OTP）")
    async def auto_auth(req: AutoAuthRequest):
        """全自动认证流程：
        1. 向 Duck 发送 OTP 请求
        2. 自动轮询 Skymail 收件箱等待验证码邮件
        3. 提取 OTP 完成认证，返回 access_token
        """
        result = duck_api.auto_authenticate(req.username)
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.post("/generate", summary="生成 Duck 私有地址")
    async def generate(req: GenerateRequest = GenerateRequest()):
        if not duck_api.access_token:
            raise HTTPException(status_code=401, detail="未设置 Duck access_token")
        if req.count == 1:
            result = duck_api.generate_address()
            if result["status"] == "error":
                raise HTTPException(status_code=400, detail=result["message"])
            return result
        else:
            results = duck_api.generate_batch(req.count, delay=1.0)
            return {"status": "success", "count": len(results), "addresses": results}

    @app.get("/dashboard", summary="Duck 账户信息")
    async def dashboard():
        if not duck_api.access_token:
            raise HTTPException(status_code=401, detail="未设置 Duck access_token")
        result = duck_api.get_dashboard()
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.get("/inbox", summary="查看 Skymail 收件箱")
    async def inbox(size: int = 20):
        if not duck_api.skymail:
            raise HTTPException(status_code=400, detail="未配置 Skymail，请先调用 /auth/set-skymail")
        try:
            messages = duck_api.skymail.list_messages(size=size)
            return {
                "status": "success",
                "count": len(messages),
                "messages": [
                    {
                        "id": m.get("emailId"),
                        "from": m.get("sendEmail"),
                        "subject": m.get("subject"),
                        "time": m.get("createTime"),
                    }
                    for m in messages
                ],
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/inbox/{email_id}", summary="读取某封邮件完整内容")
    async def get_message(email_id: int):
        if not duck_api.skymail:
            raise HTTPException(status_code=400, detail="未配置 Skymail")
        try:
            msg = duck_api.skymail.get_message(email_id)
            return {
                "status": "success",
                "id": msg.get("emailId"),
                "from": msg.get("sendEmail"),
                "subject": msg.get("subject"),
                "content": msg.get("content"),
                "time": msg.get("createTime"),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return app


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DuckDuckGo Email Protection API（Skymail 集成版）")
    parser.add_argument("--serve", action="store_true", help="启动 HTTP API 服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--duck-token", type=str, help="Duck access_token")
    parser.add_argument("--skymail-email", type=str, help="Skymail 邮箱地址")
    parser.add_argument("--skymail-password", type=str, help="Skymail 密码")
    parser.add_argument("--skymail-base", type=str, default=DEFAULT_SKYMAIL_BASE, help="Skymail 基础 URL")
    parser.add_argument("--generate", action="store_true", help="快速生成地址（CLI 模式）")
    parser.add_argument("--count", type=int, default=1)

    args = parser.parse_args()

    if args.serve:
        import uvicorn
        app = create_app(
            duck_token=args.duck_token,
            skymail_email=args.skymail_email,
            skymail_password=args.skymail_password,
            skymail_base=args.skymail_base,
        )
        print(f"[*] 启动服务: http://{args.host}:{args.port}")
        print(f"[*] API 文档: http://{args.host}:{args.port}/docs")
        uvicorn.run(app, host=args.host, port=args.port)

    elif args.generate:
        if not args.duck_token:
            print("[!] 需要 --duck-token", file=sys.stderr)
            sys.exit(1)
        api = DuckEmailAPI(access_token=args.duck_token)
        if args.count == 1:
            result = api.generate_address()
            print(result.get("email") if result["status"] == "success" else result["message"])
        else:
            for r in api.generate_batch(args.count):
                print(r.get("email") if r["status"] == "success" else r["message"])
    else:
        parser.print_help()


if __name__ == "__main__":
    main()