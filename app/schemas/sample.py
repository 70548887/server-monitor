"""样本数据模型（单次心跳检测结果）"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class SampleCreate(BaseModel):
    """创建样本"""
    node_name: str = Field(..., description="节点名称")
    url: str = Field(..., description="请求 URL")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="采样时间")
    status_code: int = Field(default=0, description="HTTP 状态码，0 表示请求失败")
    latency_ms: float = Field(..., ge=0, description="响应延迟(毫秒)")
    success: bool = Field(default=True, description="是否成功")
    error_type: Optional[str] = Field(default=None, description="错误类型")
    error_message: Optional[str] = Field(default=None, description="错误信息")
    response_size: Optional[int] = Field(default=None, ge=0, description="响应体大小(字节)")
    dns_ms: Optional[float] = Field(default=None, ge=0, description="DNS 解析耗时")
    tcp_ms: Optional[float] = Field(default=None, ge=0, description="TCP 连接耗时")
    tls_ms: Optional[float] = Field(default=None, ge=0, description="TLS 握手耗时")
    ttfb_ms: Optional[float] = Field(default=None, ge=0, description="首字节时间")


class Sample(SampleCreate):
    """样本（含 ID）"""
    id: int = Field(..., description="样本 ID")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": 1,
                "node_name": "api-gateway-01",
                "url": "https://api.example.com/health",
                "timestamp": "2025-12-05T12:00:00Z",
                "status_code": 200,
                "latency_ms": 45.2,
                "success": True,
                "error_type": None,
                "error_message": None,
                "response_size": 128,
                "dns_ms": 5.1,
                "tcp_ms": 10.3,
                "tls_ms": 15.2,
                "ttfb_ms": 35.0
            }
        }
