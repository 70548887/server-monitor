"""通知渠道 Pydantic 模型"""

from datetime import datetime
from typing import Optional, List
from enum import Enum
from pydantic import BaseModel, Field, EmailStr, field_validator


class ChannelTypeEnum(str, Enum):
    """通知渠道类型"""
    EMAIL = "email"
    WEBHOOK = "webhook"


class NotificationChannelCreate(BaseModel):
    """创建通知渠道请求"""
    name: str = Field(..., min_length=1, max_length=100, description="渠道名称")
    channel_type: ChannelTypeEnum = Field(..., description="渠道类型")
    enabled: bool = Field(default=True, description="是否启用")
    
    # 邮箱配置
    smtp_host: Optional[str] = Field(default=None, max_length=200, description="SMTP服务器")
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535, description="SMTP端口")
    smtp_ssl: bool = Field(default=True, description="是否使用SSL")
    smtp_user: Optional[str] = Field(default=None, max_length=200, description="SMTP用户名/发送人邮箱")
    smtp_password: Optional[str] = Field(default=None, max_length=200, description="SMTP密码")
    email_recipients: Optional[str] = Field(default=None, description="收件人邮箱,逗号分隔")
    
    # Webhook 配置
    webhook_url: Optional[str] = Field(default=None, max_length=500, description="Webhook URL")
    webhook_secret: Optional[str] = Field(default=None, max_length=200, description="Webhook 签名密钥")
    
    # 通知配置
    notify_on_down: bool = Field(default=True, description="节点下线时通知")
    notify_on_recovery: bool = Field(default=True, description="节点恢复时通知")
    notify_on_latency: bool = Field(default=False, description="延迟超阈值时通知")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "邮箱B",
                "channel_type": "email",
                "enabled": True,
                "smtp_host": "smtp.qq.com",
                "smtp_port": 465,
                "smtp_ssl": True,
                "smtp_user": "sender@qq.com",
                "smtp_password": "your_smtp_password",
                "email_recipients": "recipient@example.com",
                "notify_on_down": True,
                "notify_on_recovery": True,
                "notify_on_latency": False
            }
        }


class NotificationChannelUpdate(BaseModel):
    """更新通知渠道请求"""
    name: Optional[str] = Field(default=None, min_length=1, max_length=100, description="渠道名称")
    enabled: Optional[bool] = Field(default=None, description="是否启用")
    
    # 邮箱配置
    smtp_host: Optional[str] = Field(default=None, max_length=200, description="SMTP服务器")
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535, description="SMTP端口")
    smtp_ssl: Optional[bool] = Field(default=None, description="是否使用SSL")
    smtp_user: Optional[str] = Field(default=None, max_length=200, description="SMTP用户名/发送人邮箱")
    smtp_password: Optional[str] = Field(default=None, max_length=200, description="SMTP密码")
    email_recipients: Optional[str] = Field(default=None, description="收件人邮箱,逗号分隔")
    
    # Webhook 配置
    webhook_url: Optional[str] = Field(default=None, max_length=500, description="Webhook URL")
    webhook_secret: Optional[str] = Field(default=None, max_length=200, description="Webhook 签名密钥")
    
    # 通知配置
    notify_on_down: Optional[bool] = Field(default=None, description="节点下线时通知")
    notify_on_recovery: Optional[bool] = Field(default=None, description="节点恢复时通知")
    notify_on_latency: Optional[bool] = Field(default=None, description="延迟超阈值时通知")
    
    # 发送限制配置
    daily_limit_per_node: Optional[int] = Field(default=None, ge=0, description="单个节点每日发送上限")
    min_interval_seconds: Optional[int] = Field(default=None, ge=0, description="最小发送间隔(秒)")
    send_time_start: Optional[str] = Field(default=None, description="可发送时间开始(HH:MM)")
    send_time_end: Optional[str] = Field(default=None, description="可发送时间结束(HH:MM)")


class NotificationChannelResponse(BaseModel):
    """通知渠道响应"""
    id: int
    name: str
    channel_type: str
    enabled: bool
    
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_ssl: bool = True
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None  # 返回时隐藏
    email_recipients: Optional[str] = None
    
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None  # 返回时隐藏
    
    notify_on_down: bool = True
    notify_on_recovery: bool = True
    notify_on_latency: bool = False
    
    # 发送限制配置
    daily_limit_per_node: int = 10
    min_interval_seconds: int = 10
    send_time_start: Optional[str] = None
    send_time_end: Optional[str] = None
    
    last_sent_at: Optional[datetime] = None
    last_error: Optional[str] = None
    send_count: int = 0
    error_count: int = 0
    
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class NotificationTestRequest(BaseModel):
    """测试通知请求"""
    channel_id: int = Field(..., description="渠道ID")
    message: Optional[str] = Field(default="这是一条测试通知消息", description="测试消息内容")


class NotificationTestResponse(BaseModel):
    """测试通知响应"""
    success: bool
    message: str
    channel_name: str
    sent_at: datetime


class NotificationRecipientCreate(BaseModel):
    """创建通知人员请求"""
    name: str = Field(..., min_length=1, max_length=100, description="人员姓名/备注")
    email: EmailStr = Field(..., description="邮箱地址")
    enabled: bool = Field(default=True, description="是否启用")


class NotificationRecipientUpdate(BaseModel):
    """更新通知人员请求"""
    name: Optional[str] = Field(default=None, min_length=1, max_length=100, description="人员姓名/备注")
    email: Optional[EmailStr] = Field(default=None, description="邮箱地址")
    enabled: Optional[bool] = Field(default=None, description="是否启用")


class NotificationRecipientResponse(BaseModel):
    """通知人员响应"""
    id: int
    name: str
    email: str
    enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AlertNotification(BaseModel):
    """告警通知内容"""
    alert_type: str = Field(..., description="告警类型: node_down, node_recovery, latency_high")
    node_name: str = Field(..., description="节点名称")
    node_url: str = Field(..., description="节点URL")
    message: str = Field(..., description="告警消息")
    details: Optional[dict] = Field(default=None, description="详细信息")
    triggered_at: datetime = Field(default_factory=datetime.utcnow, description="触发时间")
