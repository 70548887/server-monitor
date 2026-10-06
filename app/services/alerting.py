"""告警评估服务 - 检测节点状态变化并触发告警"""

import json
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from collections import deque

from sqlalchemy import select, func, and_

from .config import get_config, AlertingConfig
from .heartbeat import HeartbeatResult
from ..models.base import get_session_context
from ..models.notification_channel import AlertLogModel

logger = logging.getLogger(__name__)


def to_local_time(
    ts: Optional[datetime],
    assume_utc: bool = True,
) -> Optional[datetime]:
    """
    把时间戳转换为本地时间，供"按本地时刻"的判定使用

    为什么需要：心跳时间戳由 datetime.utcnow() 生成（UTC），而静默时段
    这类配置是用户按本地时刻填写的（如"23:00-07:00"）。两者直接比较会
    整体错位 —— 在 UTC+8 上，拿 UTC 时间戳去套 23:00-07:00，实际静默的
    是本地 07:00-15:00，与用户意图恰好相反。

    存储层统一用 UTC（日志中 asctime 是本地、timestamp 是 UTC），
    因此在做任何"本地时刻"判定前必须先转换。

    Args:
        ts: 待转换的时间戳
        assume_utc: 朴素时间戳是否按 UTC 解释。默认 True，对应心跳与
                    数据库的写入约定；调用方已持有本地时间时传 False。
    """
    if ts is None:
        return None
    if ts.tzinfo is not None:
        # 已带时区：直接换算
        return ts.astimezone()
    if assume_utc:
        return ts.replace(tzinfo=timezone.utc).astimezone()
    # 已是本地朴素时间，无需转换
    return ts


def _format_duration_cn(seconds: Optional[int]) -> str:
    """把秒数格式化为中文时长，供通知正文使用"""
    if seconds is None:
        return "未知"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    parts = []
    if days:
        parts.append(f"{days}天")
        # 有天数时，小时位即使为 0 也补上，避免出现"1天58分钟"这种读起来别扭的写法
        parts.append(f"{hours}小时")
    elif hours:
        parts.append(f"{hours}小时")
    if minutes or not parts:
        parts.append(f"{minutes}分钟")
    return "".join(parts)


@dataclass
class NodeAlertState:
    """节点告警状态"""
    node_name: str
    consecutive_failures: int = 0
    last_success: bool = True
    last_check_at: Optional[datetime] = None
    
    # 告警抑制记录: alert_type -> last_triggered_at
    suppressed_alerts: Dict[str, datetime] = field(default_factory=dict)
    
    # 延迟记录（用于计算 p95）
    latencies: deque = field(default_factory=lambda: deque(maxlen=100))


@dataclass
class AlertEvent:
    """告警事件"""
    alert_type: str  # node_down, node_recovery, latency_high
    node_name: str
    node_url: str
    message: str
    details: Dict = field(default_factory=dict)
    triggered_at: datetime = field(default_factory=datetime.now)
    # 事件化提醒关联（Phase 2）
    incident_id: Optional[int] = None
    notify_seq: Optional[int] = None       # 该事件内第几次提醒(从1开始)
    incident_duration_s: Optional[int] = None  # 恢复通知用：故障持续秒数
    incident_notify_count: Optional[int] = None  # 恢复通知用：期间提醒次数


class AlertingService:
    """告警评估服务"""
    
    def __init__(self, config: Optional[AlertingConfig] = None):
        self._config = config or get_config().alerting
        self._states: Dict[str, NodeAlertState] = {}
        self._lock = threading.RLock()
        
        logger.info(
            f"AlertingService initialized: enabled={self._config.enabled}, "
            f"consecutive_failures={self._config.consecutive_failures}, "
            f"suppress_minutes={self._config.suppress_minutes}, "
            f"latency_p95_ms={self._config.latency_p95_ms}"
        )
    
    def _get_or_create_state(self, node_name: str) -> NodeAlertState:
        """获取或创建节点告警状态"""
        if node_name not in self._states:
            self._states[node_name] = NodeAlertState(node_name=node_name)
        return self._states[node_name]
    
    def _is_suppressed(self, state: NodeAlertState, alert_type: str) -> bool:
        """检查告警是否被抑制（仅内存判定，异步持久化层见 _prefetch_suppression）"""
        if alert_type not in state.suppressed_alerts:
            return False

        last_triggered = state.suppressed_alerts[alert_type]
        suppress_duration = timedelta(minutes=self._config.suppress_minutes)

        if datetime.now() - last_triggered < suppress_duration:
            remaining = suppress_duration - (datetime.now() - last_triggered)
            logger.debug(
                f"Alert suppressed: node={state.node_name}, type={alert_type}, "
                f"remaining={remaining.total_seconds():.0f}s"
            )
            return True

        return False

    def _suppression_key(self, node_name: str, alert_type: str) -> str:
        """告警抑制在 Redis 中的唯一标识"""
        return f"{node_name}:{alert_type}"

    async def _prefetch_suppression(self, node_name: str) -> set:
        """
        从 Redis 预取该节点的抑制状态

        在获取内存锁之前调用，避免在锁内进行网络 I/O
        （threading.RLock 在异步上下文中跨 await 持有会造成事件循环阻塞）。

        Returns:
            处于抑制状态的 alert_type 集合；Redis 不可用时返回空集合，
            由内存状态兜底。
        """
        if not self._config.suppress_persist:
            return set()

        try:
            from ..repositories.redis_repository import get_redis_repository
            repo = get_redis_repository()

            # 未连接时快速跳过：每个节点每轮会查 3 个键，
            # 若 Redis 不可用，逐次尝试建立连接会在心跳路径上放大成显著延迟。
            if not repo.is_connected():
                return set()

            suppressed = set()
            # 逐个检查三类告警的抑制键
            for alert_type in ("node_down", "node_recovery", "latency_high"):
                if await repo.is_alert_suppressed(
                    self._suppression_key(node_name, alert_type)
                ):
                    suppressed.add(alert_type)
            return suppressed
        except Exception as e:
            # Redis 不可用时不影响主流程，退化为纯内存抑制
            logger.debug(f"Suppression prefetch failed, falling back to memory: {e}")
            return set()

    def _is_suppressed_combined(
        self,
        state: NodeAlertState,
        alert_type: str,
        redis_suppressed: set,
    ) -> bool:
        """
        综合内存与 Redis 的抑制状态

        内存状态负责当前进程内的一致性，Redis 负责跨进程重启的延续性。
        两者取并集：任一判定为抑制即抑制。
        """
        if alert_type in redis_suppressed:
            logger.debug(
                f"Alert suppressed (redis, survived restart): "
                f"node={state.node_name}, type={alert_type}"
            )
            return True
        return self._is_suppressed(state, alert_type)

    def _mark_alert_triggered(self, state: NodeAlertState, alert_type: str):
        """标记告警已触发（内存部分，立即生效）"""
        state.suppressed_alerts[alert_type] = datetime.now()

    async def _persist_suppression(self, node_name: str, alert_type: str):
        """
        将抑制状态写入 Redis，使其跨进程重启存活

        失败不抛异常，不影响告警主流程。
        """
        if not self._config.suppress_persist:
            return

        try:
            from ..repositories.redis_repository import get_redis_repository
            repo = get_redis_repository()
            await repo.set_alert_suppression(
                self._suppression_key(node_name, alert_type),
                self._config.suppress_minutes * 60,
            )
        except Exception as e:
            logger.warning(f"Failed to persist suppression to redis: {e}")

    def _calculate_p95_latency(self, state: NodeAlertState) -> Optional[float]:
        """计算 p95 延迟"""
        if len(state.latencies) < 5:  # 至少需要 5 个样本
            return None
        
        sorted_latencies = sorted(state.latencies)
        p95_index = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[min(p95_index, len(sorted_latencies) - 1)]
    
    async def evaluate(self, result: HeartbeatResult) -> list[AlertEvent]:
        """
        评估心跳结果，返回需要触发的告警事件列表

        Args:
            result: 心跳检测结果

        Returns:
            需要触发的告警事件列表
        """
        if not self._config.enabled:
            return []

        # 在获取内存锁之前完成 Redis I/O：
        # threading.RLock 跨 await 持有会阻塞事件循环，因此网络调用必须在锁外。
        redis_suppressed = await self._prefetch_suppression(result.node_name)

        events = []
        # 需要在锁外落库的抑制状态 (alert_type, cleared?)
        suppression_writes: list[tuple[str, bool]] = []
        # 锁内只做纯内存判断，把需要 DB 的意图带出锁外处理
        down_intent: Optional[dict] = None       # 达到阈值，待事件闸门裁决
        recovery_intent: Optional[dict] = None   # 故障恢复，待关闭事件

        with self._lock:
            state = self._get_or_create_state(result.node_name)
            previous_success = state.last_success
            previous_failures = state.consecutive_failures

            # 更新状态
            state.last_check_at = result.timestamp
            state.latencies.append(result.latency_ms)

            if result.success:
                # 检查是否需要触发恢复告警
                if not previous_success and previous_failures >= self._config.consecutive_failures:
                    # 节点从失败恢复：重置类告警仍受抑制控制，避免抖动刷屏。
                    # 真正的恢复通知由事件闸门负责发出并关闭事件。
                    if not self._is_suppressed_combined(state, "node_recovery", redis_suppressed):
                        recovery_intent = {
                            "previous_failures": previous_failures,
                            "latency_ms": round(result.latency_ms, 2),
                            "status_code": result.status_code,
                        }
                        self._mark_alert_triggered(state, "node_recovery")
                        suppression_writes.append(("node_recovery", False))

                # 重置失败计数
                state.consecutive_failures = 0
                state.last_success = True

                # 事件已结束，清空抑制记录：
                # 否则节点在抑制窗口内二次离线会被静默吞掉。
                if state.suppressed_alerts:
                    cleared = [k for k in state.suppressed_alerts if k != "node_recovery"]
                    for alert_type in cleared:
                        state.suppressed_alerts.pop(alert_type, None)
                        suppression_writes.append((alert_type, True))

            else:
                # 失败，增加计数
                state.consecutive_failures += 1
                state.last_success = False

                # 达到连续失败阈值：交由事件闸门裁决是否真的发出提醒。
                # 这里不再判断抑制时间——节奏完全由事件的 next_notify_at 决定。
                if state.consecutive_failures >= self._config.consecutive_failures:
                    down_intent = {
                        "consecutive_failures": state.consecutive_failures,
                        "error_type": result.error_type,
                        "error_message": result.error_message,
                        "status_code": result.status_code,
                    }

            # 检查延迟告警
            if result.success:
                p95 = self._calculate_p95_latency(state)
                if p95 and p95 > self._config.latency_p95_ms:
                    if not self._is_suppressed_combined(state, "latency_high", redis_suppressed):
                        events.append(AlertEvent(
                            alert_type="latency_high",
                            node_name=result.node_name,
                            node_url=result.url,
                            message=f"节点 {result.node_name} 延迟过高，p95={p95:.0f}ms（阈值={self._config.latency_p95_ms}ms）",
                            details={
                                "p95_latency_ms": round(p95, 2),
                                "threshold_ms": self._config.latency_p95_ms,
                                "current_latency_ms": round(result.latency_ms, 2),
                            },
                        ))
                        self._mark_alert_triggered(state, "latency_high")
                        suppression_writes.append(("latency_high", False))
                        logger.info(f"Alert triggered: latency_high for {result.node_name} (p95={p95:.0f}ms)")

        # ===== 锁外：持久化抑制状态到 Redis（跨进程重启存活）=====
        for alert_type, cleared in suppression_writes:
            if cleared:
                await self._clear_persisted_suppression(result.node_name, alert_type)
            else:
                await self._persist_suppression(result.node_name, alert_type)

        # ===== 锁外：事件化提醒闸门（Phase 2）=====
        # node_down 的重发节奏不再由 suppress_minutes 决定，改由事件的
        # next_notify_at 闸门控制。事件锚定在故障开始时刻，跨午夜、
        # 进程重启都不会重置节奏；未被放行的检测不产生通知、不计配额。
        events.extend(await self._apply_incident_gate(result, down_intent, recovery_intent))

        return events
    
    async def _apply_incident_gate(
        self,
        result: HeartbeatResult,
        down_intent: Optional[dict],
        recovery_intent: Optional[dict],
    ) -> list[AlertEvent]:
        """
        事件闸门 - 决定这次检测是否真的产生一条提醒

        这是 Phase 2 的核心：node_down 的重发不再看 suppress_minutes，
        而是看事件的 next_notify_at。闸门未到 → 不产生事件、不发通知、
        不写发送记录、不消耗配额（只是静默累计失败次数）。

        Args:
            result: 本次心跳结果
            down_intent: 锁内判断达到阈值时携带的信息
            recovery_intent: 锁内判断故障恢复时携带的信息

        Returns:
            真正需要发出的告警事件
        """
        from .incident import IncidentService

        events: list[AlertEvent] = []
        node_name = result.node_name
        # 统一以"本次检测的时间"判定闸门与静默时段，而不是墙上时钟。
        # 用 datetime.now() 会让延迟到达的结果按错误的时刻评估，
        # 也会让测试无法确定性地控制时间推进。
        now = result.timestamp

        # ---------- 节点离线 ----------
        if down_intent:
            incident = await IncidentService.get_open_incident(node_name, "node_down")

            if incident is None:
                # 新故障：开事件，首次提醒立即放行
                incident = await IncidentService.open_incident(
                    node_name=node_name,
                    alert_type="node_down",
                    node_url=result.url,
                    started_at=result.timestamp,
                    last_error=down_intent.get("error_message"),
                )
                if incident is None:
                    # 事件无法建立（数据库不可用）：退化为直接告警，
                    # 宁可重复也不要漏报
                    return [self._build_down_event(result, down_intent, None, None)]

                seq = 1
            else:
                should, reason = await IncidentService.should_notify(incident, now)
                if not should:
                    # ★ 闸门未到：静默累计，不产生任何通知
                    await IncidentService.bump_failures(
                        incident["id"],
                        delta=1,
                        last_error=down_intent.get("error_message"),
                    )
                    logger.debug(
                        f"Incident gate held: node={node_name}, reason={reason}"
                    )
                    return events
                seq = (incident.get("notify_count") or 0) + 1

            # ---------- 静默时段：顺延而非丢弃 ----------
            # 这里不推进闸门、不增加 notify_count：提醒是被推迟而不是跳过，
            # 静默结束后仍按原阶梯节奏继续，不会因为夜间而少一次提醒。
            if self._in_quiet_hours(now):
                deferred_until = self._quiet_hours_end_datetime(now)
                await IncidentService.defer_notification(
                    incident["id"],
                    deferred_until,
                    last_error=down_intent.get("error_message"),
                )
                logger.info(
                    f"Alert deferred by quiet hours: node={node_name}, until={deferred_until}"
                )
                return events

            # 上限保护（默认 0 = 不限，由阶梯自然收敛）
            max_n = self._config.max_notifications_per_incident
            if max_n and seq > max_n:
                await IncidentService.bump_failures(
                    incident["id"],
                    delta=1,
                    last_error=down_intent.get("error_message"),
                )
                logger.debug(
                    f"Incident {incident['id']} hit notification cap ({max_n}), suppressing"
                )
                return events

            # 推进闸门并累计本次失败
            updated = await IncidentService.record_notification(
                incident_id=incident["id"],
                intervals=self._config.reminder_intervals,
                loop_minutes=self._config.intervals_loop_minutes,
                last_error=down_intent.get("error_message"),
                failure_delta=1,
                now=now,
            )

            event = self._build_down_event(result, down_intent, incident, seq)
            if updated:
                event.details["next_notify_at"] = updated.get("next_notify_at")
                event.details["incident_started_at"] = updated.get("started_at")
                event.details["incident_total_failures"] = updated.get("total_failures")
            events.append(event)

            # 首次告警时，抑制状态也记录一笔（供 latency 等其他路径参考）
            await self._persist_suppression(node_name, "node_down")
            return events

        # ---------- 节点恢复 ----------
        if recovery_intent:
            incident = await IncidentService.get_open_incident(node_name, "node_down")

            if incident is None:
                # 没有对应的事件（例如进程重启后事件已丢失）
                # 仍发出恢复通知，但不带事件汇总信息
                events.append(self._build_recovery_event(result, recovery_intent, None))
            else:
                snapshot = await IncidentService.resolve_incident(incident["id"])
                events.append(self._build_recovery_event(result, recovery_intent, snapshot))
                logger.info(
                    f"Alert triggered: node_recovery for {node_name} "
                    f"(incident={incident['id']}, duration={snapshot.get('duration_seconds') if snapshot else None}s)"
                )

            # 事件结束，清除 node_down / latency 抑制，避免影响下一次故障。
            # 保留 node_recovery 的抑制：否则抖动节点每次上下线都会发恢复通知。
            await self.clear_suppression_async(node_name, keep={"node_recovery"})
            return events

        return events

    @staticmethod
    def _parse_hhmm(value: str) -> Optional[tuple]:
        """解析 "HH:MM" 为 (时, 分)，失败返回 None"""
        try:
            hh, mm = value.split(":")
            h, m = int(hh), int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return (h, m)
        except (ValueError, AttributeError):
            pass
        return None

    def _in_quiet_hours(
        self,
        now: Optional[datetime] = None,
        assume_utc: bool = True,
    ) -> bool:
        """
        判断当前是否处于静默时段

        支持跨夜区间（如 23:00-07:00）。配置非法时视为不静默，
        宁可打扰也不要因为配置错误而漏掉整晚的告警。

        Args:
            now: 待判定的时间，默认取当前 UTC 时间
            assume_utc: now 是否为 UTC 朴素时间（心跳时间戳的约定）。
                        传 False 表示已是本地时间，跳过转换。
        """
        if not self._config.quiet_hours_enabled:
            return False

        start = self._parse_hhmm(self._config.quiet_hours_start)
        end = self._parse_hhmm(self._config.quiet_hours_end)
        if not start or not end:
            logger.warning(
                f"静默时段配置非法（{self._config.quiet_hours_start}-"
                f"{self._config.quiet_hours_end}），本次不静默"
            )
            return False

        if start == end:
            # 起止相同视为未启用
            return False

        # 静默时段是用户按本地时刻配置的，必须先做时区转换。
        # 直接用 UTC 比较会让静默窗口整体错位（UTC+8 上偏 8 小时）。
        local_now = to_local_time(
            now if now is not None else datetime.utcnow(),
            assume_utc=assume_utc,
        )
        if local_now is None:
            return False
        cur = (local_now.hour, local_now.minute)

        if start < end:
            # 同日区间，如 09:00-18:00
            return start <= cur < end
        # 跨夜区间，如 23:00-07:00
        return cur >= start or cur < end

    def _quiet_hours_end_datetime(
        self,
        now: Optional[datetime] = None,
        assume_utc: bool = True,
    ) -> Optional[datetime]:
        """
        计算当前静默时段的结束时刻（用于把提醒顺延到该时刻）

        不在静默时段时返回 None，避免调用方把提醒顺延到一个无关的未来时刻。

        返回值与传入的 now 采用同一时间基准（均为 UTC 朴素时间），
        这样闸门比较不会因时区差异而失效。
        """
        if not self._config.quiet_hours_enabled:
            return None

        end = self._parse_hhmm(self._config.quiet_hours_end)
        start = self._parse_hhmm(self._config.quiet_hours_start)
        if not end or not start:
            return None

        base = now if now is not None else datetime.utcnow()

        # 非静默时段没有"结束时刻"可言
        if not self._in_quiet_hours(base, assume_utc=assume_utc):
            return None

        # 按本地时刻计算结束点，再换算回与入参一致的基准
        local_now = to_local_time(base, assume_utc=assume_utc)
        local_end = local_now.replace(
            hour=end[0], minute=end[1], second=0, microsecond=0
        )

        if start < end:
            # 同日区间（如 09:00-18:00）：结束时刻就在今天
            if local_now >= local_end:
                return None
        else:
            # 跨夜区间（如 23:00-07:00）：
            #   凌晨 02:00 -> 今天 07:00
            #   深夜 23:30 -> 明天 07:00
            if local_now >= local_end:
                local_end = local_end + timedelta(days=1)

        if assume_utc:
            # 换算回 UTC 朴素时间，与 now 的基准保持一致
            return local_end.astimezone(timezone.utc).replace(tzinfo=None)
        return local_end

    def _elapsed_for_seq(self, seq: int) -> int:
        """
        根据提醒序号反推"距事件开始的大致分钟数"

        取阶梯中第 seq 档对应的时间点。这样升级阈值可以按实际阶梯
        判断，而不是假设"每次提醒间隔相等"（阶梯是递减的：
        立即/1h/5h/13h/25h）。
        """
        intervals = self._config.reminder_intervals or (0,)
        if seq <= 0:
            return 0
        if seq <= len(intervals):
            return intervals[seq - 1]
        # 超出阶梯长度：最后一个刻度 + 循环间隔
        overflow = seq - len(intervals)
        return intervals[-1] + self._config.intervals_loop_minutes * overflow

    def is_escalated(self, event: AlertEvent) -> bool:
        """
        判断该告警是否已进入升级状态

        升级的含义：故障持续时间超过阈值，后续提醒应当更醒目，
        且不再受每日发送上限约束——此时"避免打扰"要让位于"确保送达"。
        """
        threshold = self._config.escalate_after_minutes
        if not threshold or threshold <= 0:
            return False

        # 恢复通知本身不升级（它是好消息，且用于关闭事件）
        if event.alert_type == "node_recovery":
            return False

        # 优先用事件的实际持续时长（恢复通知带此信息）
        if event.incident_duration_s is not None:
            return (event.incident_duration_s / 60) >= threshold

        # 否则按提醒序号反推
        return self._elapsed_for_seq(event.notify_seq or 0) >= threshold

    def _build_down_event(
        self,
        result: HeartbeatResult,
        intent: dict,
        incident: Optional[dict],
        seq: Optional[int],
    ) -> AlertEvent:
        """构造节点离线告警事件"""
        node_name = result.node_name
        failures = intent.get("consecutive_failures", 0)

        if seq and seq > 1:
            # 重复提醒：正文体现增量信息，而不是复制第一封
            started = incident.get("started_at") if incident else None
            duration = self._format_duration(started, now=result.timestamp) if started else "未知"
            message = (
                f"节点 {node_name} 持续离线（第 {seq} 次提醒）\n"
                f"已持续: {duration}"
            )
        else:
            message = f"节点 {node_name} 连续失败 {failures} 次"

        return AlertEvent(
            alert_type="node_down",
            node_name=node_name,
            node_url=result.url,
            message=message,
            details={
                "consecutive_failures": failures,
                "error_type": intent.get("error_type"),
                "error_message": intent.get("error_message"),
                "status_code": intent.get("status_code"),
            },
            incident_id=incident.get("id") if incident else None,
            notify_seq=seq,
        )

    def _build_recovery_event(
        self,
        result: HeartbeatResult,
        intent: dict,
        snapshot: Optional[dict],
    ) -> AlertEvent:
        """构造节点恢复告警事件"""
        node_name = result.node_name
        previous_failures = intent.get("previous_failures", 0)

        duration_text = ""
        if snapshot and snapshot.get("duration_seconds") is not None:
            duration_text = self._format_duration_seconds(snapshot["duration_seconds"])

        # 有事件时以事件自身记录的失败次数为准，与告警页展示的口径保持一致；
        # 无事件（如进程重启后事件丢失）才退回内存计数器。
        event_failures = None
        if snapshot and snapshot.get("total_failures"):
            event_failures = snapshot["total_failures"]

        if event_failures:
            message = (
                f"节点 {node_name} 已恢复正常，"
                f"本次故障累计失败 {event_failures} 次"
            )
        else:
            message = f"节点 {node_name} 已恢复正常，之前连续失败 {previous_failures} 次"
        if duration_text:
            message += f"\n故障持续: {duration_text}"

        return AlertEvent(
            alert_type="node_recovery",
            node_name=node_name,
            node_url=result.url,
            message=message,
            details={
                "previous_failures": previous_failures,
                "latency_ms": intent.get("latency_ms"),
                "status_code": intent.get("status_code"),
            },
            incident_id=snapshot.get("id") if snapshot else None,
            incident_duration_s=snapshot.get("duration_seconds") if snapshot else None,
            incident_notify_count=snapshot.get("notify_count") if snapshot else None,
        )

    @staticmethod
    def _format_duration(started_at, now: Optional[datetime] = None) -> str:
        """把事件开始时间格式化为"已持续 Xh Ym" """
        if not started_at:
            return "未知"
        if isinstance(started_at, str):
            try:
                started_at = datetime.fromisoformat(started_at)
            except ValueError:
                return "未知"
        now = now or datetime.now()
        return AlertingService._format_duration_seconds(
            int((now - started_at).total_seconds())
        )

    @staticmethod
    def _format_duration_seconds(seconds: int) -> str:
        """秒数格式化为中文时长"""
        return _format_duration_cn(seconds)

    async def evaluate_batch(self, results: list[HeartbeatResult]) -> list[AlertEvent]:
        """
        批量评估心跳结果
        
        Args:
            results: 心跳检测结果列表
            
        Returns:
            所有需要触发的告警事件列表
        """
        all_events = []
        for result in results:
            events = await self.evaluate(result)
            all_events.extend(events)
        return all_events
    
    def get_node_state(self, node_name: str) -> Optional[dict]:
        """获取节点告警状态"""
        with self._lock:
            state = self._states.get(node_name)
            if not state:
                return None
            
            return {
                "node_name": state.node_name,
                "consecutive_failures": state.consecutive_failures,
                "last_success": state.last_success,
                "last_check_at": state.last_check_at.isoformat() if state.last_check_at else None,
                "suppressed_alerts": {
                    k: v.isoformat() for k, v in state.suppressed_alerts.items()
                },
            }
    
    def get_all_states(self) -> dict:
        """获取所有节点的告警状态"""
        with self._lock:
            return {
                name: self.get_node_state(name)
                for name in self._states
            }
    
    def clear_suppression(
        self,
        node_name: str,
        alert_type: Optional[str] = None,
        keep: Optional[set] = None,
    ):
        """
        清除告警抑制（仅内存部分）

        持久化的抑制状态由 clear_suppression_async 负责清理。
        保留此同步方法以兼容既有调用方。
        """
        keep = keep or set()
        with self._lock:
            state = self._states.get(node_name)
            if state:
                if alert_type:
                    if alert_type not in keep:
                        state.suppressed_alerts.pop(alert_type, None)
                else:
                    for k in [k for k in state.suppressed_alerts if k not in keep]:
                        state.suppressed_alerts.pop(k, None)
                logger.info(f"Cleared suppression for {node_name}: {alert_type or 'all'}")

    async def clear_suppression_async(
        self,
        node_name: str,
        alert_type: Optional[str] = None,
        keep: Optional[set] = None,
    ):
        """
        清除告警抑制（内存 + Redis 持久化）

        Args:
            node_name: 节点名称
            alert_type: 告警类型，留空则清除该节点所有类型
            keep: 需要保留的告警类型集合。节点恢复时传入 {"node_recovery"}，
                  否则会把刚设置的恢复抑制一起清掉，使抖动节点的恢复通知
                  失去去重保护、每次上下线都发一封。
        """
        keep = keep or set()
        self.clear_suppression(node_name, alert_type, keep=keep)

        if alert_type:
            targets = [alert_type]
        else:
            targets = ["node_down", "node_recovery", "latency_high"]
        for at in targets:
            if at in keep:
                continue
            await self._clear_persisted_suppression(node_name, at)

    async def _clear_persisted_suppression(self, node_name: str, alert_type: str):
        """删除 Redis 中的抑制记录，失败不抛异常"""
        if not self._config.suppress_persist:
            return

        try:
            from ..repositories.redis_repository import get_redis_repository
            repo = get_redis_repository()
            await repo.clear_alert_suppression(
                self._suppression_key(node_name, alert_type)
            )
        except Exception as e:
            logger.debug(f"Failed to clear persisted suppression: {e}")


# 全局告警服务实例
_alerting_service: Optional[AlertingService] = None


def get_alerting_service() -> AlertingService:
    """获取告警服务实例"""
    global _alerting_service
    if _alerting_service is None:
        _alerting_service = AlertingService()
    return _alerting_service


class AlertLogService:
    """告警记录服务 - 管理告警历史记录"""
    
    @staticmethod
    async def add_log(
        node_name: str,
        node_url: str,
        alert_type: str,
        message: str,
        details: Optional[Dict] = None,
        incident_id: Optional[int] = None,
        notify_seq: Optional[int] = None,
    ) -> dict:
        """添加告警记录"""
        async with get_session_context() as session:
            log = AlertLogModel(
                node_name=node_name,
                node_url=node_url,
                alert_type=alert_type,
                message=message,
                details=json.dumps(details, ensure_ascii=False) if details else None,
                incident_id=incident_id,
                notify_seq=notify_seq,
                triggered_at=datetime.now(),
            )
            session.add(log)
            await session.flush()
            await session.refresh(log)
            logger.info(f"告警日志已创建: id={log.id}, 节点={node_name}, 类型={alert_type}")
            return log.to_dict()
    
    @staticmethod
    async def update_notification_status(
        alert_id: int,
        notification_sent: bool,
        notification_channels: List[str],
        notification_count: int,
    ):
        """更新告警的通知状态"""
        async with get_session_context() as session:
            result = await session.execute(
                select(AlertLogModel).where(AlertLogModel.id == alert_id)
            )
            log = result.scalar_one_or_none()
            if log:
                log.notification_sent = notification_sent
                log.notification_channels = ",".join(notification_channels)
                log.notification_count = notification_count
    
    @staticmethod
    async def get_logs(
        node_name: Optional[str] = None,
        alert_type: Optional[str] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[dict], int]:
        """获取告警记录列表"""
        async with get_session_context() as session:
            query = select(AlertLogModel)
            count_query = select(func.count(AlertLogModel.id))
            
            # 应用过滤条件
            conditions = []
            if node_name:
                conditions.append(AlertLogModel.node_name == node_name)
            if alert_type:
                conditions.append(AlertLogModel.alert_type == alert_type)
            if status:
                conditions.append(AlertLogModel.status == status)
            if start_date:
                conditions.append(AlertLogModel.triggered_at >= start_date)
            if end_date:
                conditions.append(AlertLogModel.triggered_at <= end_date)
            
            if conditions:
                query = query.where(and_(*conditions))
                count_query = count_query.where(and_(*conditions))
            
            # 获取总数
            count_result = await session.execute(count_query)
            total = count_result.scalar() or 0
            
            # 获取记录
            query = query.order_by(AlertLogModel.triggered_at.desc()).limit(limit).offset(offset)
            result = await session.execute(query)
            logs = result.scalars().all()
            
            return [log.to_dict() for log in logs], total
    
    @staticmethod
    async def get_by_id(alert_id: int) -> Optional[dict]:
        """根据ID获取告警记录"""
        async with get_session_context() as session:
            result = await session.execute(
                select(AlertLogModel).where(AlertLogModel.id == alert_id)
            )
            log = result.scalar_one_or_none()
            return log.to_dict() if log else None
    
    @staticmethod
    async def acknowledge(alert_id: int, acknowledged_by: str = "admin") -> Optional[dict]:
        """确认告警"""
        async with get_session_context() as session:
            result = await session.execute(
                select(AlertLogModel).where(AlertLogModel.id == alert_id)
            )
            log = result.scalar_one_or_none()
            if log:
                log.status = "acknowledged"
                log.acknowledged_at = datetime.now()
                log.acknowledged_by = acknowledged_by
                await session.flush()
                await session.refresh(log)
                return log.to_dict()
            return None
    
    @staticmethod
    async def resolve(alert_id: int) -> Optional[dict]:
        """解决告警"""
        async with get_session_context() as session:
            result = await session.execute(
                select(AlertLogModel).where(AlertLogModel.id == alert_id)
            )
            log = result.scalar_one_or_none()
            if log:
                log.status = "resolved"
                log.resolved_at = datetime.now()
                await session.flush()
                await session.refresh(log)
                return log.to_dict()
            return None
    
    @staticmethod
    async def get_stats() -> dict:
        """获取告警统计"""
        async with get_session_context() as session:
            # 今日告警
            today_start = datetime.combine(datetime.today(), datetime.min.time())
            
            today_result = await session.execute(
                select(func.count(AlertLogModel.id)).where(
                    AlertLogModel.triggered_at >= today_start
                )
            )
            today_total = today_result.scalar() or 0
            
            # 活跃告警
            active_result = await session.execute(
                select(func.count(AlertLogModel.id)).where(
                    AlertLogModel.status == "active"
                )
            )
            active_count = active_result.scalar() or 0
            
            # 各类型统计
            type_stats = {}
            for alert_type in ["node_down", "node_recovery", "latency_high"]:
                type_result = await session.execute(
                    select(func.count(AlertLogModel.id)).where(
                        and_(
                            AlertLogModel.alert_type == alert_type,
                            AlertLogModel.triggered_at >= today_start
                        )
                    )
                )
                type_stats[alert_type] = type_result.scalar() or 0
            
            return {
                "today_total": today_total,
                "active_count": active_count,
                "by_type": type_stats,
            }
    
    @staticmethod
    async def delete(alert_id: int) -> bool:
        """删除单个告警记录"""
        async with get_session_context() as session:
            result = await session.execute(
                select(AlertLogModel).where(AlertLogModel.id == alert_id)
            )
            log = result.scalar_one_or_none()
            if log:
                await session.delete(log)
                return True
            return False
    
    @staticmethod
    async def batch_delete(ids: List[int]) -> int:
        """批量删除告警记录"""
        if not ids:
            return 0
        
        async with get_session_context() as session:
            from sqlalchemy import delete
            stmt = delete(AlertLogModel).where(AlertLogModel.id.in_(ids))
            result = await session.execute(stmt)
            return result.rowcount


async def process_alerts(events: list[AlertEvent]):
    """
    处理告警事件 - 先写入告警记录，再发送通知
    
    Args:
        events: 告警事件列表
    """
    if not events:
        return
    
    from .notification import NotificationService
    from .config import get_config
    
    # 获取告警配置
    config = get_config()
    
    for event in events:
        try:
            # Step 1: 写入告警记录
            alert_log = await AlertLogService.add_log(
                node_name=event.node_name,
                node_url=event.node_url,
                alert_type=event.alert_type,
                message=event.message,
                details=event.details,
                incident_id=event.incident_id,
                notify_seq=event.notify_seq,
            )
            alert_id = alert_log.get("id")
            logger.info(
                f"Alert logged: id={alert_id}, type={event.alert_type}, "
                f"node={event.node_name}, incident={event.incident_id}, seq={event.notify_seq}"
            )
            
            # 检查节点恢复通知全局开关
            if event.alert_type == "node_recovery" and not config.alerting.notify_on_recovery:
                logger.info(f"Skipping node_recovery notification for {event.node_name} (notify_on_recovery=False)")
                continue
            
            # Step 2: 发送通知到所有启用的渠道
            notified_channels = []
            try:
                channels = await NotificationService.get_enabled_channels()
                for channel in channels:
                    # 检查通知类型是否匹配
                    if event.alert_type == "node_down" and not channel.get("notify_on_down"):
                        continue
                    if event.alert_type == "node_recovery" and not channel.get("notify_on_recovery"):
                        continue
                    if event.alert_type == "latency_high" and not channel.get("notify_on_latency"):
                        continue
                    
                    # 发送通知
                    type_names = {
                        "node_down": "节点离线",
                        "node_recovery": "节点恢复",
                        "latency_high": "延迟告警",
                    }
                    type_name = type_names.get(event.alert_type, "告警")

                    # 升级判定：故障持续超过阈值后，提醒更醒目且跳过每日上限
                    escalated = get_alerting_service().is_escalated(event)

                    # 第 N 次提醒：主题里标明序号，避免被当成新故障
                    seq = event.notify_seq or 0
                    prefix = "[升级]" if escalated else ""
                    if event.alert_type == "node_down" and seq > 1:
                        subject = f"[服务器监控]{prefix} {type_name}(第{seq}次提醒): {event.node_name}"
                    elif event.alert_type == "node_recovery":
                        subject = f"[服务器监控] {type_name}: {event.node_name} 已恢复"
                    else:
                        subject = f"[服务器监控]{prefix} {type_name}: {event.node_name}"

                    body = f"""
告警类型: {type_name}{f'（第 {seq} 次提醒）' if seq > 1 else ''}
节点名称: {event.node_name}
节点URL: {event.node_url}
触发时间: {event.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}
"""
                    if escalated:
                        body += (
                            "⚠️ 该故障已持续较长时间仍未恢复，"
                            "本条为升级提醒（不受每日发送上限限制）\n"
                        )
                    # 恢复通知：附上故障期间的汇总
                    if event.alert_type == "node_recovery":
                        if event.incident_duration_s is not None:
                            body += f"故障时长: {_format_duration_cn(event.incident_duration_s)}\n"
                        if event.incident_notify_count:
                            body += f"期间提醒: {event.incident_notify_count} 次\n"
                        if event.incident_id:
                            body += f"事件编号: #{event.incident_id}\n"

                    body += f"\n告警详情:\n{event.message}\n"

                    # 详情里过滤掉内部字段，避免把 next_notify_at 这类
                    # 实现细节直接暴露给收件人
                    INTERNAL_KEYS = {"next_notify_at", "incident_started_at", "incident_total_failures"}
                    if event.details:
                        public = {k: v for k, v in event.details.items() if k not in INTERNAL_KEYS}
                        if public:
                            body += "\n附加信息:\n"
                            for key, value in public.items():
                                body += f"  - {key}: {value}\n"

                    # 告知下次提醒节奏，减少"为什么不再提醒了"的疑问
                    nxt = event.details.get("next_notify_at")
                    if event.alert_type == "node_down" and nxt:
                        body += f"\n下次提醒: {nxt}（若节点仍未恢复）\n恢复后将立即发送恢复通知。\n"
                    elif event.alert_type == "node_recovery":
                        body += "\n该故障事件已关闭。\n"
                    
                    success, error = await NotificationService.send_notification(
                        channel_id=channel["id"],
                        subject=subject,
                        message=body,
                        alert_type=event.alert_type,
                        node_name=event.node_name,
                        node_url=event.node_url,
                        escalated=escalated,
                    )
                    
                    if success:
                        notified_channels.append(channel["name"])
                        logger.info(f"告警通知已通过渠道 '{channel['name']}' 发送成功，跳过后续备用渠道: {event.alert_type} - {event.node_name}")
                        # 实现冗余逻辑：只要有一个渠道发送成功，就不再尝试后续渠道
                        break
                    else:
                        logger.warning(f"告警通知通过渠道 '{channel['name']}' 发送失败，将尝试下一个可用渠道: {error}")
                
                # Step 3: 更新告警记录的通知状态
                if alert_id and notified_channels:
                    await AlertLogService.update_notification_status(
                        alert_id=alert_id,
                        notification_sent=True,
                        notification_channels=notified_channels,
                        notification_count=len(notified_channels),
                    )
                    logger.info(f"Alert {alert_id} notification status updated: {len(notified_channels)} channels notified")
                
            except Exception as e:
                logger.error(f"发送告警 {alert_id} 的通知失败: {e}")
            
        except Exception as e:
            logger.error(f"处理告警事件失败: {e}")
