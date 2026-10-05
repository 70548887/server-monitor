"""通知渠道和发送记录 ORM 模型"""

from datetime import datetime, time
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Time, Float
from sqlalchemy.sql import func
import enum

from .base import Base


class ChannelType(str, enum.Enum):
    """通知渠道类型"""
    EMAIL = "email"
    WEBHOOK = "webhook"
    # 未来可扩展: SMS, DINGTALK, FEISHU, WECHAT_WORK 等


class NotificationChannelModel(Base):
    """通知渠道配置表"""
    __tablename__ = "notification_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True, comment="渠道名称")
    channel_type = Column(String(20), nullable=False, index=True, comment="渠道类型: email, webhook")
    enabled = Column(Boolean, default=True, index=True, comment="是否启用")
    
    # 邮箱配置
    smtp_host = Column(String(200), nullable=True, comment="SMTP服务器")
    smtp_port = Column(Integer, nullable=True, comment="SMTP端口")
    smtp_ssl = Column(Boolean, default=True, comment="是否使用SSL")
    smtp_user = Column(String(200), nullable=True, comment="SMTP用户名/发送人邮箱")
    smtp_password = Column(String(200), nullable=True, comment="SMTP密码")
    email_recipients = Column(Text, nullable=True, comment="收件人邮箱,逗号分隔")
    
    # Webhook 配置
    webhook_url = Column(String(500), nullable=True, comment="Webhook URL")
    webhook_secret = Column(String(200), nullable=True, comment="Webhook 签名密钥")
    
    # 通知配置
    notify_on_down = Column(Boolean, default=True, comment="节点下线时通知")
    notify_on_recovery = Column(Boolean, default=True, comment="节点恢复时通知")
    notify_on_latency = Column(Boolean, default=False, comment="延迟超阈值时通知")
    
    # 发送限制配置
    daily_limit_per_node = Column(Integer, default=10, comment="单个节点每日发送上限")
    min_interval_seconds = Column(Integer, default=10, comment="最小发送间隔(秒)")
    send_time_start = Column(Time, nullable=True, comment="可发送时间开始")
    send_time_end = Column(Time, nullable=True, comment="可发送时间结束")
    
    # 状态
    last_sent_at = Column(DateTime, nullable=True, comment="最后发送时间")
    last_error = Column(Text, nullable=True, comment="最后错误信息")
    send_count = Column(Integer, default=0, comment="发送次数")
    error_count = Column(Integer, default=0, comment="错误次数")
    
    # 元数据
    created_at = Column(DateTime, default=func.now(), comment="创建时间")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return f"<NotificationChannelModel(name={self.name}, type={self.channel_type}, enabled={self.enabled})>"
    
    def to_dict(self) -> dict:
        """转换为字典（隐藏敏感信息）"""
        return {
            "id": self.id,
            "name": self.name,
            "channel_type": self.channel_type,
            "enabled": self.enabled,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_ssl": self.smtp_ssl,
            "smtp_user": self.smtp_user,
            "smtp_password": "******" if self.smtp_password else None,
            "email_recipients": self.email_recipients,
            "webhook_url": self.webhook_url,
            "webhook_secret": "******" if self.webhook_secret else None,
            "notify_on_down": self.notify_on_down,
            "notify_on_recovery": self.notify_on_recovery,
            "notify_on_latency": self.notify_on_latency,
            "daily_limit_per_node": self.daily_limit_per_node,
            "min_interval_seconds": self.min_interval_seconds,
            "send_time_start": self.send_time_start.strftime("%H:%M") if self.send_time_start else None,
            "send_time_end": self.send_time_end.strftime("%H:%M") if self.send_time_end else None,
            "last_sent_at": self.last_sent_at.isoformat() if self.last_sent_at else None,
            "last_error": self.last_error,
            "send_count": self.send_count,
            "error_count": self.error_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
    
    def to_dict_full(self) -> dict:
        """转换为字典（包含敏感信息，仅内部使用）"""
        result = self.to_dict()
        result["smtp_password"] = self.smtp_password
        result["webhook_secret"] = self.webhook_secret
        return result


class NotificationRecipientModel(Base):
    """通知人员/接收人管理表"""
    __tablename__ = "notification_recipients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, comment="人员姓名/备注")
    email = Column(String(200), unique=True, nullable=False, index=True, comment="邮箱地址")
    enabled = Column(Boolean, default=True, index=True, comment="是否启用")
    
    # 元数据
    created_at = Column(DateTime, default=func.now(), comment="创建时间")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return f"<NotificationRecipientModel(name={self.name}, email={self.email}, enabled={self.enabled})>"
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class NotificationLogModel(Base):
    """通知发送记录表"""
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, nullable=False, index=True, comment="渠道ID")
    channel_name = Column(String(100), nullable=False, comment="渠道名称")
    node_name = Column(String(100), nullable=True, index=True, comment="节点名称")
    node_url = Column(String(500), nullable=True, comment="节点URL")

    # 关联的故障事件（Phase 2 事件化提醒）
    incident_id = Column(Integer, nullable=True, index=True, comment="所属故障事件ID")
    notify_seq = Column(Integer, nullable=True, comment="该事件内第几次提醒(从1开始)")

    # 发送信息
    alert_type = Column(String(50), nullable=False, index=True, comment="告警类型: node_down, node_recovery, latency_high, test")
    subject = Column(String(500), nullable=True, comment="邮件主题")
    message = Column(Text, nullable=True, comment="发送内容")
    recipients = Column(Text, nullable=True, comment="收件人")

    # 发送结果
    success = Column(Boolean, default=False, comment="是否成功")
    error_message = Column(Text, nullable=True, comment="错误信息")
    send_duration_ms = Column(Float, nullable=True, comment="发送耗时(毫秒)")

    # 时间
    sent_at = Column(DateTime, default=func.now(), index=True, comment="发送时间")

    def __repr__(self):
        return f"<NotificationLogModel(channel={self.channel_name}, node={self.node_name}, success={self.success})>"

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "node_name": self.node_name,
            "node_url": self.node_url,
            "incident_id": self.incident_id,
            "notify_seq": self.notify_seq,
            "alert_type": self.alert_type,
            "subject": self.subject,
            "message": self.message[:200] + "..." if self.message and len(self.message) > 200 else self.message,
            "recipients": self.recipients,
            "success": self.success,
            "error_message": self.error_message,
            "send_duration_ms": self.send_duration_ms,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }


class AlertIncidentModel(Base):
    """
    故障事件表 - 一次故障 = 一个事件

    取代"按自然日重置"的去重方式：事件有明确的开始与结束，
    重复提醒的节奏由 next_notify_at 这个闸门控制，
    跨午夜、进程重启都不会重置节奏。
    """
    __tablename__ = "alert_incidents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(String(100), nullable=False, index=True, comment="节点名称")
    alert_type = Column(String(50), nullable=False, index=True, comment="告警类型: node_down / latency_high")

    # 事件生命周期
    # 事件锚点：首次越过阈值的时间，阶梯提醒以此为基准
    started_at = Column(DateTime, nullable=False, default=func.now(), index=True, comment="事件开始时间")
    last_notified_at = Column(DateTime, nullable=True, comment="上次实际发出通知的时间")
    # 核心闸门：now < next_notify_at 时不产生任何告警
    next_notify_at = Column(DateTime, nullable=True, index=True, comment="下次允许通知的时间")
    notify_count = Column(Integer, default=0, comment="已通知次数")

    status = Column(String(20), default="open", index=True, comment="状态: open, resolved")
    resolved_at = Column(DateTime, nullable=True, comment="解决时间")
    duration_seconds = Column(Integer, nullable=True, comment="故障持续时长(秒)")

    # 事件快照，用于恢复通知汇总
    node_url = Column(String(500), nullable=True, comment="节点URL")
    last_error = Column(Text, nullable=True, comment="最近一次错误信息")
    total_failures = Column(Integer, default=0, comment="事件期间累计失败次数")

    created_at = Column(DateTime, default=func.now(), comment="创建时间")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), comment="更新时间")

    def __repr__(self):
        return (
            f"<AlertIncidentModel(node={self.node_name}, type={self.alert_type}, "
            f"status={self.status}, notify_count={self.notify_count})>"
        )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "node_name": self.node_name,
            "node_url": self.node_url,
            "alert_type": self.alert_type,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_notified_at": self.last_notified_at.isoformat() if self.last_notified_at else None,
            "next_notify_at": self.next_notify_at.isoformat() if self.next_notify_at else None,
            "notify_count": self.notify_count,
            "status": self.status,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "duration_seconds": self.duration_seconds,
            "last_error": self.last_error,
            "total_failures": self.total_failures,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AlertLogModel(Base):
    """告警记录表 - 记录所有触发的告警事件"""
    __tablename__ = "alert_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(String(100), nullable=False, index=True, comment="节点名称")
    node_url = Column(String(500), nullable=True, comment="节点URL")

    # 关联的故障事件（Phase 2 事件化提醒）
    incident_id = Column(Integer, nullable=True, index=True, comment="所属故障事件ID")
    notify_seq = Column(Integer, nullable=True, comment="该事件内第几次提醒(从1开始)")

    # 告警信息
    alert_type = Column(String(50), nullable=False, index=True, comment="告警类型: node_down, node_recovery, latency_high")
    message = Column(Text, nullable=True, comment="告警消息")
    details = Column(Text, nullable=True, comment="告警详情(JSON)")
    
    # 通知状态
    notification_sent = Column(Boolean, default=False, comment="是否已发送通知")
    notification_channels = Column(Text, nullable=True, comment="已通知的渠道名称,逗号分隔")
    notification_count = Column(Integer, default=0, comment="发送通知的渠道数")
    
    # 状态
    status = Column(String(20), default="active", index=True, comment="状态: active, acknowledged, resolved")
    acknowledged_at = Column(DateTime, nullable=True, comment="确认时间")
    acknowledged_by = Column(String(100), nullable=True, comment="确认人")
    resolved_at = Column(DateTime, nullable=True, comment="解决时间")
    
    # 时间
    triggered_at = Column(DateTime, default=func.now(), index=True, comment="触发时间")
    created_at = Column(DateTime, default=func.now(), comment="创建时间")
    
    def __repr__(self):
        return f"<AlertLogModel(node={self.node_name}, type={self.alert_type}, status={self.status})>"
    
    def to_dict(self) -> dict:
        """转换为字典"""
        import json
        details_dict = None
        if self.details:
            try:
                details_dict = json.loads(self.details)
            except:
                details_dict = self.details
        
        return {
            "id": self.id,
            "node_name": self.node_name,
            "node_url": self.node_url,
            "incident_id": self.incident_id,
            "notify_seq": self.notify_seq,
            "alert_type": self.alert_type,
            "message": self.message,
            "details": details_dict,
            "notification_sent": self.notification_sent,
            "notification_channels": self.notification_channels,
            "notification_count": self.notification_count,
            "status": self.status,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "acknowledged_by": self.acknowledged_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
