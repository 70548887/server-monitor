"""安全中间件 - 请求过滤、速率限制、安全响应头"""

import time
import logging
import re
import json
import asyncio
from collections import defaultdict
from typing import Dict, Tuple, Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from starlette.templating import Jinja2Templates
from pathlib import Path

from app.utils.network import get_client_ip
from app.repositories.redis_repository import get_redis_repository

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


class RateLimitStore:
    """速率限制存储（支持 Redis 存储与内存降级）"""
    
    def __init__(self):
        # 内存存储（Redis 不可用时降级使用）
        # {key: [(timestamp, count), ...]}
        self._requests: Dict[str, list] = defaultdict(list)
        # {ip: (locked_until, fail_count)}
        self._login_failures: Dict[str, Tuple[float, int]] = {}
        
        # 锁，延迟初始化以避免事件循环问题
        self._lock: Optional[asyncio.Lock] = None
        
        # 清理间隔
        self._last_cleanup = time.time()
        
        # Redis 相关
        self._redis_available = True
        self._prefix = "ratelimit:"

    async def _get_lock(self) -> asyncio.Lock:
        """获取或创建异步锁"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _get_redis(self):
        """获取 Redis 仓库实例"""
        try:
            if not self._redis_available:
                return None
            return get_redis_repository()
        except Exception:
            self._redis_available = False
            return None

    async def _cleanup_old_records(self, window: int = 60):
        """清理内存中的过期记录"""
        now = time.time()
        # 每分钟清理一次
        if now - self._last_cleanup < 60:
            return
        
        lock = await self._get_lock()
        async with lock:
            cutoff = now - window
            for key in list(self._requests.keys()):
                self._requests[key] = [
                    (ts, cnt) for ts, cnt in self._requests[key] 
                    if ts > cutoff
                ]
                if not self._requests[key]:
                    del self._requests[key]
            
            # 清理过期的登录锁定
            for ip in list(self._login_failures.keys()):
                locked_until, _ = self._login_failures[ip]
                if locked_until < now:
                    del self._login_failures[ip]
            
            self._last_cleanup = now
    
    async def is_rate_limited(self, key: str, limit: int = 100, window: int = 60) -> bool:
        """检查某个 Key 是否超过速率限制"""
        # 1. 尝试使用 Redis
        redis = await self._get_redis()
        if redis:
            try:
                redis_key = f"{self._prefix}req:{key}"
                # 使用 INCR + EXPIRE 实现滑动窗口（简单计数方案）
                # 注意：这并非严格的滑动窗口，而是固定窗口，但对于防刷通常足够
                count = await redis.increment_error_count(redis_key, window)
                if count > limit:
                    return True
                return False
            except Exception as e:
                logger.warning(f"Redis rate limit failed, falling back to memory: {e}")
                self._redis_available = False

        # 2. 降级到内存存储
        await self._cleanup_old_records(window)
        lock = await self._get_lock()
        async with lock:
            now = time.time()
            cutoff = now - window
            
            # 获取窗口内的请求数
            recent = [cnt for ts, cnt in self._requests[key] if ts > cutoff]
            total = sum(recent)
            
            if total >= limit:
                return True
            
            # 记录当前请求
            self._requests[key].append((now, 1))
            return False
    
    async def record_login_failure(self, ip: str, max_failures: int = 5, lockout_minutes: int = 15) -> bool:
        """记录登录失败，返回是否被锁定"""
        now = time.time()
        
        # 1. 尝试使用 Redis
        redis = await self._get_redis()
        if redis:
            try:
                fail_key = f"{self._prefix}login_fail:{ip}"
                count = await redis.increment_error_count(fail_key, lockout_minutes * 60)
                
                if count >= max_failures:
                    lock_key = f"{self._prefix}login_lock:{ip}"
                    await redis.set_alert_suppression(lock_key, lockout_minutes * 60)
                    logger.warning(f"IP {ip} 登录失败过多，已锁定 {lockout_minutes} 分钟 (Redis)")
                    return True
                return False
            except Exception as e:
                logger.warning(f"Redis record login failure failed, falling back to memory: {e}")
                self._redis_available = False

        # 2. 降级到内存
        lock = await self._get_lock()
        async with lock:
            if ip in self._login_failures:
                locked_until, fail_count = self._login_failures[ip]
                # 检查是否仍在锁定期
                if locked_until > now:
                    return True
                fail_count += 1
            else:
                fail_count = 1
            
            # 判断是否需要锁定
            if fail_count >= max_failures:
                locked_until = now + (lockout_minutes * 60)
                self._login_failures[ip] = (locked_until, fail_count)
                logger.warning(f"IP {ip} 登录失败次数过多，已锁定 {lockout_minutes} 分钟 (Memory)")
                return True
            
            self._login_failures[ip] = (0, fail_count)
            return False
    
    async def is_ip_locked(self, ip: str) -> Tuple[bool, int]:
        """检查 IP 是否被锁定，返回 (是否锁定, 剩余秒数)"""
        # 1. 尝试使用 Redis
        redis = await self._get_redis()
        if redis:
            try:
                lock_key = f"{self._prefix}login_lock:{ip}"
                if await redis.is_alert_suppressed(lock_key):
                    # 如果被锁定，返回大致时间（由于 repository 接口限制，这里返回 window 的一半或固定值）
                    return True, 300 
            except Exception as e:
                logger.warning(f"Redis check login lock failed, falling back to memory: {e}")
                self._redis_available = False

        # 2. 降级到内存
        if ip not in self._login_failures:
            return False, 0
        
        locked_until, _ = self._login_failures[ip]
        now = time.time()
        
        if locked_until > now:
            return True, int(locked_until - now)
        return False, 0
    
    async def clear_login_failures(self, ip: str):
        """登录成功后清除失败记录"""
        # 1. 尝试使用 Redis
        redis = await self._get_redis()
        if redis:
            try:
                await redis.delete(f"{self._prefix}login_fail:{ip}")
                await redis.delete(f"{self._prefix}login_lock:{ip}")
            except Exception:
                pass

        # 2. 内存清除
        lock = await self._get_lock()
        async with lock:
            if ip in self._login_failures:
                del self._login_failures[ip]


# 全局存储实例
rate_limit_store = RateLimitStore()


def get_rate_limit_store() -> RateLimitStore:
    """获取速率限制存储实例"""
    return rate_limit_store


class SecurityMiddleware(BaseHTTPMiddleware):
    """安全中间件"""
    
    # 可疑请求模式（SQL 注入、XSS 等）
    SUSPICIOUS_PATTERNS = [
        r"<script.*?>",
        r"javascript:",
        r"on\w+\s*=",
        r"union\s+select",
        r"select\s+.*\s+from",
        r"insert\s+into",
        r"delete\s+from",
        r"drop\s+table",
        r"--\s*$",
        r";\s*--",
        r"\.\./\.\./",  # 路径遍历
        r"%2e%2e%2f",   # URL 编码的路径遍历
        r"etc/passwd",
        r"etc/shadow",
    ]
    
    # 编译正则表达式
    COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in SUSPICIOUS_PATTERNS]
    
    # 敏感路径速率限制配置
    RATE_LIMITS = {
        "/login": (10, 60),      # 仅登录提交：每分钟最多10次，浏览登录页单独计数
        "/settings": (30, 60),   # 设置：每分钟最多 30 次
        "/api/": (200, 60),      # API：每分钟最多 200 次
        "default": (100, 60),    # 默认：每分钟最多 100 次
    }
    
    # 免检路径前缀（如静态资源）
    EXEMPT_PREFIXES = (
        "/static/",
        "/favicon.ico",
        "/robots.txt",
        "/anti-crawling.txt",
    )
    
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        # 获取客户端 IP
        client_ip = get_client_ip(request)
        path = request.url.path
        
        # 1. 检查是否为免检路径
        if path.startswith(self.EXEMPT_PREFIXES):
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            return response

        # 2. 检查 IP 是否被锁定（登录失败过多）
        if path.rstrip("/") == "/login" and request.method == "POST":
            is_locked, remaining = await rate_limit_store.is_ip_locked(client_ip)
            if is_locked:
                logger.warning(f"Blocked request from locked IP: {client_ip}")
                return self._rate_limit_response(
                    request,
                    f"登录尝试次数过多，请 {(remaining + 59) // 60} 分钟后重试",
                    remaining,
                )
        
        # 2. 速率限制检查
        limit, window, category = self._get_rate_limit(path, request.method)
        rate_limit_key = f"{client_ip}:{category}"
        if await rate_limit_store.is_rate_limited(rate_limit_key, limit, window):
            logger.warning(f"Rate limit exceeded for IP: {client_ip}, path: {path} (category: {category})")
            detail = ("登录提交过于频繁，请60秒后再试" if category == "login-submit"
                      else "请求过于频繁，请60秒后再试")
            return self._rate_limit_response(
                request, detail, window,
            )
        
        # 3. 检查可疑请求内容
        if self._is_suspicious_request(request):
            logger.warning(f"Suspicious request blocked from IP: {client_ip}, path: {path}")
            return JSONResponse(
                status_code=403,
                content={"detail": "请求被拒绝"}
            )
        
        # 4. 处理请求
        response = await call_next(request)
        
        # 5. 添加安全响应头
        self._add_security_headers(response)
        
        # 6. 添加处理时间头
        process_time = time.time() - start_time
        response.headers["X-Process-Time"] = str(process_time)
        
        return response
    
    def _rate_limit_response(self, request: Request, detail: str, retry_after: int) -> Response:
        """浏览器登录提交仍返回可重试的登录页；API 保持 JSON 响应。"""
        retry_after = max(1, retry_after)
        headers = {"Retry-After": str(retry_after), "Cache-Control": "no-store"}
        if request.url.path.rstrip("/") == "/login" and "text/html" in request.headers.get("accept", ""):
            response = templates.TemplateResponse(
                request, "login.html", {"error": detail}, status_code=429, headers=headers,
            )
        else:
            response = JSONResponse(
                status_code=429, content={"detail": detail, "retry_after": retry_after}, headers=headers,
            )
        self._add_security_headers(response)
        return response

    def _get_rate_limit(self, path: str, method: str = "GET") -> Tuple[int, int, str]:
        """获取路径对应的速率限制 (limit, window, category)"""
        if path.rstrip("/") == "/login":
            if method == "POST":
                return *self.RATE_LIMITS["/login"], "login-submit"
            # 多标签页、刷新或登录跳转不消耗密码提交额度。
            return *self.RATE_LIMITS["default"], "login-page"
        for pattern, limits in self.RATE_LIMITS.items():
            if pattern in ("default", "/login"):
                continue
            if path.startswith(pattern) or path == pattern:
                return limits[0], limits[1], pattern
        return self.RATE_LIMITS["default"][0], self.RATE_LIMITS["default"][1], "default"

    def _is_suspicious_request(self, request: Request) -> bool:
        """检查请求是否包含可疑内容
        
        注意：只检查 URL 和请求头，不检查请求体
        请求体检查会消耗 body，导致后续处理失败
        """
        # 检查 URL 路径和查询参数
        full_path = str(request.url)
        for pattern in self.COMPILED_PATTERNS:
            if pattern.search(full_path):
                return True
        
        # 检查请求头
        user_agent = request.headers.get("User-Agent", "")
        for pattern in self.COMPILED_PATTERNS:
            if pattern.search(user_agent):
                return True
        
        return False
    
    def _add_security_headers(self, response: Response):
        """添加安全响应头"""
        # 防止 XSS
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        
        # 防止点击劫持
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        
        # 控制 Referrer 信息
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        
        # 权限策略
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        
        # HSTS（仅在 HTTPS 环境启用）
        # response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


class TrustedHostMiddleware(BaseHTTPMiddleware):
    """可信主机中间件 - 防止 Host 头攻击"""
    
    def __init__(self, app, allowed_hosts: list = None):
        super().__init__(app)
        self.allowed_hosts = allowed_hosts or ["*"]
    
    async def dispatch(self, request: Request, call_next):
        if "*" in self.allowed_hosts:
            return await call_next(request)
        
        host = request.headers.get("host", "").split(":")[0]
        
        if host not in self.allowed_hosts:
            logger.warning(f"Rejected request with untrusted host: {host}")
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid host header"}
            )
        
        return await call_next(request)
