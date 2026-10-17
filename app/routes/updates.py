"""管理员在线升级接口，写操作要求同源自定义请求头。"""
from urllib.parse import urlsplit
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.services.updates import get_update_service
from app.services.update_runtime import UpdateError, UpdateBusy


async def admin_required(request: Request):
    admin = getattr(request.state, "admin", None)
    if not admin or int(admin.get("admin_id", 0)) <= 0:
        raise HTTPException(status_code=401, detail="请使用管理员账户登录")
    if request.method != "GET":
        if request.headers.get("X-Update-Request") != "1":
            raise HTTPException(status_code=403, detail="升级操作缺少请求校验头")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            raise HTTPException(status_code=403, detail="升级操作必须来自本站页面")
    return admin


router = APIRouter(prefix="/api/system/update", tags=["system-update"], dependencies=[Depends(admin_required)])


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup: str


def failure(exc):
    return HTTPException(status_code=409 if isinstance(exc, UpdateBusy) else 400, detail=str(exc))


@router.get("/status")
async def status():
    try:
        return get_update_service().status()
    except UpdateError as exc:
        raise failure(exc)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="升级目录不可读写或状态文件损坏，请检查服务用户权限") from exc


@router.post("/proxy")
async def save_proxy(request: Request, admin=Depends(admin_required)):
    # 手工校验并返回固定错误，避免框架422响应回显带认证的原始代理地址。
    try:
        raw = await request.body()
        if len(raw) > 8192:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) - {"enabled", "url"}:
            raise ValueError
        if type(data.get("enabled")) is not bool:
            raise ValueError
        if data.get("url") is not None and not isinstance(data["url"], str):
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="代理配置格式无效") from None
    try:
        return get_update_service().save_proxy(data["enabled"], data.get("url"), admin.get("username"))
    except UpdateError as exc:
        raise failure(exc)
    except OSError:
        raise HTTPException(status_code=503, detail="代理设置保存失败，请检查升级目录权限") from None


@router.post("/check", status_code=202)
async def check(admin=Depends(admin_required)):
    try:
        return await get_update_service().start("check", admin.get("username"))
    except UpdateError as exc:
        raise failure(exc)


@router.post("/download", status_code=202)
async def download(admin=Depends(admin_required)):
    try:
        return await get_update_service().start("download", admin.get("username"))
    except UpdateError as exc:
        raise failure(exc)


@router.post("/upgrade", status_code=202)
async def upgrade(admin=Depends(admin_required)):
    try:
        return await get_update_service().start("upgrade", admin.get("username"))
    except UpdateError as exc:
        raise failure(exc)


@router.post("/backup", status_code=202)
async def backup(admin=Depends(admin_required)):
    try:
        return await get_update_service().start("backup", admin.get("username"))
    except UpdateError as exc:
        raise failure(exc)


@router.post("/apply", status_code=202)
async def apply(admin=Depends(admin_required)):
    try:
        return get_update_service().request_operation("apply", admin.get("username"))
    except UpdateError as exc:
        raise failure(exc)


@router.post("/rollback", status_code=202)
async def rollback(data: RollbackRequest, admin=Depends(admin_required)):
    try:
        return get_update_service().request_operation("rollback", admin.get("username"), data.backup)
    except UpdateError as exc:
        raise failure(exc)


@router.post("/restart", status_code=202)
async def restart(admin=Depends(admin_required)):
    try:
        return get_update_service().request_operation("restart", admin.get("username"))
    except UpdateError as exc:
        raise failure(exc)
