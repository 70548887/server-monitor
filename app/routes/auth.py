"""认证路由 - 登录/登出"""

import logging
import os
import httpx
from typing import Optional

from fastapi import APIRouter, Request, Response, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.auth import get_auth_service, AuthService
from app.middleware.security import get_rate_limit_store
from app.utils.network import get_client_ip
from app.utils.captcha import create_captcha, verify_captcha

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")

# Cookie 配置
SESSION_COOKIE_NAME = "session_id"
SESSION_COOKIE_MAX_AGE = 24 * 60 * 60  # 24小时


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    """登录页面"""
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request, 
            "error": error
        }
    )


@router.get("/api/auth/captcha")
async def get_captcha():
    """获取验证码数据"""
    return await create_captcha()


@router.post("/api/auth/captcha/verify")
async def api_verify_captcha(data: dict):
    """
    前端实时校验验证码
    注意：这只是为了提升用户体验，真正的安全校验在登录接口
    """
    captcha_id = data.get("id")
    x_offset = data.get("x")
    
    if not captcha_id or x_offset is None:
        return {"success": False, "message": "参数不完整"}
    
    # 校验但不弹出记录（因为登录接口还需要校验一次）
    # 修改：为了支持两次校验，我们需要在 verify_captcha 中支持不弹出选项
    # 或者我们在这里校验完后，把结果存入 session 或 允许短暂复用
    
    # 简单方案：允许 verify_captcha 接受一个 keep_record 参数
    from app.utils.captcha import captcha_store
    if captcha_id not in captcha_store:
        return {"success": False, "message": "验证码已过期，请刷新"}
    
    correct_x = captcha_store[captcha_id]
    is_valid = abs(correct_x - int(x_offset)) <= 5
    
    return {"success": is_valid}


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    captcha_id: str = Form(None),
    captcha_x: int = Form(None),
    auth_service: AuthService = Depends(get_auth_service),
):
    """处理登录请求"""
    # 验证滑动验证码
    if not captcha_id or captcha_x is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "请先完成人机验证"},
            status_code=400
        )
    
    is_valid = await verify_captcha(captcha_id, captcha_x)
    if not is_valid:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "验证码校验失败，请重试"},
            status_code=400
        )

    # 获取客户端 IP
    client_ip = get_client_ip(request)
    rate_store = get_rate_limit_store()
    
    # 检查 IP 是否被锁定
    is_locked, remaining = await rate_store.is_ip_locked(client_ip)
    if is_locked:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request, 
                "error": f"登录尝试次数过多，请 {remaining // 60 + 1} 分钟后重试"
            },
            status_code=429
        )
    
    # 验证用户凭据
    admin = await auth_service.authenticate(username, password)
    
    if not admin:
        # 记录登录失败
        is_now_locked = await rate_store.record_login_failure(client_ip)
        
        if is_now_locked:
            error_msg = "登录尝试次数过多，账户已被临时锁定"
        else:
            error_msg = "用户名或密码错误"
        
        # 登录失败，返回登录页面并显示错误
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": error_msg},
            status_code=401
        )
    
    # 登录成功，清除失败记录
    await rate_store.clear_login_failures(client_ip)
    
    # 创建会话
    session_id = await auth_service.create_session(admin.id, admin.username)
    
    # 设置 Cookie 并重定向到首页
    redirect_response = RedirectResponse(url="/", status_code=302)
    redirect_response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    
    logger.info(f"Admin logged in: {username} from IP: {client_ip}")
    return redirect_response


@router.get("/logout")
async def logout(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    """登出"""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    
    if session_id:
        await auth_service.destroy_session(session_id)
    
    # 清除 Cookie 并重定向到登录页
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    
    logger.info("Admin logged out")
    return response


async def get_current_admin(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> Optional[dict]:
    """
    获取当前登录的管理员（依赖注入）
    返回 session 数据 或 None
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    
    if not session_id:
        return None
    
    session_data = await auth_service.get_session(session_id)
    return session_data


async def require_auth(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> dict:
    """
    要求登录的依赖（用于保护路由）
    未登录则重定向到登录页
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    session_data = await auth_service.get_session(session_id)
    
    if not session_data:
        raise HTTPException(status_code=401, detail="会话已过期")
    
    return session_data


# ============= 账户管理路由 =============

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    """账户设置页面"""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return RedirectResponse(url="/login", status_code=302)
    
    session_data = await auth_service.get_session(session_id)
    if not session_data:
        return RedirectResponse(url="/login", status_code=302)
    
    # 获取管理员详细信息
    admin_id = int(session_data.get("admin_id", 0))
    admin = await auth_service.get_admin_by_id(admin_id)
    
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "username": session_data.get("username"),
            "admin": admin,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        }
    )


@router.post("/settings/password")
async def change_password(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    auth_service: AuthService = Depends(get_auth_service),
):
    """修改密码"""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return RedirectResponse(url="/login", status_code=302)
    
    session_data = await auth_service.get_session(session_id)
    if not session_data:
        return RedirectResponse(url="/login", status_code=302)
    
    admin_id = int(session_data.get("admin_id", 0))
    
    # 检查新密码确认
    if new_password != confirm_password:
        return RedirectResponse(
            url="/settings?error=两次输入的新密码不一致",
            status_code=302
        )
    
    # 修改密码
    success, message = await auth_service.change_password(admin_id, old_password, new_password)
    
    if success:
        logger.info(f"管理员密码已修改: {session_data.get('username')}")
        return RedirectResponse(url="/settings?success=密码修改成功", status_code=302)
    else:
        return RedirectResponse(url=f"/settings?error={message}", status_code=302)


@router.post("/settings/profile")
async def update_profile(
    request: Request,
    username: str = Form(...),
    email: str = Form(None),
    auth_service: AuthService = Depends(get_auth_service),
):
    """更新账户信息"""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return RedirectResponse(url="/login", status_code=302)
    
    session_data = await auth_service.get_session(session_id)
    if not session_data:
        return RedirectResponse(url="/login", status_code=302)
    
    admin_id = int(session_data.get("admin_id", 0))
    
    # 更新信息
    success, message = await auth_service.update_admin_info(admin_id, username, email)
    
    if success:
        # 如果用户名更改了，需要重新登录
        old_username = session_data.get("username")
        if username != old_username:
            # 销毁旧会话，重新登录
            await auth_service.destroy_session(session_id)
            response = RedirectResponse(url="/login?error=用户名已更改，请重新登录", status_code=302)
            response.delete_cookie(SESSION_COOKIE_NAME)
            return response
        
        logger.info(f"管理员信息已更新: {username}")
        return RedirectResponse(url="/settings?success=信息更新成功", status_code=302)
    else:
        return RedirectResponse(url=f"/settings?error={message}", status_code=302)


# ============= API 接口 =============

@router.get("/api/auth/me")
async def get_current_user(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    """获取当前登录用户信息"""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    session_data = await auth_service.get_session(session_id)
    if not session_data:
        raise HTTPException(status_code=401, detail="会话已过期")
    
    admin_id = int(session_data.get("admin_id", 0))
    admin = await auth_service.get_admin_by_id(admin_id)
    
    if not admin:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    return {
        "id": admin.id,
        "username": admin.username,
        "email": admin.email,
        "is_active": admin.is_active,
        "created_at": admin.created_at.isoformat() if admin.created_at else None,
        "last_login": admin.last_login.isoformat() if admin.last_login else None,
    }


@router.post("/api/auth/change-password")
async def api_change_password(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    """修改密码 API"""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    session_data = await auth_service.get_session(session_id)
    if not session_data:
        raise HTTPException(status_code=401, detail="会话已过期")
    
    admin_id = int(session_data.get("admin_id", 0))
    
    # 解析请求体
    data = await request.json()
    old_password = data.get("old_password")
    new_password = data.get("new_password")
    
    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="缺少必要参数")
    
    success, message = await auth_service.change_password(admin_id, old_password, new_password)
    
    if success:
        return {"success": True, "message": message}
    else:
        raise HTTPException(status_code=400, detail=message)
