"""可用性统计服务 - 基于历史心跳记录计算可用性"""

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatRecord:
    """心跳记录"""
    node_name: str
    timestamp: datetime
    success: bool
    latency_ms: float
    status_code: int = 0
    error_type: Optional[str] = None


@dataclass
class AvailabilityStats:
    """可用性统计结果"""
    node_name: str
    uptime_24h: float = 0.0  # 24小时可用性百分比
    uptime_7d: float = 0.0   # 7天可用性百分比
    uptime_30d: float = 0.0  # 30天可用性百分比
    total_checks_24h: int = 0
    success_checks_24h: int = 0
    avg_latency_ms: float = 0.0
    last_check_at: Optional[datetime] = None
    last_success: bool = True
    current_status: str = "unknown"  # up, down, unknown


@dataclass
class HourlyStats:
    """每小时统计（用于趋势图）"""
    hour: datetime
    total: int = 0
    success: int = 0
    
    @property
    def availability(self) -> float:
        if self.total == 0:
            return 100.0
        return (self.success / self.total) * 100.0
    
    @property
    def status(self) -> str:
        """返回状态: up(>99%), degraded(>90%), down(<=90%)"""
        avail = self.availability
        if avail >= 99.0:
            return "up"
        elif avail >= 90.0:
            return "degraded"
        else:
            return "down"


class AvailabilityService:
    """可用性统计服务
    
    内存优化策略:
    - 默认只保留 48 小时数据，足够支持趋势图显示
    - 7天/30天可用性从数据库 aggregates 表读取（如有实现）
    - 定期清理过期记录
    """
    
    # 最大保留记录数（每分钟1条，保留 48 小时 = 2880 条）
    # 内存估算: 100节点 * 2880条 * 100字节/条 ≈ 28MB
    MAX_RECORDS_PER_NODE = 2880
    
    # 数据保留小时数
    RETENTION_HOURS = 48
    
    def __init__(self):
        # 节点 -> 心跳记录队列
        self._records: dict[str, deque[HeartbeatRecord]] = {}
        # 线程锁
        self._lock = threading.RLock()
        # 最新状态缓存
        self._latest_status: dict[str, AvailabilityStats] = {}
        # 上次清理时间
        self._last_cleanup = datetime.utcnow()
        # 清理间隔（小时）
        self._cleanup_interval_hours = 1
    
    def add_record(self, record: HeartbeatRecord):
        """添加心跳记录"""
        with self._lock:
            if record.node_name not in self._records:
                self._records[record.node_name] = deque(maxlen=self.MAX_RECORDS_PER_NODE)
            self._records[record.node_name].append(record)
            
            # 更新缓存
            self._update_cache(record.node_name)
            
            # 定期清理过期记录
            self._maybe_cleanup()
    
    def add_records(self, records: list[HeartbeatRecord]):
        """批量添加心跳记录"""
        with self._lock:
            for record in records:
                if record.node_name not in self._records:
                    self._records[record.node_name] = deque(maxlen=self.MAX_RECORDS_PER_NODE)
                self._records[record.node_name].append(record)
            
            # 更新所有相关节点的缓存
            node_names = set(r.node_name for r in records)
            for name in node_names:
                self._update_cache(name)
            
            # 定期清理过期记录
            self._maybe_cleanup()
    
    def _maybe_cleanup(self):
        """检查并执行定期清理（已持有锁）"""
        now = datetime.utcnow()
        if (now - self._last_cleanup).total_seconds() >= self._cleanup_interval_hours * 3600:
            self._cleanup_expired_records()
            self._last_cleanup = now
    
    def _cleanup_expired_records(self):
        """清理过期记录（已持有锁）"""
        cutoff = datetime.utcnow() - timedelta(hours=self.RETENTION_HOURS)
        total_removed = 0
        
        for node_name, records in list(self._records.items()):
            # 过滤过期记录
            original_count = len(records)
            # deque 不支持直接过滤，需要重建
            new_records = deque(
                (r for r in records if r.timestamp >= cutoff),
                maxlen=self.MAX_RECORDS_PER_NODE
            )
            removed = original_count - len(new_records)
            if removed > 0:
                self._records[node_name] = new_records
                total_removed += removed
        
        if total_removed > 0:
            logger.info(f"Cleaned up {total_removed} expired availability records")
    
    def get_memory_stats(self) -> dict:
        """获取内存使用统计（用于监控）"""
        with self._lock:
            total_records = sum(len(records) for records in self._records.values())
            return {
                "node_count": len(self._records),
                "total_records": total_records,
                "max_records_per_node": self.MAX_RECORDS_PER_NODE,
                "retention_hours": self.RETENTION_HOURS,
                "estimated_memory_mb": round(total_records * 100 / 1024 / 1024, 2),
            }
    
    def _update_cache(self, node_name: str):
        """更新节点可用性缓存"""
        stats = self._calculate_stats(node_name)
        self._latest_status[node_name] = stats
    
    def _calculate_stats(self, node_name: str) -> AvailabilityStats:
        """计算节点可用性统计"""
        records = self._records.get(node_name)
        if not records:
            return AvailabilityStats(node_name=node_name)
        
        now = datetime.utcnow()
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)
        cutoff_30d = now - timedelta(days=30)
        
        # 按时间窗口分组统计
        stats_24h = {"total": 0, "success": 0, "latency_sum": 0.0}
        stats_7d = {"total": 0, "success": 0}
        stats_30d = {"total": 0, "success": 0}
        
        last_record: Optional[HeartbeatRecord] = None
        
        for record in records:
            # 30天内
            if record.timestamp >= cutoff_30d:
                stats_30d["total"] += 1
                if record.success:
                    stats_30d["success"] += 1
                
                # 7天内
                if record.timestamp >= cutoff_7d:
                    stats_7d["total"] += 1
                    if record.success:
                        stats_7d["success"] += 1
                    
                    # 24小时内
                    if record.timestamp >= cutoff_24h:
                        stats_24h["total"] += 1
                        stats_24h["latency_sum"] += record.latency_ms
                        if record.success:
                            stats_24h["success"] += 1
            
            last_record = record
        
        # 计算百分比
        def calc_percentage(success: int, total: int) -> float:
            if total == 0:
                return 100.0  # 无数据时默认100%
            return round((success / total) * 100, 3)
        
        # 确定当前状态
        current_status = "unknown"
        last_success = True
        if last_record:
            last_success = last_record.success
            current_status = "up" if last_record.success else "down"
        
        return AvailabilityStats(
            node_name=node_name,
            uptime_24h=calc_percentage(stats_24h["success"], stats_24h["total"]),
            uptime_7d=calc_percentage(stats_7d["success"], stats_7d["total"]),
            uptime_30d=calc_percentage(stats_30d["success"], stats_30d["total"]),
            total_checks_24h=stats_24h["total"],
            success_checks_24h=stats_24h["success"],
            avg_latency_ms=round(stats_24h["latency_sum"] / max(stats_24h["total"], 1), 2),
            last_check_at=last_record.timestamp if last_record else None,
            last_success=last_success,
            current_status=current_status,
        )
    
    def get_stats(self, node_name: str) -> AvailabilityStats:
        """获取节点可用性统计"""
        with self._lock:
            if node_name in self._latest_status:
                return self._latest_status[node_name]
            return AvailabilityStats(node_name=node_name)
    
    def get_all_stats(self) -> dict[str, AvailabilityStats]:
        """获取所有节点可用性统计"""
        with self._lock:
            return dict(self._latest_status)
    
    def remove_node(self, node_name: str):
        """移除节点的所有历史数据"""
        with self._lock:
            if node_name in self._records:
                del self._records[node_name]
                logger.info(f"Removed availability records for node: {node_name}")
            if node_name in self._latest_status:
                del self._latest_status[node_name]
                logger.info(f"Removed availability status cache for node: {node_name}")
    
    def get_hourly_trend(self, node_name: str, hours: int = 24) -> list[HourlyStats]:
        """
        获取节点的每小时可用性趋势
        
        Args:
            node_name: 节点名称
            hours: 查询小时数（默认24小时）
        
        Returns:
            每小时统计列表（精确返回 hours 个数据点，从旧到新）
        """
        with self._lock:
            records = self._records.get(node_name)
            if not records:
                return []
            
            now = datetime.utcnow()
            # 当前小时的开始时间
            current_hour = now.replace(minute=0, second=0, microsecond=0)
            # 起始小时（hours 小时前，不包含当前小时，所以是 hours-1）
            start_hour = current_hour - timedelta(hours=hours-1)
            cutoff = now - timedelta(hours=hours)
            
            # 按小时分组
            hourly_data: dict[str, HourlyStats] = {}
            
            for record in records:
                if record.timestamp < cutoff:
                    continue
                
                # 取整到小时
                hour_key = record.timestamp.replace(minute=0, second=0, microsecond=0)
                hour_str = hour_key.isoformat()
                
                if hour_str not in hourly_data:
                    hourly_data[hour_str] = HourlyStats(hour=hour_key)
                
                hourly_data[hour_str].total += 1
                if record.success:
                    hourly_data[hour_str].success += 1
            
            # 精确生成 hours 个数据点
            result = []
            current = start_hour
            for _ in range(hours):
                hour_str = current.isoformat()
                if hour_str in hourly_data:
                    result.append(hourly_data[hour_str])
                else:
                    # 无数据的小时，显示为未知
                    result.append(HourlyStats(hour=current, total=0, success=0))
                current += timedelta(hours=1)
            
            return result
    
    def get_daily_trend(self, node_name: str, days: int = 30) -> list[dict]:
        """
        获取节点的每天可用性趋势
        
        Args:
            node_name: 节点名称  
            days: 查询天数（默认30天）
        
        Returns:
            每天统计列表
        """
        with self._lock:
            records = self._records.get(node_name)
            if not records:
                return []
            
            now = datetime.utcnow()
            cutoff = now - timedelta(days=days)
            
            # 按天分组
            daily_data: dict[str, dict] = {}
            
            for record in records:
                if record.timestamp < cutoff:
                    continue
                
                day_key = record.timestamp.strftime("%Y-%m-%d")
                
                if day_key not in daily_data:
                    daily_data[day_key] = {"date": day_key, "total": 0, "success": 0}
                
                daily_data[day_key]["total"] += 1
                if record.success:
                    daily_data[day_key]["success"] += 1
            
            # 排序并计算可用性
            result = []
            for day in sorted(daily_data.keys()):
                data = daily_data[day]
                avail = (data["success"] / data["total"] * 100) if data["total"] > 0 else 100.0
                result.append({
                    "date": data["date"],
                    "total": data["total"],
                    "success": data["success"],
                    "availability": round(avail, 3),
                    "status": "up" if avail >= 99 else ("degraded" if avail >= 90 else "down"),
                })
            
            return result
    
    def get_latency_trend(self, node_name: str, hours: int = 24) -> list[dict]:
        """
        获取节点的延迟趋势数据
        
        Args:
            node_name: 节点名称
            hours: 查询小时数（默认24小时）
        
        Returns:
            每小时延迟统计列表（精确返回 hours 个数据点）
        """
        with self._lock:
            records = self._records.get(node_name)
            if not records:
                return []
            
            now = datetime.utcnow()
            # 当前小时的开始时间
            current_hour = now.replace(minute=0, second=0, microsecond=0)
            # 起始小时（hours 小时前）
            start_hour = current_hour - timedelta(hours=hours-1)
            cutoff = now - timedelta(hours=hours)
            
            # 按小时分组
            hourly_data: dict[str, dict] = {}
            
            for record in records:
                if record.timestamp < cutoff:
                    continue
                
                # 取整到小时
                hour_key = record.timestamp.replace(minute=0, second=0, microsecond=0)
                hour_str = hour_key.isoformat()
                
                if hour_str not in hourly_data:
                    hourly_data[hour_str] = {
                        "hour": hour_key,
                        "latencies": [],
                        "total": 0,
                        "success": 0,
                    }
                
                hourly_data[hour_str]["latencies"].append(record.latency_ms)
                hourly_data[hour_str]["total"] += 1
                if record.success:
                    hourly_data[hour_str]["success"] += 1
            
            # 精确生成 hours 个数据点
            result = []
            current = start_hour
            for _ in range(hours):
                hour_str = current.isoformat()
                
                if hour_str in hourly_data:
                    data = hourly_data[hour_str]
                    latencies = data["latencies"]
                    result.append({
                        "hour": current.isoformat() + "Z",
                        "avg_latency": round(sum(latencies) / len(latencies), 2) if latencies else 0,
                        "min_latency": round(min(latencies), 2) if latencies else 0,
                        "max_latency": round(max(latencies), 2) if latencies else 0,
                        "total": data["total"],
                        "success": data["success"],
                        "availability": round((data["success"] / data["total"]) * 100, 2) if data["total"] > 0 else 100,
                    })
                else:
                    # 无数据的小时
                    result.append({
                        "hour": current.isoformat() + "Z",
                        "avg_latency": 0,
                        "min_latency": 0,
                        "max_latency": 0,
                        "total": 0,
                        "success": 0,
                        "availability": 100,
                    })
                
                current += timedelta(hours=1)
            
            return result
    
    def get_all_latency_trends(self, hours: int = 24) -> dict[str, list[dict]]:
        """
        获取所有节点的延迟趋势
        
        Returns:
            {节点名: 延迟趋势列表}
        """
        with self._lock:
            result = {}
            for node_name in self._records.keys():
                result[node_name] = self.get_latency_trend(node_name, hours)
            return result


# 全局实例
_availability_service: Optional[AvailabilityService] = None


def get_availability_service() -> AvailabilityService:
    """获取可用性统计服务实例"""
    global _availability_service
    if _availability_service is None:
        _availability_service = AvailabilityService()
    return _availability_service
