"""认证中间件 - 保护需要登录的路由"""

import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse

from app.services.auth import get_auth_service

logger = logging.getLogger(__name__)

# Cookie 名称（与 auth.py 保持一致）
SESSION_COOKIE_NAME = "session_id"

# 不需要登录的公开路由
PUBLIC_PATHS = {
    "/login",
    "/api/auth/captcha",
    "/api/auth/captcha/verify",
    "/healthz",
    "/healthz/live",
    "/healthz/ready",
}

# 公开路由前缀
PUBLIC_PREFIXES = (
    "/static/",
)


class AuthMiddleware(BaseHTTPMiddleware):
    """认证中间件"""
    
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        
        # 统一处理路径：去除末尾斜杠（如果是根目录则保留 /）
        normalized_path = path.rstrip("/") if len(path) > 1 else path
        
        # 检查是否为公开路由
        if self._is_public_path(normalized_path):
            return await call_next(request)
        
        # 1. 尝试从 Cookie 获取 Session
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        
        # 2. 如果 Cookie 没有，尝试从 Header 获取 (支持 Authorization: Bearer <token> 或 X-Session-ID)
        if not session_id:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                session_id = auth_header.replace("Bearer ", "", 1)
            else:
                session_id = request.headers.get("X-Session-ID")
        
        if not session_id:
            logger.debug(f"No session found in cookies or headers, redirecting/blocking: {path}")
            # 如果是 API 路径或 metrics，返回 401 而不是重定向
            if path.startswith("/api/") or path == "/metrics":
                return JSONResponse(status_code=401, content={"detail": "未登录或 token 无效"})
            
            return RedirectResponse(url="/login", status_code=302)
        
        # 验证 Session 是否有效
        auth_service = get_auth_service()
        session_data = await auth_service.get_session(session_id)
        
        if not session_data:
            logger.debug(f"Invalid/expired session: {path}")
            if path.startswith("/api/") or path == "/metrics":
                return JSONResponse(status_code=401, content={"detail": "会话已过期"})
            
            response = RedirectResponse(url="/login", status_code=302)
            response.delete_cookie(SESSION_COOKIE_NAME)
            return response
        
        # Session 有效，继续处理请求
        # 将用户信息附加到请求状态
        request.state.admin = session_data
        
        return await call_next(request)
    
    def _is_public_path(self, path: str) -> bool:
        """判断路径是否为公开路由"""
        # 精确匹配（使用归一化后的路径）
        if path in PUBLIC_PATHS:
            return True
        
        # 特殊处理：如果是 /api/auth/captcha 且 PUBLIC_PATHS 里有
        #（防止一些奇怪的匹配问题）
        if path == "/api/auth/captcha":
            return True
            
        # 前缀匹配
        if path.startswith(PUBLIC_PREFIXES):
            return True
        
        return False
