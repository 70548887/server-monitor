"""通知渠道 API 路由"""

import logging
from datetime import datetime, time
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..schemas.notification import (
    NotificationChannelCreate,
    NotificationChannelUpdate,
    NotificationChannelResponse,
    NotificationTestRequest,
    NotificationTestResponse,
    NotificationRecipientCreate,
    NotificationRecipientUpdate,
    NotificationRecipientResponse,
)
from ..services.notification import (
    NotificationService,
    NotificationLogService,
    NotificationRecipientService,
    get_notification_service,
    get_notification_log_service,
)
from ..utils.timeutil import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _is_valid_hhmm(value: str) -> bool:
    """校验 "HH:MM" 格式"""
    try:
        hh, mm = value.split(":")
        return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except (ValueError, AttributeError):
        return False


@router.get("/channels", response_model=List[NotificationChannelResponse])
async def list_channels():
    """
    获取所有通知渠道列表
    """
    service = get_notification_service()
    channels = await service.get_all_channels()
    return channels


@router.get("/channels/{channel_id}", response_model=NotificationChannelResponse)
async def get_channel(channel_id: int):
    """
    获取指定通知渠道详情
    """
    service = get_notification_service()
    channel = await service.get_channel_by_id(channel_id)
    if not channel:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"通知渠道不存在: {channel_id}"}
        )
    # 隐藏敏感信息
    channel["smtp_password"] = "******" if channel.get("smtp_password") else None
    channel["webhook_secret"] = "******" if channel.get("webhook_secret") else None
    return channel


@router.post("/channels", response_model=NotificationChannelResponse, status_code=201)
async def create_channel(data: NotificationChannelCreate):
    """
    创建新的通知渠道
    """
    service = get_notification_service()
    
    # 检查名称是否已存在
    existing = await service.get_channel_by_name(data.name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail={"code": "E002", "message": f"通知渠道名称已存在: {data.name}"}
        )
    
    # 验证邮箱配置
    if data.channel_type.value == "email":
        if not all([data.smtp_host, data.smtp_port, data.smtp_user, data.smtp_password, data.email_recipients]):
            raise HTTPException(
                status_code=400,
                detail={"code": "E003", "message": "邮箱配置不完整，请填写SMTP服务器、端口、用户名、密码和收件人"}
            )
    
    # 创建渠道
    channel_data = {
        "name": data.name,
        "channel_type": data.channel_type.value,
        "enabled": data.enabled,
        "smtp_host": data.smtp_host,
        "smtp_port": data.smtp_port,
        "smtp_ssl": data.smtp_ssl,
        "smtp_user": data.smtp_user,
        "smtp_password": data.smtp_password,
        "email_recipients": data.email_recipients,
        "webhook_url": data.webhook_url,
        "webhook_secret": data.webhook_secret,
        "notify_on_down": data.notify_on_down,
        "notify_on_recovery": data.notify_on_recovery,
        "notify_on_latency": data.notify_on_latency,
    }
    
    channel = await service.create_channel(channel_data)
    logger.info(f"已创建通知渠道: {data.name}")
    return channel


@router.put("/channels/{channel_id}", response_model=NotificationChannelResponse)
async def update_channel(channel_id: int, data: NotificationChannelUpdate):
    """
    更新通知渠道配置
    """
    service = get_notification_service()
    
    # 检查渠道是否存在
    existing = await service.get_channel_by_id(channel_id)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"通知渠道不存在: {channel_id}"}
        )
    
    # 如果更改名称，检查新名称是否已存在
    if data.name and data.name != existing["name"]:
        name_exists = await service.get_channel_by_name(data.name)
        if name_exists:
            raise HTTPException(
                status_code=409,
                detail={"code": "E002", "message": f"通知渠道名称已存在: {data.name}"}
            )
    
    # 准备更新数据（只更新非None字段）
    update_data = {}
    for field in [
        "name", "enabled", "smtp_host", "smtp_port", "smtp_ssl",
        "smtp_user", "smtp_password", "email_recipients",
        "webhook_url", "webhook_secret",
        "notify_on_down", "notify_on_recovery", "notify_on_latency",
        "daily_limit_per_node", "min_interval_seconds",
        "send_time_start", "send_time_end",
    ]:
        value = getattr(data, field, None)
        if value is not None:
            # 处理时间字段
            if field in ["send_time_start", "send_time_end"] and isinstance(value, str):
                try:
                    hours, minutes = map(int, value.split(":"))
                    value = time(hours, minutes)
                except:
                    pass
            update_data[field] = value
    
    channel = await service.update_channel(channel_id, update_data)
    logger.info(f"Updated notification channel: {channel_id}")
    return channel


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: int):
    """
    删除通知渠道
    """
    service = get_notification_service()
    
    success = await service.delete_channel(channel_id)
    if not success:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"通知渠道不存在: {channel_id}"}
        )
    
    logger.info(f"Deleted notification channel: {channel_id}")
    return {"message": "通知渠道已删除", "id": channel_id}


@router.post("/test", response_model=NotificationTestResponse)
async def test_notification(data: NotificationTestRequest):
    """
    测试通知渠道
    
    发送一条测试消息到指定渠道，验证配置是否正确。
    """
    service = get_notification_service()
    
    # 获取渠道
    channel = await service.get_channel_by_id(data.channel_id)
    if not channel:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"通知渠道不存在: {data.channel_id}"}
        )
    
    # 发送测试通知
    subject = "[服务器监控] 测试通知"
    message = data.message or "这是一条测试通知消息，用于验证通知渠道配置是否正确。"
    
    success, error = await service.send_notification(
        channel_id=data.channel_id,
        subject=subject,
        message=message,
        alert_type="test",
        check_limits=False,  # 测试邮件不检查限制
    )
    
    if not success:
        raise HTTPException(
            status_code=500,
            detail={"code": "E004", "message": f"发送测试通知失败: {error}"}
        )
    
    return NotificationTestResponse(
        success=True,
        message="测试通知发送成功",
        channel_name=channel["name"],
        sent_at=utcnow(),
    )


@router.post("/channels/{channel_id}/toggle")
async def toggle_channel(channel_id: int, enabled: bool = Query(..., description="是否启用")):
    """
    启用或禁用通知渠道
    """
    service = get_notification_service()
    
    channel = await service.update_channel(channel_id, {"enabled": enabled})
    if not channel:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"通知渠道不存在: {channel_id}"}
        )
    
    status = "启用" if enabled else "禁用"
    logger.info(f"Toggled notification channel {channel_id}: {status}")
    return {"message": f"通知渠道已{status}", "id": channel_id, "enabled": enabled}


# ===================== 发送记录 API =====================

class NotificationLogResponse(BaseModel):
    """发送记录响应"""
    id: int
    channel_id: int
    channel_name: str
    node_name: Optional[str]
    node_url: Optional[str]
    alert_type: str
    subject: Optional[str]
    message: Optional[str]
    recipients: Optional[str]
    success: bool
    error_message: Optional[str]
    send_duration_ms: Optional[float]
    sent_at: Optional[str]


class NotificationLogsListResponse(BaseModel):
    """发送记录列表响应"""
    logs: List[NotificationLogResponse]
    total: int
    limit: int
    offset: int


@router.get("/logs", response_model=NotificationLogsListResponse)
async def list_logs(
    channel_id: Optional[int] = Query(None, description="渠道ID过滤"),
    node_name: Optional[str] = Query(None, description="节点名称过滤"),
    alert_type: Optional[str] = Query(None, description="告警类型过滤"),
    success: Optional[bool] = Query(None, description="成功状态过滤"),
    limit: int = Query(50, ge=1, le=500, description="每页数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
):
    """
    获取通知发送记录列表
    """
    logs, total = await NotificationLogService.get_logs(
        channel_id=channel_id,
        node_name=node_name,
        alert_type=alert_type,
        success=success,
        limit=limit,
        offset=offset,
    )
    return {
        "logs": logs,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/logs/stats")
async def get_logs_stats():
    """
    获取发送记录统计
    """
    # 今日记录
    today_start = datetime.combine(datetime.today(), time.min)
    today_logs, today_total = await NotificationLogService.get_logs(
        start_date=today_start,
        limit=1000,
    )
    
    today_success = sum(1 for log in today_logs if log.get("success"))
    today_failed = today_total - today_success
    
    # 最近的记录
    recent_logs, _ = await NotificationLogService.get_logs(limit=10)
    
    return {
        "today": {
            "total": today_total,
            "success": today_success,
            "failed": today_failed,
        },
        "recent_logs": recent_logs,
    }


@router.delete("/logs/cleanup")
async def cleanup_logs(
    days: int = Query(30, ge=1, le=365, description="清理多少天前的记录"),
):
    """
    清理旧的发送记录
    """
    count = await NotificationLogService.cleanup_old_logs(days=days)
    return {
        "message": f"已清理 {count} 条记录",
        "deleted_count": count,
    }


@router.delete("/logs/{log_id}")
async def delete_log(log_id: int):
    """
    删除单条发送记录
    """
    success = await NotificationLogService.delete_log(log_id)
    
    if not success:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"记录不存在: {log_id}"}
        )
    
    return {
        "message": "记录已删除",
        "id": log_id,
    }


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    ids: List[int]


@router.post("/logs/batch-delete")
async def batch_delete_logs(request: BatchDeleteRequest):
    """
    批量删除发送记录
    """
    if not request.ids:
        raise HTTPException(
            status_code=400,
            detail={"code": "E005", "message": "请提供要删除的记录ID"}
        )
    
    if len(request.ids) > 500:
        raise HTTPException(
            status_code=400,
            detail={"code": "E005", "message": "单次最多删除500条记录"}
        )
    
    deleted_count = await NotificationLogService.delete_logs_batch(request.ids)
    
    return {
        "message": f"已删除 {deleted_count} 条记录",
        "deleted_count": deleted_count,
        "requested_count": len(request.ids),
    }


# ===================== 通知人员 API =====================

@router.get("/recipients", response_model=List[NotificationRecipientResponse])
async def list_recipients():
    """获取所有通知人员列表"""
    return await NotificationRecipientService.get_all()


@router.get("/recipients/{recipient_id}", response_model=NotificationRecipientResponse)
async def get_recipient(recipient_id: int):
    """获取单个通知人员详情"""
    recipient = await NotificationRecipientService.get_by_id(recipient_id)
    if not recipient:
        raise HTTPException(
            status_code=404,
            detail={"code": "E008", "message": "通知人员不存在"}
        )
    return recipient


@router.post("/recipients", response_model=NotificationRecipientResponse, status_code=201)
async def create_recipient(data: NotificationRecipientCreate):
    """添加通知人员"""
    # 检查邮箱是否已存在
    existing = await NotificationRecipientService.get_by_email(data.email)
    if existing:
        raise HTTPException(
            status_code=409,
            detail={"code": "E007", "message": f"邮箱已在名单中: {data.email}"}
        )
    
    return await NotificationRecipientService.create(data.dict())


@router.put("/recipients/{recipient_id}", response_model=NotificationRecipientResponse)
async def update_recipient(recipient_id: int, data: NotificationRecipientUpdate):
    """更新通知人员信息"""
    updated = await NotificationRecipientService.update(recipient_id, data.dict(exclude_unset=True))
    if not updated:
        raise HTTPException(
            status_code=404,
            detail={"code": "E008", "message": "通知人员不存在"}
        )
    return updated


@router.delete("/recipients/{recipient_id}")
async def delete_recipient(recipient_id: int):
    """删除通知人员"""
    success = await NotificationRecipientService.delete(recipient_id)
    if not success:
        raise HTTPException(
            status_code=404,
            detail={"code": "E008", "message": "通知人员不存在"}
        )
    return {"message": "人员已从通知名单中移除", "id": recipient_id}


@router.post("/recipients/{recipient_id}/toggle")
async def toggle_recipient(recipient_id: int, enabled: bool = Query(...)):
    """启用/禁用通知人员"""
    updated = await NotificationRecipientService.update(recipient_id, {"enabled": enabled})
    if not updated:
        raise HTTPException(
            status_code=404,
            detail={"code": "E008", "message": "通知人员不存在"}
        )
    return {"message": f"人员通知已{'开启' if enabled else '关闭'}", "id": recipient_id, "enabled": enabled}


@router.post("/channels/recalculate-stats")
async def recalculate_channel_stats():
    """
    重新计算所有渠道的发送统计
    
    从发送记录表重新统计，修复统计数据与记录不一致的问题
    """
    result = await NotificationLogService.recalculate_channel_stats()
    
    # 统计变化数量
    changed_count = sum(1 for ch in result.values() if ch.get("changed"))
    
    return {
        "message": f"已重新计算 {len(result)} 个渠道的统计，{changed_count} 个渠道有变化",
        "channels": result,
        "total_channels": len(result),
        "changed_count": changed_count,
    }


# ===================== 告警设置 API =====================

class AlertSettingsUpdate(BaseModel):
    """告警设置更新"""
    daily_limit_per_node: Optional[int] = None
    min_interval_seconds: Optional[int] = None
    send_time_start: Optional[str] = None  # "HH:MM" 格式
    send_time_end: Optional[str] = None  # "HH:MM" 格式


@router.get("/channels/{channel_id}/settings")
async def get_channel_settings(channel_id: int):
    """
    获取渠道的告警设置
    """
    service = get_notification_service()
    channel = await service.get_channel_by_id(channel_id)
    
    if not channel:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"通知渠道不存在: {channel_id}"}
        )
    
    return {
        "channel_id": channel_id,
        "channel_name": channel.get("name"),
        "daily_limit_per_node": channel.get("daily_limit_per_node", 10),
        "min_interval_seconds": channel.get("min_interval_seconds", 10),
        "send_time_start": channel.get("send_time_start"),
        "send_time_end": channel.get("send_time_end"),
    }


@router.put("/channels/{channel_id}/settings")
async def update_channel_settings(channel_id: int, data: AlertSettingsUpdate):
    """
    更新渠道的告警设置
    """
    service = get_notification_service()
    
    # 检查渠道是否存在
    existing = await service.get_channel_by_id(channel_id)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"通知渠道不存在: {channel_id}"}
        )
    
    # 准备更新数据
    update_data = {}
    
    if data.daily_limit_per_node is not None:
        update_data["daily_limit_per_node"] = data.daily_limit_per_node
    
    if data.min_interval_seconds is not None:
        update_data["min_interval_seconds"] = data.min_interval_seconds
    
    if data.send_time_start is not None:
        if data.send_time_start:
            try:
                hours, minutes = map(int, data.send_time_start.split(":"))
                update_data["send_time_start"] = time(hours, minutes)
            except:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "E005", "message": "时间格式错误，请使用 HH:MM 格式"}
                )
        else:
            update_data["send_time_start"] = None
    
    if data.send_time_end is not None:
        if data.send_time_end:
            try:
                hours, minutes = map(int, data.send_time_end.split(":"))
                update_data["send_time_end"] = time(hours, minutes)
            except:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "E005", "message": "时间格式错误，请使用 HH:MM 格式"}
                )
        else:
            update_data["send_time_end"] = None
    
    if update_data:
        await service.update_channel(channel_id, update_data)
    
    logger.info(f"Updated alert settings for channel {channel_id}: {update_data}")
    return {
        "message": "告警设置已更新",
        "channel_id": channel_id,
        **update_data,
    }


# ===================== 告警状态 API =====================

@router.get("/alerts/states")
async def get_alert_states():
    """
    获取所有节点的告警状态
    """
    from ..services.alerting import get_alerting_service
    alerting_service = get_alerting_service()
    return {
        "states": alerting_service.get_all_states(),
    }


@router.get("/alerts/states/{node_name}")
async def get_node_alert_state(node_name: str):
    """
    获取指定节点的告警状态
    """
    from ..services.alerting import get_alerting_service
    alerting_service = get_alerting_service()
    state = alerting_service.get_node_state(node_name)
    
    if not state:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"节点不存在或没有告警状态: {node_name}"}
        )
    
    return state


@router.post("/alerts/states/{node_name}/clear-suppression")
async def clear_node_suppression(
    node_name: str,
    alert_type: Optional[str] = Query(None, description="告警类型(可选)，留空清除所有")
):
    """
    清除节点的告警抑制状态
    """
    from ..services.alerting import get_alerting_service
    alerting_service = get_alerting_service()
    await alerting_service.clear_suppression_async(node_name, alert_type)

    return {
        "message": f"已清除节点 {node_name} 的告警抑制",
        "node_name": node_name,
        "alert_type": alert_type or "all",
        "persisted": True,
    }


@router.get("/alerts/config")
async def get_alert_config():
    """
    获取告警配置
    """
    from ..services.config import get_config
    config = get_config()
    
    return {
        "enabled": config.alerting.enabled,
        "consecutive_failures": config.alerting.consecutive_failures,
        "latency_p95_ms": config.alerting.latency_p95_ms,
        "error_rate_threshold": config.alerting.error_rate_threshold,
        "suppress_minutes": config.alerting.suppress_minutes,
        "window_minutes": config.alerting.window_minutes,
        "notify_on_recovery": config.alerting.notify_on_recovery,
        # 事件化提醒节奏
        "reminder_intervals_minutes": list(config.alerting.reminder_intervals),
        "intervals_loop_minutes": config.alerting.intervals_loop_minutes,
        "max_notifications_per_incident": config.alerting.max_notifications_per_incident,
        "quiet_hours": {
            "enabled": config.alerting.quiet_hours_enabled,
            "start": config.alerting.quiet_hours_start,
            "end": config.alerting.quiet_hours_end,
        },
    }


class AlertConfigUpdate(BaseModel):
    """告警触发条件配置更新"""
    enabled: Optional[bool] = None
    consecutive_failures: Optional[int] = None  # 连续失败N次触发
    latency_p95_ms: Optional[int] = None  # p95延迟阈值(ms)
    suppress_minutes: Optional[int] = None  # 抖动抑制时间(分钟)
    notify_on_recovery: Optional[bool] = None  # 节点恢复时是否发送通知
    # 事件化提醒节奏（Phase 2/3）
    reminder_intervals_minutes: Optional[List[int]] = None  # 阶梯提醒时刻
    intervals_loop_minutes: Optional[int] = None            # 走完阶梯后的循环间隔
    max_notifications_per_incident: Optional[int] = None    # 单事件提醒上限，0=不限
    quiet_hours_enabled: Optional[bool] = None              # 夜间静默
    quiet_hours_start: Optional[str] = None                 # 静默开始 HH:MM
    quiet_hours_end: Optional[str] = None                   # 静默结束 HH:MM
    escalate_after_minutes: Optional[int] = None            # 升级阈值(分钟)，0=不启用


@router.put("/alerts/config")
async def update_alert_config(data: AlertConfigUpdate):
    """
    更新告警触发条件配置
    """
    from ..services.config import get_config
    config = get_config()
    
    updated_fields = {}
    
    if data.enabled is not None:
        config.alerting.enabled = data.enabled
        updated_fields["enabled"] = data.enabled
    
    if data.consecutive_failures is not None:
        if data.consecutive_failures < 1 or data.consecutive_failures > 100:
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "连续失败次数必须在 1-100 之间"}
            )
        config.alerting.consecutive_failures = data.consecutive_failures
        updated_fields["consecutive_failures"] = data.consecutive_failures
    
    if data.latency_p95_ms is not None:
        if data.latency_p95_ms < 100 or data.latency_p95_ms > 60000:
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "延迟阈值必须在 100-60000ms 之间"}
            )
        config.alerting.latency_p95_ms = data.latency_p95_ms
        updated_fields["latency_p95_ms"] = data.latency_p95_ms
    
    if data.suppress_minutes is not None:
        if data.suppress_minutes < 1 or data.suppress_minutes > 1440:
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "抑制时间必须在 1-1440 分钟之间"}
            )
        config.alerting.suppress_minutes = data.suppress_minutes
        updated_fields["suppress_minutes"] = data.suppress_minutes
    
    if data.notify_on_recovery is not None:
        config.alerting.notify_on_recovery = data.notify_on_recovery
        updated_fields["notify_on_recovery"] = data.notify_on_recovery

    # ---------- 事件化提醒节奏 ----------
    if data.reminder_intervals_minutes is not None:
        intervals = data.reminder_intervals_minutes
        if not intervals:
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "提醒时刻不能为空"}
            )
        if any(v < 0 for v in intervals):
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "提醒时刻不能为负数"}
            )
        if intervals != sorted(intervals):
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "提醒时刻必须按升序填写"}
            )
        if len(intervals) > 20:
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "提醒时刻最多 20 个"}
            )
        config.alerting.reminder_intervals = tuple(intervals)
        updated_fields["reminder_intervals_minutes"] = intervals

    if data.intervals_loop_minutes is not None:
        if data.intervals_loop_minutes < 10 or data.intervals_loop_minutes > 10080:
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "循环间隔必须在 10-10080 分钟之间"}
            )
        config.alerting.intervals_loop_minutes = data.intervals_loop_minutes
        updated_fields["intervals_loop_minutes"] = data.intervals_loop_minutes

    if data.max_notifications_per_incident is not None:
        if data.max_notifications_per_incident < 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "单事件提醒上限不能为负（0 表示不限）"}
            )
        config.alerting.max_notifications_per_incident = data.max_notifications_per_incident
        updated_fields["max_notifications_per_incident"] = data.max_notifications_per_incident

    # ---------- 夜间静默 ----------
    if data.quiet_hours_enabled is not None:
        config.alerting.quiet_hours_enabled = data.quiet_hours_enabled
        updated_fields["quiet_hours_enabled"] = data.quiet_hours_enabled

    if data.quiet_hours_start is not None:
        if not _is_valid_hhmm(data.quiet_hours_start):
            raise HTTPException(
                status_code=400,
                detail={"code": "E005", "message": "静默开始时间格式错误，请使用 HH:MM"}
            )
        config.alerting.quiet_hours_start = data.quiet_hours_start
        updated_fields["quiet_hours_start"] = data.quiet_hours_start

    if data.quiet_hours_end is not None:
        if not _is_valid_hhmm(data.quiet_hours_end):
            raise HTTPException(
                status_code=400,
                detail={"code": "E005", "message": "静默结束时间格式错误，请使用 HH:MM"}
            )
        config.alerting.quiet_hours_end = data.quiet_hours_end
        updated_fields["quiet_hours_end"] = data.quiet_hours_end

    if config.alerting.quiet_hours_enabled and \
            config.alerting.quiet_hours_start == config.alerting.quiet_hours_end:
        raise HTTPException(
            status_code=400,
            detail={"code": "E006", "message": "静默时段的开始与结束时间不能相同"}
        )

    # ---------- 升级提醒 ----------
    if data.escalate_after_minutes is not None:
        if data.escalate_after_minutes < 0 or data.escalate_after_minutes > 10080:
            raise HTTPException(
                status_code=400,
                detail={"code": "E006", "message": "升级阈值必须在 0-10080 分钟之间（0 表示不启用）"}
            )
        config.alerting.escalate_after_minutes = data.escalate_after_minutes
        updated_fields["escalate_after_minutes"] = data.escalate_after_minutes
    
    # 同步更新 AlertingService 的配置
    from ..services.alerting import get_alerting_service
    alerting_service = get_alerting_service()
    alerting_service._config = config.alerting

    # 落盘，否则重启后回到文件里的旧值
    from ..services.config import save_alerting_config
    saved = save_alerting_config()
    if not saved:
        logger.warning("告警配置已生效但未能写回 app.yaml，重启后将恢复旧值")

    logger.info(f"Updated alert config: {updated_fields}")

    return {
        "message": "告警配置已更新" + ("" if saved else "（未能写回配置文件，重启后会恢复）"),
        "persisted": saved,
        "updated": updated_fields,
        "current": {
            "enabled": config.alerting.enabled,
            "consecutive_failures": config.alerting.consecutive_failures,
            "latency_p95_ms": config.alerting.latency_p95_ms,
            "suppress_minutes": config.alerting.suppress_minutes,
            "notify_on_recovery": config.alerting.notify_on_recovery,
            "reminder_intervals_minutes": list(config.alerting.reminder_intervals),
            "intervals_loop_minutes": config.alerting.intervals_loop_minutes,
            "quiet_hours_enabled": config.alerting.quiet_hours_enabled,
            "quiet_hours_start": config.alerting.quiet_hours_start,
            "quiet_hours_end": config.alerting.quiet_hours_end,
        }
    }


# ===================== 告警记录 API =====================

class AlertLogResponse(BaseModel):
    """告警记录响应"""
    id: int
    node_name: str
    node_url: Optional[str]
    alert_type: str
    message: Optional[str]
    details: Optional[dict]
    incident_id: Optional[int] = None   # 所属故障事件
    notify_seq: Optional[int] = None    # 该事件内第几次提醒
    notification_sent: bool
    notification_channels: Optional[str]
    notification_count: int
    status: str
    acknowledged_at: Optional[str]
    acknowledged_by: Optional[str]
    resolved_at: Optional[str]
    triggered_at: Optional[str]
    created_at: Optional[str]


class AlertLogsListResponse(BaseModel):
    """告警记录列表响应"""
    alerts: List[AlertLogResponse]
    total: int
    limit: int
    offset: int


@router.get("/alerts/logs", response_model=AlertLogsListResponse)
async def list_alert_logs(
    node_name: Optional[str] = Query(None, description="节点名称过滤"),
    alert_type: Optional[str] = Query(None, description="告警类型过滤"),
    status: Optional[str] = Query(None, description="状态过滤: active, acknowledged, resolved"),
    start_date: Optional[str] = Query(None, description="开始时间 (格式: YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS)"),
    end_date: Optional[str] = Query(None, description="结束时间 (格式: YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS)"),
    limit: int = Query(50, ge=1, le=500, description="每页数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
):
    """
    获取告警记录列表（告警历史）
    
    支持按时间范围筛选，格式支持：
    - YYYY-MM-DD (如 2026-01-21，开始时间默认 00:00:00，结束时间默认 23:59:59)
    - YYYY-MM-DDTHH:MM:SS (如 2026-01-21T14:30:00)
    """
    from ..services.alerting import AlertLogService
    
    # 解析时间参数
    parsed_start = None
    parsed_end = None
    
    if start_date:
        try:
            if "T" in start_date:
                parsed_start = datetime.fromisoformat(start_date)
            else:
                parsed_start = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"code": "E005", "message": f"开始时间格式错误: {start_date}，请使用 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS"}
            )
    
    if end_date:
        try:
            if "T" in end_date:
                parsed_end = datetime.fromisoformat(end_date)
            else:
                # 如果只提供日期，设置为当天结束时间
                parsed_end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"code": "E005", "message": f"结束时间格式错误: {end_date}，请使用 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS"}
            )
    
    alerts, total = await AlertLogService.get_logs(
        node_name=node_name,
        alert_type=alert_type,
        status=status,
        start_date=parsed_start,
        end_date=parsed_end,
        limit=limit,
        offset=offset,
    )
    return {
        "alerts": alerts,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/alerts/logs/stats")
async def get_alert_logs_stats():
    """
    获取告警记录统计
    """
    from ..services.alerting import AlertLogService
    
    stats = await AlertLogService.get_stats()
    return stats


@router.get("/alerts/logs/{alert_id}")
async def get_alert_log(alert_id: int):
    """
    获取告警记录详情
    """
    from ..services.alerting import AlertLogService
    
    alert = await AlertLogService.get_by_id(alert_id)
    if not alert:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"告警记录不存在: {alert_id}"}
        )
    return alert


@router.post("/alerts/logs/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: int,
    acknowledged_by: str = Query("admin", description="确认人"),
):
    """
    确认告警
    """
    from ..services.alerting import AlertLogService
    
    alert = await AlertLogService.acknowledge(alert_id, acknowledged_by)
    if not alert:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"告警记录不存在: {alert_id}"}
        )
    
    logger.info(f"Alert {alert_id} acknowledged by {acknowledged_by}")
    return {
        "message": "告警已确认",
        "alert": alert,
    }


@router.post("/alerts/logs/{alert_id}/resolve")
async def resolve_alert(alert_id: int):
    """
    解决告警
    """
    from ..services.alerting import AlertLogService
    
    alert = await AlertLogService.resolve(alert_id)
    if not alert:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"告警记录不存在: {alert_id}"}
        )
    
    logger.info(f"Alert {alert_id} resolved")
    return {
        "message": "告警已解决",
        "alert": alert,
    }


@router.delete("/alerts/logs/{alert_id}")
async def delete_alert(alert_id: int):
    """
    删除单个告警记录
    """
    from ..services.alerting import AlertLogService
    
    success = await AlertLogService.delete(alert_id)
    if not success:
        raise HTTPException(
            status_code=404,
            detail={"code": "E001", "message": f"告警记录不存在: {alert_id}"}
        )
    
    logger.info(f"Alert {alert_id} deleted")
    return {"message": "告警记录已删除"}


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    ids: List[int]


@router.post("/alerts/logs/batch-delete")
async def batch_delete_alerts(data: BatchDeleteRequest):
    """
    批量删除告警记录
    """
    if not data.ids:
        raise HTTPException(
            status_code=400,
            detail={"code": "E006", "message": "请选择要删除的记录"}
        )

    from ..services.alerting import AlertLogService

    deleted_count = await AlertLogService.batch_delete(data.ids)

    logger.info(f"Batch deleted {deleted_count} alerts")
    return {
        "message": f"已删除 {deleted_count} 条告警记录",
        "deleted_count": deleted_count,
    }


# ===================== 故障事件 API (Phase 2) =====================
class IncidentResponse(BaseModel):
    """故障事件响应"""
    id: int
    node_name: str
    node_url: Optional[str] = None
    alert_type: str
    started_at: Optional[str] = None
    last_notified_at: Optional[str] = None
    next_notify_at: Optional[str] = None
    notify_count: int = 0
    status: str
    resolved_at: Optional[str] = None
    duration_seconds: Optional[int] = None
    last_error: Optional[str] = None
    total_failures: int = 0
    created_at: Optional[str] = None


class IncidentsListResponse(BaseModel):
    """事件列表响应"""
    incidents: List[IncidentResponse]
    total: int
    limit: int
    offset: int


@router.get("/incidents", response_model=IncidentsListResponse)
async def list_incidents(
    node_name: Optional[str] = Query(None, description="节点名称过滤"),
    status: Optional[str] = Query(None, description="状态过滤: open, resolved"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    查询故障事件列表

    一次故障 = 一个事件。相比逐条告警记录，事件视图能直接看出
    "这个节点故障了多久、期间提醒了几次"，而不是一屏重复条目。
    """
    from ..services.incident import IncidentService

    incidents, total = await IncidentService.list_incidents(
        node_name=node_name,
        status=status,
        limit=limit,
        offset=offset,
    )

    return {
        "incidents": incidents,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/incidents/stats")
async def get_incident_stats():
    """
    事件概览统计 - 用于告警页顶部的状态卡片
    """
    from ..services.incident import IncidentService

    open_incidents, open_total = await IncidentService.list_incidents(
        status="open", limit=200
    )
    _, resolved_total = await IncidentService.list_incidents(
        status="resolved", limit=1
    )

    # 事件期间累计的提醒次数，反映"有多少重复提醒被省掉"
    total_notifications = sum(i.get("notify_count") or 0 for i in open_incidents)

    return {
        "open_incidents": open_total,
        "resolved_incidents": resolved_total,
        "open_notifications": total_notifications,
        "open_details": [
            {
                "node_name": i.get("node_name"),
                "started_at": i.get("started_at"),
                "duration_seconds": i.get("duration_seconds"),
                "notify_count": i.get("notify_count"),
                "next_notify_at": i.get("next_notify_at"),
            }
            for i in open_incidents
        ],
    }


@router.get("/incidents/{incident_id}", response_model=IncidentResponse)
async def get_incident(incident_id: int):
    """获取单个事件详情"""
    from ..models.base import get_session_context
    from ..models.notification_channel import AlertIncidentModel
    from sqlalchemy import select

    async with get_session_context() as session:
        stmt = select(AlertIncidentModel).where(AlertIncidentModel.id == incident_id)
        result = await session.execute(stmt)
        incident = result.scalar_one_or_none()

    if not incident:
        raise HTTPException(
            status_code=404,
            detail={"code": "E007", "message": f"事件不存在: {incident_id}"}
        )

    return incident.to_dict()


@router.get("/incidents/{incident_id}/notifications")
async def get_incident_notifications(
    incident_id: int,
    limit: int = Query(100, ge=1, le=500),
):
    """
    查询某事件下的全部提醒记录

    用于事件详情展开：能看到这个故障一共打扰了几次、分别是什么时候。
    """
    from sqlalchemy import select
    from ..models.base import get_session_context
    from ..models.notification_channel import NotificationLogModel

    async with get_session_context() as session:
        stmt = (
            select(NotificationLogModel)
            .where(NotificationLogModel.incident_id == incident_id)
            .order_by(NotificationLogModel.sent_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        logs = [log.to_dict() for log in result.scalars().all()]

    return {
        "incident_id": incident_id,
        "notifications": logs,
        "count": len(logs),
    }

