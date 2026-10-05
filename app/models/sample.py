"""样本 ORM 模型"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, Text, Index
from sqlalchemy.sql import func

from .base import Base


class SampleModel(Base):
    """心跳样本表（单次检测结果）"""
    __tablename__ = "samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(String(100), nullable=False, index=True, comment="节点名称")
    url = Column(String(500), nullable=False, comment="请求 URL")
    timestamp = Column(DateTime, default=func.now(), index=True, comment="采样时间")
    
    # 响应信息
    status_code = Column(Integer, default=0, comment="HTTP 状态码")
    latency_ms = Column(Float, nullable=False, comment="响应延迟(毫秒)")
    success = Column(Boolean, default=True, index=True, comment="是否成功")
    error_type = Column(String(100), nullable=True, comment="错误类型")
    error_message = Column(Text, nullable=True, comment="错误信息")
    response_size = Column(Integer, nullable=True, comment="响应体大小(字节)")
    
    # 阶段性耗时（可选）
    dns_ms = Column(Float, nullable=True, comment="DNS 解析耗时")
    tcp_ms = Column(Float, nullable=True, comment="TCP 连接耗时")
    tls_ms = Column(Float, nullable=True, comment="TLS 握手耗时")
    ttfb_ms = Column(Float, nullable=True, comment="首字节时间")

    # 复合索引用于查询优化
    __table_args__ = (
        Index("ix_samples_node_timestamp", "node_name", "timestamp"),
        Index("ix_samples_timestamp_success", "timestamp", "success"),
    )

    def __repr__(self):
        return f"<SampleModel(node={self.node_name}, status={self.status_code}, latency={self.latency_ms}ms)>"
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "node_name": self.node_name,
            "url": self.url,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "response_size": self.response_size,
            "dns_ms": self.dns_ms,
            "tcp_ms": self.tcp_ms,
            "tls_ms": self.tls_ms,
            "ttfb_ms": self.ttfb_ms,
        }


class AggregateModel(Base):
    """聚合快照表（窗口统计）"""
    __tablename__ = "aggregates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(String(100), nullable=False, index=True, comment="节点名称")
    window_start = Column(DateTime, nullable=False, comment="窗口开始时间")
    window_end = Column(DateTime, nullable=False, comment="窗口结束时间")
    window_minutes = Column(Integer, default=5, comment="窗口大小(分钟)")
    
    # 计数
    total_count = Column(Integer, default=0, comment="总请求数")
    success_count = Column(Integer, default=0, comment="成功数")
    error_count = Column(Integer, default=0, comment="失败数")
    error_rate = Column(Float, default=0.0, comment="错误率")
    
    # 延迟统计
    latency_min_ms = Column(Float, nullable=True, comment="最小延迟")
    latency_max_ms = Column(Float, nullable=True, comment="最大延迟")
    latency_avg_ms = Column(Float, nullable=True, comment="平均延迟")
    latency_p50_ms = Column(Float, nullable=True, comment="p50 延迟")
    latency_p95_ms = Column(Float, nullable=True, comment="p95 延迟")
    latency_p99_ms = Column(Float, nullable=True, comment="p99 延迟")
    
    # 可用性
    availability = Column(Float, default=1.0, comment="可用性")
    
    # 元数据
    created_at = Column(DateTime, default=func.now(), comment="创建时间")

    # 复合索引
    __table_args__ = (
        Index("ix_aggregates_node_window", "node_name", "window_start"),
    )

    def __repr__(self):
        return f"<AggregateModel(node={self.node_name}, window={self.window_start})>"
