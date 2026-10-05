"""聚合数据模型（统计快照）"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class AggregateCreate(BaseModel):
    """创建聚合"""
    node_name: str = Field(..., description="节点名称")
    window_start: datetime = Field(..., description="窗口开始时间")
    window_end: datetime = Field(..., description="窗口结束时间")
    window_minutes: int = Field(default=5, ge=1, description="窗口大小(分钟)")
    total_count: int = Field(default=0, ge=0, description="总请求数")
    success_count: int = Field(default=0, ge=0, description="成功数")
    error_count: int = Field(default=0, ge=0, description="失败数")
    error_rate: float = Field(default=0.0, ge=0, le=1, description="错误率")
    latency_min_ms: Optional[float] = Field(default=None, ge=0, description="最小延迟")
    latency_max_ms: Optional[float] = Field(default=None, ge=0, description="最大延迟")
    latency_avg_ms: Optional[float] = Field(default=None, ge=0, description="平均延迟")
    latency_p50_ms: Optional[float] = Field(default=None, ge=0, description="p50 延迟")
    latency_p95_ms: Optional[float] = Field(default=None, ge=0, description="p95 延迟")
    latency_p99_ms: Optional[float] = Field(default=None, ge=0, description="p99 延迟")
    availability: float = Field(default=1.0, ge=0, le=1, description="可用性")


class Aggregate(AggregateCreate):
    """聚合（含 ID）"""
    id: int = Field(..., description="聚合 ID")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="创建时间")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": 1,
                "node_name": "api-gateway-01",
                "window_start": "2025-12-05T12:00:00Z",
                "window_end": "2025-12-05T12:05:00Z",
                "window_minutes": 5,
                "total_count": 5,
                "success_count": 5,
                "error_count": 0,
                "error_rate": 0.0,
                "latency_min_ms": 40.0,
                "latency_max_ms": 55.0,
                "latency_avg_ms": 45.5,
                "latency_p50_ms": 44.0,
                "latency_p95_ms": 52.0,
                "latency_p99_ms": 55.0,
                "availability": 1.0,
                "created_at": "2025-12-05T12:05:01Z"
            }
        }
