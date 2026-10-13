"""管理员在线升级接口，写操作要求同源自定义请求头。"""
from urllib.parse import urlsplit

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
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="升级目录不可读写或状态文件损坏，请检查服务用户权限") from exc


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
