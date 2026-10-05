"""节点配置与状态模型"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, HttpUrl


class NodeExpect(BaseModel):
    """节点期望响应"""
    status: int = Field(default=200, description="期望状态码")
    body_contains: Optional[str] = Field(default=None, description="响应体包含的关键字")


class NodeConfig(BaseModel):
    """节点配置"""
    name: str = Field(..., description="节点唯一名称")
    url: str = Field(..., description="心跳检测 URL")
    region: str = Field(default="default", description="区域标签")
    tags: list[str] = Field(default_factory=list, description="自定义标签")
    timeout_ms: int = Field(default=2000, ge=100, le=30000, description="超时时间(毫秒)")
    enabled: bool = Field(default=True, description="是否启用")
    expect: NodeExpect = Field(default_factory=NodeExpect, description="期望响应")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "api-gateway-01",
                "url": "https://api.example.com/health",
                "region": "cn-shanghai",
                "tags": ["core", "gateway"],
                "timeout_ms": 3000,
                "enabled": True,
                "expect": {"status": 200, "body_contains": "ok"}
            }
        }


class NodeStatus(BaseModel):
    """节点状态（实时）"""
    name: str
    url: str
    region: str
    tags: list[str]
    enabled: bool
    availability: float = Field(default=1.0, ge=0, le=1, description="可用性 0-1")
    last_status_code: Optional[int] = Field(default=None, description="最近状态码")
    last_latency_ms: Optional[float] = Field(default=None, description="最近延迟(毫秒)")
    last_error: Optional[str] = Field(default=None, description="最近错误")
    last_check_at: Optional[datetime] = Field(default=None, description="最近检测时间")
    error_rate: float = Field(default=0.0, ge=0, le=1, description="错误率")
    consecutive_failures: int = Field(default=0, ge=0, description="连续失败次数")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "api-gateway-01",
                "url": "https://api.example.com/health",
                "region": "cn-shanghai",
                "tags": ["core", "gateway"],
                "enabled": True,
                "availability": 0.995,
                "last_status_code": 200,
                "last_latency_ms": 45.2,
                "last_error": None,
                "last_check_at": "2025-12-05T12:00:00Z",
                "error_rate": 0.005,
                "consecutive_failures": 0
            }
        }


class NodeCreate(BaseModel):
    """创建节点请求"""
    name: str = Field(..., min_length=1, max_length=100, description="节点唯一名称")
    url: str = Field(..., description="心跳检测 URL")
    region: str = Field(default="default", description="区域标签")
    tags: list[str] = Field(default_factory=list, description="自定义标签")
    timeout_ms: int = Field(default=2000, ge=100, le=30000, description="超时时间(毫秒)")
    enabled: bool = Field(default=True, description="是否启用")
    expect: Optional[NodeExpect] = Field(default=None, description="期望响应")


class NodeUpdate(BaseModel):
    """更新节点请求"""
    url: Optional[str] = Field(default=None, description="心跳检测 URL")
    region: Optional[str] = Field(default=None, description="区域标签")
    tags: Optional[list[str]] = Field(default=None, description="自定义标签")
    timeout_ms: Optional[int] = Field(default=None, ge=100, le=30000, description="超时时间(毫秒)")
    enabled: Optional[bool] = Field(default=None, description="是否启用")
    expect: Optional[NodeExpect] = Field(default=None, description="期望响应")
