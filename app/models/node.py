"""节点 ORM 模型"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, Text, JSON
from sqlalchemy.sql import func

from .base import Base


class NodeModel(Base):
    """节点配置表"""
    __tablename__ = "nodes"
    __table_args__ = {"mysql_engine": "InnoDB"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True, comment="节点唯一名称")
    url = Column(String(500), nullable=False, comment="心跳检测 URL")
    region = Column(String(50), default="default", index=True, comment="区域标签")
    tags = Column(JSON, default=list, comment="自定义标签列表")
    timeout_ms = Column(Integer, default=2000, comment="超时时间(毫秒)")
    enabled = Column(Boolean, default=True, index=True, comment="是否启用")
    expect_status = Column(Integer, default=200, comment="期望状态码")
    expect_body_contains = Column(String(500), nullable=True, comment="期望响应体包含")
    
    # 实时状态（缓存）
    last_status_code = Column(Integer, nullable=True, comment="最近状态码")
    last_latency_ms = Column(Float, nullable=True, comment="最近延迟(毫秒)")
    last_error = Column(Text, nullable=True, comment="最近错误信息")
    last_check_at = Column(DateTime, nullable=True, comment="最近检测时间")
    consecutive_failures = Column(Integer, default=0, comment="连续失败次数")
    
    # 统计（滚动窗口）
    availability = Column(Float, default=1.0, comment="可用性 0-1")
    error_rate = Column(Float, default=0.0, comment="错误率 0-1")
    
    # 元数据
    created_at = Column(DateTime, default=func.now(), comment="创建时间")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")
    deleted_at = Column(DateTime, nullable=True, comment="软删除时间")

    def __repr__(self):
        return f"<NodeModel(name={self.name}, url={self.url}, enabled={self.enabled})>"
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "region": self.region,
            "tags": self.tags or [],
            "timeout_ms": self.timeout_ms,
            "enabled": self.enabled,
            "expect": {
                "status": self.expect_status,
                "body_contains": self.expect_body_contains,
            },
            "last_status_code": self.last_status_code,
            "last_latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
            "consecutive_failures": self.consecutive_failures,
            "availability": self.availability,
            "error_rate": self.error_rate,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
