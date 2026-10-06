"""可观测性模块 - Prometheus 指标与监控自身健康"""

import time
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field

from prometheus_client import Counter, Histogram, Gauge, Info, REGISTRY, generate_latest
from ..utils.timeutil import utcnow

logger = logging.getLogger(__name__)


@dataclass
class SchedulerStats:
    """调度器统计"""
    last_run_at: Optional[datetime] = None
    runs_total: int = 0
    errors_total: int = 0
    last_duration_seconds: float = 0.0
    status: str = "stopped"  # stopped, running, error


@dataclass
class StorageStats:
    """存储层统计"""
    mysql_status: str = "unknown"  # up, down, unknown
    mysql_latency_ms: float = 0.0
    redis_status: str = "unknown"
    redis_latency_ms: float = 0.0
    mysql_errors_total: int = 0
    redis_errors_total: int = 0


class Metrics:
    """Prometheus 指标管理"""
    
    def __init__(self):
        # 应用启动时间
        self._start_time = time.time()
        
        # 调度器与存储统计
        self.scheduler_stats = SchedulerStats()
        self.storage_stats = StorageStats()
        
        # ========== 心跳检测指标 ==========
        # 请求计数器
        self.http_requests_total = Counter(
            "http_requests_total",
            "Total HTTP requests made",
            ["node", "url", "region", "status", "error_type"],
        )
        
        # 请求延迟直方图
        self.http_request_duration_seconds = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["node", "region"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        
        # 节点可用性
        self.node_availability = Gauge(
            "node_availability",
            "Node availability (0 or 1)",
            ["node", "region"],
        )
        
        # ========== 监控自身指标 (P0) ==========
        # 进程运行时长
        self.monitor_uptime_seconds = Gauge(
            "monitor_uptime_seconds",
            "Monitor process uptime in seconds",
        )
        
        # 心跳任务执行次数
        self.monitor_heartbeat_runs_total = Counter(
            "monitor_heartbeat_runs_total",
            "Total heartbeat runs",
        )
        
        # 心跳任务异常次数
        self.monitor_heartbeat_errors_total = Counter(
            "monitor_heartbeat_errors_total",
            "Total heartbeat run errors",
        )
        
        # 单轮心跳执行耗时
        self.monitor_heartbeat_duration_seconds = Histogram(
            "monitor_heartbeat_duration_seconds",
            "Heartbeat run duration in seconds",
            buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
        )
        
        # 存储层延迟
        self.monitor_storage_latency_seconds = Gauge(
            "monitor_storage_latency_seconds",
            "Storage backend latency in seconds",
            ["backend"],  # mysql, redis
        )
        
        # 存储层错误计数
        self.monitor_storage_errors_total = Counter(
            "monitor_storage_errors_total",
            "Storage backend errors",
            ["backend"],
        )
        
        # 应用信息
        self.app_info = Info(
            "server_monitor",
            "Server monitor application info",
        )
    
    def set_app_info(self, version: str, name: str = "server-monitor"):
        """设置应用信息"""
        self.app_info.info({
            "version": version,
            "name": name,
        })
    
    def get_uptime_seconds(self) -> float:
        """获取进程运行时长"""
        return time.time() - self._start_time
    
    def update_uptime(self):
        """更新运行时长指标"""
        self.monitor_uptime_seconds.set(self.get_uptime_seconds())
    
    def record_heartbeat(
        self,
        node_name: str,
        url: str,
        region: str,
        status_code: int,
        latency_ms: float,
        success: bool,
        error_type: Optional[str] = None,
    ):
        """记录心跳检测结果"""
        # 请求计数
        self.http_requests_total.labels(
            node=node_name,
            url=url,
            region=region,
            status=str(status_code),
            error_type=error_type or "",
        ).inc()
        
        # 延迟直方图
        self.http_request_duration_seconds.labels(
            node=node_name,
            region=region,
        ).observe(latency_ms / 1000.0)
        
        # 可用性
        self.node_availability.labels(
            node=node_name,
            region=region,
        ).set(1 if success else 0)
    
    def record_heartbeat_run(self, duration_seconds: float, success: bool):
        """记录一轮心跳任务执行"""
        self.monitor_heartbeat_runs_total.inc()
        self.monitor_heartbeat_duration_seconds.observe(duration_seconds)
        
        if not success:
            self.monitor_heartbeat_errors_total.inc()
        
        # 更新调度器统计
        self.scheduler_stats.last_run_at = utcnow()
        self.scheduler_stats.runs_total += 1
        self.scheduler_stats.last_duration_seconds = duration_seconds
        if not success:
            self.scheduler_stats.errors_total += 1
    
    def record_storage_ping(self, backend: str, success: bool, latency_ms: float):
        """记录存储层 ping 结果"""
        self.monitor_storage_latency_seconds.labels(backend=backend).set(latency_ms / 1000.0)
        
        if not success:
            self.monitor_storage_errors_total.labels(backend=backend).inc()
        
        # 更新存储统计
        if backend == "mysql":
            self.storage_stats.mysql_status = "up" if success else "down"
            self.storage_stats.mysql_latency_ms = latency_ms
            if not success:
                self.storage_stats.mysql_errors_total += 1
        elif backend == "redis":
            self.storage_stats.redis_status = "up" if success else "down"
            self.storage_stats.redis_latency_ms = latency_ms
            if not success:
                self.storage_stats.redis_errors_total += 1
    
    def get_health_status(self) -> dict:
        """获取健康状态（用于 /healthz 接口）"""
        from .. import __version__
        
        # 更新运行时长
        self.update_uptime()
        
        # 判断整体状态
        components_status = {
            "mysql": {
                "status": self.storage_stats.mysql_status,
                "latency_ms": round(self.storage_stats.mysql_latency_ms, 2),
            },
            "redis": {
                "status": self.storage_stats.redis_status,
                "latency_ms": round(self.storage_stats.redis_latency_ms, 2),
            },
            "scheduler": {
                "status": self.scheduler_stats.status,
                "last_run_at": self.scheduler_stats.last_run_at.isoformat() if self.scheduler_stats.last_run_at else None,
                "runs_total": self.scheduler_stats.runs_total,
                "errors_total": self.scheduler_stats.errors_total,
            },
        }
        
        # 综合状态判断
        overall_status = "healthy"
        if self.storage_stats.mysql_status == "down":
            overall_status = "unhealthy"
        elif self.storage_stats.redis_status == "down":
            overall_status = "degraded"
        elif self.scheduler_stats.status == "error":
            overall_status = "degraded"
        
        return {
            "status": overall_status,
            "uptime_seconds": round(self.get_uptime_seconds(), 2),
            "version": __version__,
            "components": components_status,
        }
    
    def generate_metrics(self) -> bytes:
        """生成 Prometheus 指标文本"""
        self.update_uptime()
        return generate_latest(REGISTRY)


# 全局指标实例
metrics = Metrics()
