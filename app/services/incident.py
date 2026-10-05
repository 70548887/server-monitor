"""
故障事件服务 - 事件化告警提醒

设计要点：
    一次故障 = 一个 AlertIncident。重复提醒的节奏由事件的
    next_notify_at 闸门控制，而不再是"自然日重置计数"。

    这样做解决了三个问题：
    1. 跨午夜不再重新爆发——事件有自己的生命周期，与日历无关
    2. 进程重启后节奏延续——状态落在 MySQL，不在内存
    3. 提醒次数递减——阶梯节奏把配额留给真正的新信息

阶梯提醒：
    reminder_intervals = [0, 60, 240, 480, 720] 表示距事件开始
    0min / 1h / 5h / 13h / 25h 各提醒一次，之后每 12 小时循环。
    早期密集（确认有人看见），后期稀疏（不再刷屏）。
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, and_, or_, func
from sqlalchemy.exc import IntegrityError

from ..models.base import get_session_context
from ..models.notification_channel import AlertIncidentModel

logger = logging.getLogger(__name__)


def compute_next_notify_at(
    started_at: datetime,
    notify_count: int,
    intervals: tuple,
    loop_minutes: int,
    now: Optional[datetime] = None,
) -> datetime:
    """
    根据阶梯节奏计算"第 notify_count 次已发"之后的下次通知时间

    以事件开始时间为锚点（而非上次发送时间），这样即使某次发送因限流
    被跳过，后续节奏也不会累积漂移——提醒时点始终对齐到事件开始后的
    固定刻度。

    但有一种情况必须处理：若上一次提醒被推迟很久（静默时段顺延、
    进程停摆、限流堆积），锚点算出的时刻会落在过去，闸门将持续敞开，
    导致静默结束后连发多封。因此取两者较大值：

        max( 阶梯锚点时刻, 本次发送时刻 + 当前档位间隔 )

    Args:
        started_at: 事件开始时间（阶梯锚点）
        notify_count: 已发出的通知次数（1 表示刚发完第一次）
        intervals: 阶梯时间点列表（分钟），需升序
        loop_minutes: 走完阶梯后的循环间隔（分钟）
        now: 本次发送的时刻，默认当前时间

    Returns:
        下次允许通知的时间
    """
    if not intervals:
        intervals = (0,)

    idx = notify_count  # 下一次通知对应阶梯的第 idx 个下标
    if idx < len(intervals):
        offset = intervals[idx]
        prev_offset = intervals[idx - 1] if idx >= 1 else 0
        step = offset - prev_offset
    else:
        # 超出阶梯长度：以最后一个刻度为基准循环
        offset = intervals[-1] + loop_minutes * (idx - len(intervals) + 1)
        step = loop_minutes

    anchor_based = started_at + timedelta(minutes=offset)

    if now is None:
        return anchor_based

    # 防连发：距本次发送至少隔 step 分钟。step 为 0 时兜底 1 分钟，
    # 避免阶梯首档 0 导致同一分钟内重复发送。
    min_gap = max(step, 1)
    spacing_based = now + timedelta(minutes=min_gap)

    return max(anchor_based, spacing_based)


class IncidentService:
    """故障事件管理"""

    @staticmethod
    async def get_open_incident(node_name: str, alert_type: str) -> Optional[dict]:
        """获取节点当前未关闭的事件"""
        try:
            async with get_session_context() as session:
                stmt = (
                    select(AlertIncidentModel)
                    .where(
                        and_(
                            AlertIncidentModel.node_name == node_name,
                            AlertIncidentModel.alert_type == alert_type,
                            AlertIncidentModel.status == "open",
                        )
                    )
                    .order_by(AlertIncidentModel.started_at.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                incident = result.scalar_one_or_none()
                return incident.to_dict() if incident else None
        except Exception as e:
            logger.error(f"Failed to get open incident: node={node_name}, error={e}")
            return None

    @staticmethod
    async def open_incident(
        node_name: str,
        alert_type: str,
        node_url: Optional[str],
        started_at: datetime,
        last_error: Optional[str] = None,
    ) -> Optional[dict]:
        """
        开启一个新事件

        首次告警时调用。若并发下已有 open 事件（唯一索引冲突），
        返回既有的那条，避免同一故障开出两个事件。
        """
        try:
            async with get_session_context() as session:
                incident = AlertIncidentModel(
                    node_name=node_name,
                    alert_type=alert_type,
                    node_url=node_url,
                    started_at=started_at,
                    notify_count=0,
                    status="open",
                    last_error=last_error,
                    total_failures=0,
                    # 首次提醒立即放行（阶梯第一档为 0 分钟）
                    next_notify_at=started_at,
                )
                session.add(incident)
                await session.commit()
                await session.refresh(incident)
                logger.info(
                    f"Incident opened: id={incident.id}, node={node_name}, "
                    f"type={alert_type}, started_at={started_at}"
                )
                return incident.to_dict()
        except IntegrityError:
            # 并发下另一个协程已开启事件，复用它的
            await session.rollback() if 'session' in dir() else None
            logger.debug(f"Incident already opened concurrently: node={node_name}")
            return await IncidentService.get_open_incident(node_name, alert_type)
        except Exception as e:
            logger.error(f"Failed to open incident: node={node_name}, error={e}")
            return None

    @staticmethod
    async def should_notify(incident: dict, now: datetime) -> tuple[bool, str]:
        """
        判断事件当前是否允许发出提醒

        Returns:
            (是否允许, 原因说明)
        """
        next_notify_at = incident.get("next_notify_at")

        if next_notify_at is None:
            # 未设置闸门视为可发（新事件）
            return True, "no gate set"

        # 数据库中读出的可能是字符串（to_dict 做了 isoformat）
        if isinstance(next_notify_at, str):
            try:
                next_notify_at = datetime.fromisoformat(next_notify_at)
            except ValueError:
                return True, "unparsable gate, allowing"

        if now < next_notify_at:
            remaining = (next_notify_at - now).total_seconds()
            return False, f"闸门未到，还需 {int(remaining)} 秒"

        return True, "gate passed"

    @staticmethod
    async def record_notification(
        incident_id: int,
        intervals: tuple,
        loop_minutes: int,
        last_error: Optional[str] = None,
        failure_delta: int = 0,
        now: Optional[datetime] = None,
    ) -> Optional[dict]:
        """
        记录一次提醒已发出，并推进闸门

        notify_count 自增后计算下一次的 next_notify_at。

        Args:
            now: 本次发送的时刻。调用方应传入该次检测的时间戳，
                 使闸门推进与闸门判定共用同一时间基准；
                 省略时退回当前时间。

        Returns:
            更新后的事件字典
        """
        try:
            async with get_session_context() as session:
                stmt = select(AlertIncidentModel).where(AlertIncidentModel.id == incident_id)
                result = await session.execute(stmt)
                incident = result.scalar_one_or_none()

                if not incident:
                    logger.warning(f"Incident not found when recording notification: {incident_id}")
                    return None

                incident.notify_count = (incident.notify_count or 0) + 1
                sent_at = now or datetime.now()
                incident.last_notified_at = sent_at
                incident.next_notify_at = compute_next_notify_at(
                    started_at=incident.started_at,
                    notify_count=incident.notify_count,
                    intervals=intervals,
                    loop_minutes=loop_minutes,
                    now=sent_at,
                )
                if last_error:
                    incident.last_error = last_error
                if failure_delta:
                    incident.total_failures = (incident.total_failures or 0) + failure_delta

                await session.commit()
                await session.refresh(incident)

                logger.debug(
                    f"Incident {incident_id} notified #{incident.notify_count}, "
                    f"next at {incident.next_notify_at}"
                )
                return incident.to_dict()
        except Exception as e:
            logger.error(f"Failed to record notification for incident {incident_id}: {e}")
            return None

    @staticmethod
    async def defer_notification(
        incident_id: int,
        until: Optional[datetime],
        last_error: Optional[str] = None,
    ) -> Optional[dict]:
        """
        因静默时段推迟提醒

        只把闸门推到静默结束时刻，**不增加 notify_count**——
        提醒是被推迟而不是跳过，静默结束后仍按原阶梯节奏继续。
        同时累计失败次数，保证事件期间的失败总数不受影响。

        Args:
            incident_id: 事件ID
            until: 静默结束时刻；None 表示不推迟（仅累计失败）
            last_error: 最近错误信息
        """
        try:
            async with get_session_context() as session:
                stmt = select(AlertIncidentModel).where(AlertIncidentModel.id == incident_id)
                result = await session.execute(stmt)
                incident = result.scalar_one_or_none()
                if not incident:
                    return None

                incident.total_failures = (incident.total_failures or 0) + 1
                if last_error:
                    incident.last_error = last_error

                if until is not None:
                    # 闸门推到静默结束；阶梯锚点（started_at）保持不变，
                    # 因此不会因为跨夜而累积漂移。
                    incident.next_notify_at = until

                await session.commit()
                await session.refresh(incident)
                return incident.to_dict()
        except Exception as e:
            logger.error(f"Failed to defer notification for incident {incident_id}: {e}")
            return None

    @staticmethod
    async def bump_failures(incident_id: int, delta: int = 1, last_error: Optional[str] = None):
        """累计事件期间的失败次数（每次心跳失败调用，不产生通知）"""
        try:
            async with get_session_context() as session:
                stmt = select(AlertIncidentModel).where(AlertIncidentModel.id == incident_id)
                result = await session.execute(stmt)
                incident = result.scalar_one_or_none()
                if not incident:
                    return
                incident.total_failures = (incident.total_failures or 0) + delta
                if last_error:
                    incident.last_error = last_error
                await session.commit()
        except Exception as e:
            logger.debug(f"Failed to bump incident failures: {e}")

    @staticmethod
    async def resolve_incident(incident_id: int, resolved_at: Optional[datetime] = None) -> Optional[dict]:
        """
        关闭事件

        恢复通知发出后调用。返回关闭前的事件快照，
        供恢复通知汇总故障时长与提醒次数。
        """
        try:
            async with get_session_context() as session:
                stmt = select(AlertIncidentModel).where(AlertIncidentModel.id == incident_id)
                result = await session.execute(stmt)
                incident = result.scalar_one_or_none()

                if not incident:
                    logger.warning(f"Incident not found when resolving: {incident_id}")
                    return None

                resolved_at = resolved_at or datetime.now()
                snapshot = incident.to_dict()

                incident.status = "resolved"
                incident.resolved_at = resolved_at
                if incident.started_at:
                    incident.duration_seconds = int(
                        (resolved_at - incident.started_at).total_seconds()
                    )
                # 关闭后清空闸门，避免残留状态影响下一次故障
                incident.next_notify_at = None

                await session.commit()
                await session.refresh(incident)

                snapshot["status"] = "resolved"
                snapshot["resolved_at"] = incident.resolved_at.isoformat()
                snapshot["duration_seconds"] = incident.duration_seconds

                logger.info(
                    f"Incident resolved: id={incident_id}, node={incident.node_name}, "
                    f"duration={incident.duration_seconds}s, notifications={incident.notify_count}"
                )
                return snapshot
        except Exception as e:
            logger.error(f"Failed to resolve incident {incident_id}: {e}")
            return None

    @staticmethod
    async def list_incidents(
        node_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list, int]:
        """查询事件列表（供事件视图使用）"""
        try:
            async with get_session_context() as session:
                conditions = []
                if node_name:
                    conditions.append(AlertIncidentModel.node_name == node_name)
                if status:
                    conditions.append(AlertIncidentModel.status == status)

                base = select(AlertIncidentModel)
                count_stmt = select(func.count()).select_from(AlertIncidentModel)

                if conditions:
                    base = base.where(and_(*conditions))
                    count_stmt = count_stmt.where(and_(*conditions))

                total_result = await session.execute(count_stmt)
                total = total_result.scalar() or 0

                stmt = (
                    base.order_by(AlertIncidentModel.started_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                result = await session.execute(stmt)
                incidents = [i.to_dict() for i in result.scalars().all()]
                return incidents, total
        except Exception as e:
            logger.error(f"Failed to list incidents: {e}")
            return [], 0


def get_incident_service() -> IncidentService:
    """获取事件服务实例（无状态，返回类本身）"""
    return IncidentService
