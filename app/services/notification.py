"""
通知服务 - 支持邮箱和Webhook通知（真正异步）

功能说明：
    - EmailNotificationService: 邮箱通知发送（支持 aiosmtplib 异步发送）
    - NotificationLogService: 通知发送记录管理
    - NotificationService: 通知渠道管理和发送控制

发送限制功能：
    - 时间范围限制：只在指定时间段内发送
    - 最小发送间隔：避免频繁发送
    - 每日发送上限：按节点限制每日发送次数

错误处理策略：
    - SMTP 认证失败：记录错误并返回失败结果
    - 网络超时：记录警告并重试
    - SSL 失败：自动回退到 STARTTLS
    - 数据库错误：记录日志不影响发送

使用方法：
    from app.services.notification import NotificationService
    
    # 发送通知
    success, error = await NotificationService.send_notification(
        channel_id=1,
        subject="测试通知",
        message="这是一条测试消息",
    )
"""

import ssl
import asyncio
import logging
import time as time_module
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Tuple

# 尝试导入 certifi 获取 CA 证书路径
try:
    import certifi
    _CA_BUNDLE = certifi.where()
except ImportError:
    _CA_BUNDLE = None

# 尝试导入 aiosmtplib，如果不可用则降级到线程池
try:
    import aiosmtplib
    _HAS_AIOSMTPLIB = True
except ImportError:
    _HAS_AIOSMTPLIB = False
    import smtplib
    from concurrent.futures import ThreadPoolExecutor
    _executor = ThreadPoolExecutor(max_workers=10)


def _create_ssl_context() -> ssl.SSLContext:
    """
    创建 SSL 上下文，正确加载 CA 证书
    
    解决 Windows 上 "unable to get local issuer certificate" 错误
    """
    context = ssl.create_default_context()
    
    if _CA_BUNDLE:
        # 使用 certifi 提供的 CA 证书
        context.load_verify_locations(_CA_BUNDLE)
        logger.debug(f"SSL: 使用 certifi CA 证书: {_CA_BUNDLE}")
    else:
        # certifi 不可用，尝试使用系统证书
        try:
            context.load_default_certs()
            logger.debug("SSL: 使用系统默认 CA 证书")
        except Exception as e:
            # 如果都失败，禁用证书验证（仅开发环境）
            logger.warning(f"SSL: 无法加载 CA 证书 ({e})，将禁用证书验证")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    
    return context

from sqlalchemy import select, update, func, and_
from sqlalchemy.exc import SQLAlchemyError

from ..models.notification_channel import (
    NotificationChannelModel, 
    NotificationLogModel,
    NotificationRecipientModel
)
from ..models.base import get_session_context
from ..utils.timeutil import utcnow

logger = logging.getLogger(__name__)


class EmailNotificationService:
    """邮箱通知服务（支持真正异步发送）"""
    
    @staticmethod
    def _build_message(
        smtp_user: str,
        recipients: List[str],
        subject: str,
        body: str,
    ) -> MIMEMultipart:
        """构建邮件对象"""
        msg = MIMEMultipart("alternative")
        msg["From"] = smtp_user
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = Header(subject, "utf-8")
        
        # 邮件正文（HTML格式）
        html_body = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .alert-box {{ 
                    border: 1px solid #ddd; 
                    border-radius: 8px; 
                    padding: 20px; 
                    margin: 10px 0;
                    background-color: #f9f9f9;
                }}
                .alert-title {{ 
                    color: #333; 
                    font-size: 18px; 
                    font-weight: bold; 
                    margin-bottom: 10px;
                }}
                .alert-content {{ color: #666; line-height: 1.6; }}
                .footer {{ 
                    margin-top: 20px; 
                    padding-top: 10px; 
                    border-top: 1px solid #eee; 
                    color: #999; 
                    font-size: 12px;
                }}
            </style>
        </head>
        <body>
            <div class="alert-box">
                <div class="alert-title">服务器监控告警</div>
                <div class="alert-content">
                    {body.replace(chr(10), '<br>')}
                </div>
            </div>
            <div class="footer">
                此邮件由 Server Monitor 自动发送<br>
                发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            </div>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        return msg
    
    @classmethod
    async def _send_email_async(cls,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        recipients: List[str],
        subject: str,
        body: str,
        use_ssl: bool = True,
    ) -> tuple[bool, str]:
        """使用 aiosmtplib 发送邮件（真正异步）"""
        try:
            msg = cls._build_message(smtp_user, recipients, subject, body)
            
            logger.info(f"正在连接 SMTP 服务器 (异步): {smtp_host}:{smtp_port} (SSL={use_ssl})")
            
            # 创建正确配置的 SSL 上下文（解决 Windows 证书验证问题）
            tls_context = _create_ssl_context()
            
            if use_ssl:
                # SSL 连接 (端口 465)
                try:
                    await aiosmtplib.send(
                        msg,
                        hostname=smtp_host,
                        port=smtp_port,
                        username=smtp_user,
                        password=smtp_password,
                        use_tls=True,
                        tls_context=tls_context,
                        timeout=30,
                    )
                except Exception as ssl_err:
                    # SSL 失败，尝试 STARTTLS
                    logger.warning(f"SSL 连接失败，尝试 STARTTLS: {ssl_err}")
                    await aiosmtplib.send(
                        msg,
                        hostname=smtp_host,
                        port=587,
                        username=smtp_user,
                        password=smtp_password,
                        start_tls=True,
                        tls_context=tls_context,
                        timeout=30,
                    )
            else:
                # STARTTLS (端口 587) 或普通 SMTP (端口 25)
                await aiosmtplib.send(
                    msg,
                    hostname=smtp_host,
                    port=smtp_port,
                    username=smtp_user,
                    password=smtp_password,
                    start_tls=(smtp_port == 587),
                    tls_context=tls_context if smtp_port == 587 else None,
                    timeout=30,
                )
            
            logger.info(f"邮件发送成功 (异步): 收件人={recipients}")
            return True, ""
            
        except aiosmtplib.SMTPAuthenticationError as e:
            error_msg = f"SMTP认证失败: {e}"
            logger.error(error_msg)
            return False, error_msg
        except aiosmtplib.SMTPException as e:
            error_msg = f"SMTP错误: {e}"
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            error_msg = f"发送邮件失败: {e}"
            logger.error(error_msg)
            return False, error_msg
    
    @staticmethod
    def _send_email_sync(
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        recipients: List[str],
        subject: str,
        body: str,
        use_ssl: bool = True,
    ) -> tuple[bool, str]:
        """同步发送邮件（备用，在线程池中执行）"""
        import smtplib as sync_smtplib
        try:
            msg = EmailNotificationService._build_message(smtp_user, recipients, subject, body)
            
            logger.info(f"正在连接 SMTP 服务器 (同步): {smtp_host}:{smtp_port} (SSL={use_ssl})")
            
            # 使用正确配置的 SSL 上下文
            context = _create_ssl_context()
            
            if use_ssl:
                try:
                    server = sync_smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30)
                    server.login(smtp_user, smtp_password)
                    server.sendmail(smtp_user, recipients, msg.as_string())
                    server.quit()
                except ssl.SSLError as ssl_err:
                    logger.warning(f"SSL 连接失败，尝试 STARTTLS: {ssl_err}")
                    server = sync_smtplib.SMTP(smtp_host, 587, timeout=30)
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(smtp_user, smtp_password)
                    server.sendmail(smtp_user, recipients, msg.as_string())
                    server.quit()
            else:
                server = sync_smtplib.SMTP(smtp_host, smtp_port, timeout=30)
                server.ehlo()
                if smtp_port == 587:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(smtp_user, smtp_password)
                server.sendmail(smtp_user, recipients, msg.as_string())
                server.quit()
            
            logger.info(f"邮件发送成功 (同步): 收件人={recipients}")
            return True, ""
            
        except Exception as e:
            error_msg = f"发送邮件失败: {e}"
            logger.error(error_msg)
            return False, error_msg
    
    @classmethod
    async def send_email(
        cls,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        recipients: List[str],
        subject: str,
        body: str,
        use_ssl: bool = True,
    ) -> tuple[bool, str]:
        """
        异步发送邮件
        
        优先使用 aiosmtplib（真正异步），如果不可用则降级到线程池。
        
        Returns:
            (是否成功, 错误消息)
        """
        if _HAS_AIOSMTPLIB:
            # 真正异步发送
            return await cls._send_email_async(
                smtp_host, smtp_port, smtp_user, smtp_password,
                recipients, subject, body, use_ssl
            )
        else:
            # 降级到线程池
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                cls._send_email_sync,
                smtp_host, smtp_port, smtp_user, smtp_password,
                recipients, subject, body, use_ssl
            )


class WebhookNotificationService:
    """
    Webhook 通知服务

    此前该分支直接返回"未实现"，但模型、Schema、前端表单都支持
    webhook 渠道 —— 用户能在面板上创建渠道并点测试，结果永远是失败。
    这里补齐发送实现。

    请求体为 JSON，字段与邮件正文对应，便于对端直接消费。
    若渠道配置了签名密钥，则附加 HMAC-SHA256 签名头，
    接收方可据此校验请求确实来自本监控系统。
    """

    # 超时与重试：webhook 对端不可控，必须设置上限，
    # 否则会拖住整条告警链路（告警是串行发送的）
    TIMEOUT_SECONDS = 10.0
    MAX_RETRIES = 3
    # 指数退避：base * 2^attempt，上限 MAX_RETRY_DELAY_SECONDS。
    # 对端长时间不可用时固定间隔重试会加剧其负担，
    # 退避让压力随时间释放，也给对端留出恢复窗口。
    RETRY_BASE_DELAY_SECONDS = 1.0
    MAX_RETRY_DELAY_SECONDS = 30.0

    @classmethod
    def _retry_delay(cls, attempt: int) -> float:
        """
        计算第 attempt 次重试前的等待秒数

        Args:
            attempt: 已经失败并准备重试的次数（从 0 开始）

        Returns:
            等待秒数，封顶 MAX_RETRY_DELAY_SECONDS
        """
        delay = cls.RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
        return min(delay, cls.MAX_RETRY_DELAY_SECONDS)

    @classmethod
    def _build_payload(
        cls,
        alert_type: str,
        node_name: Optional[str],
        node_url: Optional[str],
        subject: str,
        message: str,
    ) -> dict:
        """构造 webhook 请求体"""
        return {
            "alert_type": alert_type,
            "node_name": node_name,
            "node_url": node_url,
            "subject": subject,
            "message": message,
            "triggered_at": datetime.now().isoformat(),
            "source": "server-monitor",
        }

    @classmethod
    def _sign(cls, body: bytes, secret: str) -> str:
        """计算 HMAC-SHA256 签名"""
        import hashlib
        import hmac

        return hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()

    @classmethod
    async def send_webhook(
        cls,
        url: str,
        payload: dict,
        secret: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        发送 webhook 请求

        Args:
            url: 接收地址
            payload: 请求体
            secret: 可选的签名密钥

        Returns:
            (是否成功, 错误信息)
        """
        import json

        import httpx

        if not url:
            return False, "Webhook URL 为空"

        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            return False, f"请求体序列化失败: {e}"

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "server-monitor/1.0",
        }
        if secret:
            headers["X-Signature"] = cls._sign(body, secret)
            headers["X-Signature-Algorithm"] = "hmac-sha256"

        # 本地地址不做 TLS 校验（常见于内网自签证书场景）
        verify = not url.startswith("http://")

        last_error = None
        for attempt in range(cls.MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=cls.TIMEOUT_SECONDS, verify=verify
                ) as client:
                    response = await client.post(url, content=body, headers=headers)

                if 200 <= response.status_code < 300:
                    logger.info(
                        f"Webhook 发送成功: url={url}, "
                        f"status={response.status_code}"
                    )
                    return True, None

                # 4xx 是配置问题，重试无意义（除非是 429 限流）
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    return False, f"Webhook 返回 {response.status_code}（配置或权限问题）"

                last_error = f"Webhook 返回 {response.status_code}"

            except httpx.TimeoutException:
                last_error = f"Webhook 请求超时（{cls.TIMEOUT_SECONDS}s）"
            except httpx.ConnectError as e:
                last_error = f"Webhook 连接失败: {e}"
            except httpx.HTTPError as e:
                last_error = f"Webhook 请求错误: {e}"
            except Exception as e:
                last_error = f"Webhook 发送异常: {type(e).__name__}: {e}"

            if attempt < cls.MAX_RETRIES:
                delay = cls._retry_delay(attempt)
                logger.warning(
                    f"Webhook 发送失败（第 {attempt + 1} 次），"
                    f"{delay:.1f}s 后重试: {last_error}"
                )
                await asyncio.sleep(delay)

        logger.error(f"Webhook 发送最终失败: url={url}, error={last_error}")
        return False, last_error


class NotificationLogService:
    """
    通知发送记录服务
    
    记录每次通知发送的详细信息，包括：
    - 发送时间、渠道、收件人
    - 发送结果、错误信息
    - 发送耗时统计
    """
    
    @staticmethod
    async def add_log(
        channel_id: int,
        channel_name: str,
        alert_type: str,
        success: bool,
        node_name: Optional[str] = None,
        node_url: Optional[str] = None,
        subject: Optional[str] = None,
        message: Optional[str] = None,
        recipients: Optional[str] = None,
        error_message: Optional[str] = None,
        send_duration_ms: Optional[float] = None,
    ) -> dict:
        """
        添加发送记录
        
        Args:
            channel_id: 渠道ID
            channel_name: 渠道名称
            alert_type: 告警类型 (node_down/node_recovery/latency_high/test)
            success: 是否成功
            node_name: 节点名称（可选）
            node_url: 节点URL（可选）
            subject: 邮件主题（可选）
            message: 邮件内容（可选）
            recipients: 收件人列表（可选）
            error_message: 错误信息（可选）
            send_duration_ms: 发送耗时（毫秒）
        
        Returns:
            记录字典
        """
        try:
            async with get_session_context() as session:
                log = NotificationLogModel(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    node_name=node_name,
                    node_url=node_url,
                    alert_type=alert_type,
                    subject=subject,
                    message=message,
                    recipients=recipients,
                    success=success,
                    error_message=error_message,
                    send_duration_ms=send_duration_ms,
                )
                session.add(log)
                await session.flush()
                await session.refresh(log)
                
                if success:
                    logger.debug(
                        f"Notification log added: channel={channel_name}, "
                        f"alert_type={alert_type}, node={node_name}"
                    )
                else:
                    logger.debug(
                        f"Notification log added (failed): channel={channel_name}, "
                        f"alert_type={alert_type}, error={error_message}"
                    )
                
                return log.to_dict()
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to add notification log: channel={channel_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return {}  # 返回空字典，不影响发送流程
        except Exception as e:
            logger.error(
                f"Unexpected error adding notification log: "
                f"error_type={type(e).__name__}, error={e}"
            )
            return {}

    @staticmethod
    async def get_logs(
        channel_id: Optional[int] = None,
        node_name: Optional[str] = None,
        alert_type: Optional[str] = None,
        success: Optional[bool] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[dict], int]:
        """
        获取发送记录列表
        
        Returns:
            (logs, total_count)
        """
        try:
            async with get_session_context() as session:
                # 构建查询
                stmt = select(NotificationLogModel)
                
                # 条件筛选
                conditions = []
                if channel_id is not None:
                    conditions.append(NotificationLogModel.channel_id == channel_id)
                if node_name:
                    conditions.append(NotificationLogModel.node_name == node_name)
                if alert_type:
                    conditions.append(NotificationLogModel.alert_type == alert_type)
                if success is not None:
                    conditions.append(NotificationLogModel.success == success)
                if start_date:
                    conditions.append(NotificationLogModel.sent_at >= start_date)
                if end_date:
                    conditions.append(NotificationLogModel.sent_at <= end_date)
                
                if conditions:
                    stmt = stmt.where(and_(*conditions))
                
                # 统计总数
                count_stmt = select(func.count()).select_from(stmt.alias())
                total_result = await session.execute(count_stmt)
                total = total_result.scalar() or 0
                
                # 分页查询
                stmt = stmt.order_by(NotificationLogModel.sent_at.desc()).limit(limit).offset(offset)
                result = await session.execute(stmt)
                logs = [log.to_dict() for log in result.scalars().all()]
                
                return logs, total
        except Exception as e:
            logger.error(f"Failed to get notification logs: {e}")
            return [], 0

    @staticmethod
    async def delete_log(log_id: int) -> bool:
        """删除单条发送记录，同步更新渠道统计"""
        try:
            async with get_session_context() as session:
                stmt = select(NotificationLogModel).where(NotificationLogModel.id == log_id)
                result = await session.execute(stmt)
                log = result.scalar_one_or_none()
                if not log:
                    return False
                
                # 保存记录信息用于更新统计
                channel_id = log.channel_id
                was_failed = not log.success
                
                await session.delete(log)
                await session.flush()
                
                # 同步减少渠道统计
                channel_stmt = select(NotificationChannelModel).where(NotificationChannelModel.id == channel_id)
                channel_result = await session.execute(channel_stmt)
                channel = channel_result.scalar_one_or_none()
                if channel:
                    if channel.send_count > 0:
                        channel.send_count -= 1
                    if was_failed and channel.error_count > 0:
                        channel.error_count -= 1
                
                return True
        except Exception as e:
            logger.error(f"Failed to delete notification log {log_id}: {e}")
            return False

    @staticmethod
    async def delete_logs_batch(log_ids: List[int]) -> int:
        """批量删除发送记录，同步更新渠道统计"""
        try:
            async with get_session_context() as session:
                # 先查询要删除的记录，统计每个渠道的变化
                stmt = select(NotificationLogModel).where(NotificationLogModel.id.in_(log_ids))
                result = await session.execute(stmt)
                logs = result.scalars().all()
                
                # 统计每个渠道的变化
                channel_changes = {}
                for log in logs:
                    if log.channel_id not in channel_changes:
                        channel_changes[log.channel_id] = {"total": 0, "failed": 0}
                    channel_changes[log.channel_id]["total"] += 1
                    if not log.success:
                        channel_changes[log.channel_id]["failed"] += 1
                
                # 执行删除
                from sqlalchemy import delete as sqlalchemy_delete
                delete_stmt = sqlalchemy_delete(NotificationLogModel).where(NotificationLogModel.id.in_(log_ids))
                delete_result = await session.execute(delete_stmt)
                deleted_count = delete_result.rowcount
                await session.flush()
                
                # 更新渠道统计
                for channel_id, changes in channel_changes.items():
                    channel_stmt = select(NotificationChannelModel).where(NotificationChannelModel.id == channel_id)
                    channel_result = await session.execute(channel_stmt)
                    channel = channel_result.scalar_one_or_none()
                    if channel:
                        channel.send_count = max(0, channel.send_count - changes["total"])
                        channel.error_count = max(0, channel.error_count - changes["failed"])
                
                logger.info(f"Batch deleted {deleted_count} notification logs")
                return deleted_count
        except Exception as e:
            logger.error(f"Failed to batch delete notification logs: {e}")
            return 0

    @staticmethod
    async def cleanup_old_logs(days: int = 30) -> int:
        """清理过旧的发送记录"""
        try:
            async with get_session_context() as session:
                cutoff_date = datetime.now() - timedelta(days=days)
                
                # 先统计要删除的数量
                count_stmt = select(func.count()).where(NotificationLogModel.sent_at < cutoff_date)
                count_result = await session.execute(count_stmt)
                count = count_result.scalar() or 0
                
                if count > 0:
                    # 执行删除
                    from sqlalchemy import delete as sqlalchemy_delete
                    stmt = sqlalchemy_delete(NotificationLogModel).where(NotificationLogModel.sent_at < cutoff_date)
                    await session.execute(stmt)
                    logger.info(f"Cleaned up {count} notification logs older than {days} days")
                
                return count
        except Exception as e:
            logger.error(f"Failed to cleanup old notification logs: {e}")
            return 0

    @staticmethod
    async def get_last_send_time(channel_id: int, node_name: Optional[str] = None) -> Optional[datetime]:
        """获取指定渠道和节点的最后发送时间"""
        try:
            async with get_session_context() as session:
                stmt = select(NotificationLogModel.sent_at).where(
                    NotificationLogModel.channel_id == channel_id
                )
                if node_name:
                    stmt = stmt.where(NotificationLogModel.node_name == node_name)
                
                stmt = stmt.order_by(NotificationLogModel.sent_at.desc()).limit(1)
                result = await session.execute(stmt)
                sent_at = result.scalar_one_or_none()
                return sent_at
        except Exception as e:
            logger.error(f"Failed to get last send time: {e}")
            return None

    @staticmethod
    async def get_today_count_for_node(channel_id: int, node_name: str) -> int:
        """
        获取今日指定节点的成功发送次数

        只统计真正发送成功的记录（success=True）。被限流拒绝的尝试
        同样会写入 notification_logs，若一并计入会反过来顶高配额，
        造成"越拒绝越容易触发上限"的配额污染。
        """
        try:
            async with get_session_context() as session:
                today_start = datetime.combine(datetime.today(), time(0, 0, 0))
                stmt = select(func.count()).where(
                    and_(
                        NotificationLogModel.channel_id == channel_id,
                        NotificationLogModel.node_name == node_name,
                        NotificationLogModel.sent_at >= today_start,
                        NotificationLogModel.success == True,
                    )
                )
                result = await session.execute(stmt)
                return result.scalar() or 0
        except Exception as e:
            logger.error(f"Failed to get today count for node: {e}")
            return 0

    @staticmethod
    async def recalculate_channel_stats() -> dict:
        """从发送记录表重新统计所有渠道的发送次数"""
        try:
            async with get_session_context() as session:
                # 获取所有渠道
                channels_stmt = select(NotificationChannelModel)
                channels_result = await session.execute(channels_stmt)
                channels = channels_result.scalars().all()
                
                result = {}
                for channel in channels:
                    # 统计该渠道的发送记录
                    total_stmt = select(func.count()).where(NotificationLogModel.channel_id == channel.id)
                    total_result = await session.execute(total_stmt)
                    actual_total = total_result.scalar() or 0
                    
                    failed_stmt = select(func.count()).where(
                        and_(
                            NotificationLogModel.channel_id == channel.id,
                            NotificationLogModel.success == False
                        )
                    )
                    failed_result = await session.execute(failed_stmt)
                    actual_failed = failed_result.scalar() or 0
                    
                    # 对比是否有变化
                    old_total = channel.send_count
                    old_failed = channel.error_count
                    changed = (old_total != actual_total) or (old_failed != actual_failed)
                    
                    if changed:
                        channel.send_count = actual_total
                        channel.error_count = actual_failed
                    
                    result[channel.id] = {
                        "channel_name": channel.name,
                        "old_total": old_total,
                        "new_total": actual_total,
                        "old_failed": old_failed,
                        "new_failed": actual_failed,
                        "changed": changed
                    }
                
                logger.info(f"Recalculated stats for {len(channels)} channels")
                return result
        except Exception as e:
            logger.error(f"Failed to recalculate channel stats: {e}")
            return {}


class NotificationRecipientService:
    """通知人员管理服务"""

    @classmethod
    async def get_all(cls) -> List[dict]:
        """获取所有通知人员"""
        try:
            async with get_session_context() as session:
                stmt = select(NotificationRecipientModel).order_by(NotificationRecipientModel.created_at.desc())
                result = await session.execute(stmt)
                return [r.to_dict() for r in result.scalars().all()]
        except Exception as e:
            logger.error(f"Failed to get all recipients: {e}")
            return []

    @classmethod
    async def get_by_id(cls, recipient_id: int) -> Optional[dict]:
        """通过ID获取人员"""
        try:
            async with get_session_context() as session:
                stmt = select(NotificationRecipientModel).where(NotificationRecipientModel.id == recipient_id)
                result = await session.execute(stmt)
                recipient = result.scalar_one_or_none()
                return recipient.to_dict() if recipient else None
        except Exception as e:
            logger.error(f"Failed to get recipient by id: {recipient_id}, error: {e}")
            return None

    @classmethod
    async def get_enabled_emails(cls) -> List[str]:
        """获取所有已启用的通知人员邮箱"""
        try:
            async with get_session_context() as session:
                stmt = select(NotificationRecipientModel.email).where(NotificationRecipientModel.enabled == True)
                result = await session.execute(stmt)
                return [email for email in result.scalars().all()]
        except Exception as e:
            logger.error(f"Failed to get enabled emails: {e}")
            return []

    @classmethod
    async def get_by_email(cls, email: str) -> Optional[dict]:
        """通过邮箱获取人员"""
        try:
            async with get_session_context() as session:
                stmt = select(NotificationRecipientModel).where(NotificationRecipientModel.email == email)
                result = await session.execute(stmt)
                recipient = result.scalar_one_or_none()
                return recipient.to_dict() if recipient else None
        except Exception as e:
            logger.error(f"Failed to get recipient by email: {email}, error: {e}")
            return None

    @classmethod
    async def create(cls, data: dict) -> dict:
        """创建通知人员"""
        try:
            async with get_session_context() as session:
                recipient = NotificationRecipientModel(**data)
                session.add(recipient)
                await session.flush()
                await session.refresh(recipient)
                logger.info(f"已创建通知人员: {recipient.name} ({recipient.email})")
                return recipient.to_dict()
        except Exception as e:
            logger.error(f"Failed to create recipient: {e}")
            raise e

    @classmethod
    async def update(cls, recipient_id: int, data: dict) -> Optional[dict]:
        """更新通知人员"""
        try:
            async with get_session_context() as session:
                stmt = select(NotificationRecipientModel).where(NotificationRecipientModel.id == recipient_id)
                result = await session.execute(stmt)
                recipient = result.scalar_one_or_none()
                if not recipient:
                    return None
                
                for key, value in data.items():
                    if hasattr(recipient, key):
                        setattr(recipient, key, value)
                
                await session.flush()
                await session.refresh(recipient)
                logger.info(f"已更新通知人员: {recipient.name}")
                return recipient.to_dict()
        except Exception as e:
            logger.error(f"Failed to update recipient {recipient_id}: {e}")
            return None

    @classmethod
    async def delete(cls, recipient_id: int) -> bool:
        """删除通知人员"""
        try:
            async with get_session_context() as session:
                from sqlalchemy import delete as sqlalchemy_delete
                stmt = sqlalchemy_delete(NotificationRecipientModel).where(NotificationRecipientModel.id == recipient_id)
                result = await session.execute(stmt)
                await session.commit()
                logger.info(f"已删除通知人员: ID={recipient_id}")
                return result.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete recipient {recipient_id}: {e}")
            return False


class NotificationService:
    """
    通知服务 - 管理所有通知渠道
    
    功能：
    - 渠道管理：CRUD 操作
    - 发送限制：时间范围、间隔、每日上限
    - 通知发送：支持邮箱和 Webhook
    """
    
    @staticmethod
    async def get_all_channels() -> List[dict]:
        """获取所有通知渠道"""
        async with get_session_context() as session:
            result = await session.execute(
                select(NotificationChannelModel)
            )
            channels = result.scalars().all()
            return [ch.to_dict() for ch in channels]
    
    @staticmethod
    async def get_enabled_channels() -> List[dict]:
        """获取所有启用的通知渠道"""
        async with get_session_context() as session:
            result = await session.execute(
                select(NotificationChannelModel).where(
                    NotificationChannelModel.enabled == True
                )
            )
            channels = result.scalars().all()
            return [ch.to_dict_full() for ch in channels]
    
    @staticmethod
    async def get_channel_by_id(channel_id: int) -> Optional[dict]:
        """根据ID获取通知渠道"""
        async with get_session_context() as session:
            result = await session.execute(
                select(NotificationChannelModel).where(
                    NotificationChannelModel.id == channel_id
                )
            )
            channel = result.scalar_one_or_none()
            return channel.to_dict_full() if channel else None
    
    @staticmethod
    async def get_channel_by_name(name: str) -> Optional[dict]:
        """根据名称获取通知渠道"""
        async with get_session_context() as session:
            result = await session.execute(
                select(NotificationChannelModel).where(
                    NotificationChannelModel.name == name
                )
            )
            channel = result.scalar_one_or_none()
            return channel.to_dict_full() if channel else None
    
    @staticmethod
    async def create_channel(data: dict) -> dict:
        """创建通知渠道"""
        async with get_session_context() as session:
            channel = NotificationChannelModel(**data)
            session.add(channel)
            await session.flush()
            await session.refresh(channel)
            logger.info(f"已创建通知渠道: {channel.name}")
            return channel.to_dict()
    
    @staticmethod
    async def update_channel(channel_id: int, data: dict) -> Optional[dict]:
        """更新通知渠道"""
        async with get_session_context() as session:
            result = await session.execute(
                select(NotificationChannelModel).where(
                    NotificationChannelModel.id == channel_id
                )
            )
            channel = result.scalar_one_or_none()
            if not channel:
                return None
            
            for key, value in data.items():
                if value is not None and hasattr(channel, key):
                    setattr(channel, key, value)
            
            await session.flush()
            await session.refresh(channel)
            logger.info(f"已更新通知渠道: {channel.name}")
            return channel.to_dict()
    
    @staticmethod
    async def delete_channel(channel_id: int) -> bool:
        """删除通知渠道"""
        async with get_session_context() as session:
            result = await session.execute(
                select(NotificationChannelModel).where(
                    NotificationChannelModel.id == channel_id
                )
            )
            channel = result.scalar_one_or_none()
            if not channel:
                return False
            
            await session.delete(channel)
            logger.info(f"已删除通知渠道: {channel.name}")
            return True
    
    @staticmethod
    async def update_channel_status(
        channel_id: int, 
        success: bool, 
        error: Optional[str] = None
    ):
        """更新通知渠道发送状态"""
        async with get_session_context() as session:
            result = await session.execute(
                select(NotificationChannelModel).where(
                    NotificationChannelModel.id == channel_id
                )
            )
            channel = result.scalar_one_or_none()
            if channel:
                channel.last_sent_at = utcnow()
                channel.send_count += 1
                if not success:
                    channel.error_count += 1
                    channel.last_error = error
                else:
                    channel.last_error = None
    
    @classmethod
    async def check_send_limits(
        cls,
        channel: dict,
        node_name: Optional[str] = None,
        skip_daily_limit: bool = False,
    ) -> tuple[bool, str]:
        """
        检查发送限制

        Args:
            channel: 渠道配置
            node_name: 节点名称
            skip_daily_limit: 跳过每日总量上限。仅用于升级提醒 ——
                故障长时间未恢复时，送达优先于抑制重复。
                最小间隔与可发送时段仍然生效，避免瞬时刷屏。

        Returns:
            (是否允许发送, 限制原因)
        """
        channel_id = channel.get("id")
        
        # 1. 检查时间范围
        send_time_start = channel.get("send_time_start")
        send_time_end = channel.get("send_time_end")
        
        if send_time_start and send_time_end:
            now_time = datetime.now().time()
            # 解析时间字符串
            if isinstance(send_time_start, str):
                start_time = datetime.strptime(send_time_start, "%H:%M").time()
            else:
                start_time = send_time_start
            if isinstance(send_time_end, str):
                end_time = datetime.strptime(send_time_end, "%H:%M").time()
            else:
                end_time = send_time_end
            
            # 如果两个时间都是 00:00:00，表示不限制
            if start_time == time(0, 0, 0) and end_time == time(0, 0, 0):
                pass  # 不限制时间
            # 如果两个时间相同且不是 00:00，表示只能在这个时间点发送
            elif start_time == end_time:
                if now_time.hour != start_time.hour or now_time.minute != start_time.minute:
                    return False, f"当前时间不在允许发送时段（{start_time.strftime('%H:%M')}）"
            # 正常时间段（如 09:00 - 18:00）
            elif start_time <= end_time:
                if not (start_time <= now_time <= end_time):
                    return False, f"当前时间不在允许发送时段（{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}）"
            # 跨夜时间段（如 22:00 - 08:00）
            else:
                if not (now_time >= start_time or now_time <= end_time):
                    return False, f"当前时间不在允许发送时段（{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}）"
        
        # 2. 检查最小发送间隔
        min_interval = channel.get("min_interval_seconds", 10)
        if min_interval and min_interval > 0:
            last_send_time = await NotificationLogService.get_last_send_time(channel_id, node_name)
            if last_send_time:
                # 使用本地时间进行比较（因为数据库存储的是本地时间）
                elapsed = (datetime.now() - last_send_time).total_seconds()
                if elapsed < min_interval:
                    return False, f"发送间隔太短，需等待 {int(min_interval - elapsed)} 秒"
        
        # 3. 检查单节点每日发送上限
        # 升级提醒跳过此项：故障长时间未恢复时，送达优先于抑制
        if node_name and not skip_daily_limit:
            daily_limit = channel.get("daily_limit_per_node", 10)
            if daily_limit and daily_limit > 0:
                today_count = await NotificationLogService.get_today_count_for_node(channel_id, node_name)
                if today_count >= daily_limit:
                    return False, f"节点 {node_name} 今日发送已达上限（{daily_limit}次）"

        return True, ""
    
    @classmethod
    async def send_notification(
        cls,
        channel_id: int,
        subject: str,
        message: str,
        alert_type: str = "manual",
        node_name: Optional[str] = None,
        node_url: Optional[str] = None,
        check_limits: bool = True,
        escalated: bool = False,
    ) -> tuple[bool, str]:
        """
        通过指定渠道发送通知

        Args:
            channel_id: 渠道ID
            subject: 通知主题
            message: 通知消息
            alert_type: 告警类型
            node_name: 节点名称
            node_url: 节点URL
            check_limits: 是否检查发送限制
            escalated: 是否升级提醒。升级时跳过"每日发送上限"——
                       故障长时间未恢复，送达的重要性高于避免打扰。

        Returns:
            (是否成功, 错误消息)
        """
        start_time = time_module.time()

        channel = await cls.get_channel_by_id(channel_id)
        if not channel:
            return False, "通知渠道不存在"

        if not channel.get("enabled"):
            return False, "通知渠道已禁用"

        # 检查发送限制（测试邮件不检查）
        if check_limits and alert_type != "test":
            # 升级提醒仍需受"最小间隔"与"可发送时段"约束（避免瞬时刷屏），
            # 但放开每日总量上限 —— 那是为抑制重复提醒设计的。
            can_send, limit_reason = await cls.check_send_limits(
                channel, node_name, skip_daily_limit=escalated
            )
            if not can_send:
                # 记录被限制的发送
                await NotificationLogService.add_log(
                    channel_id=channel_id,
                    channel_name=channel.get("name", ""),
                    alert_type=alert_type,
                    success=False,
                    node_name=node_name,
                    node_url=node_url,
                    subject=subject,
                    error_message=f"发送被限制: {limit_reason}",
                )
                return False, limit_reason
        
        channel_type = channel.get("channel_type")
        recipients_str = ""
        
        if channel_type == "email":
            # 邮箱通知
            smtp_host = channel.get("smtp_host")
            smtp_port = channel.get("smtp_port")
            smtp_user = channel.get("smtp_user")
            smtp_password = channel.get("smtp_password")
            recipients_str = channel.get("email_recipients", "")
            use_ssl = channel.get("smtp_ssl", True)
            
            if not all([smtp_host, smtp_port, smtp_user, smtp_password]):
                return False, "邮箱配置不完整"
            
            if alert_type == "test":
                # 测试模式：仅使用渠道中配置的测试收件人
                recipients = [r.strip() for r in recipients_str.split(",") if r.strip()]
                if not recipients:
                    return False, "请填写测试收件人邮箱"
            else:
                # 正常告警：仅使用“通知人员”列表中的邮箱
                recipients = await NotificationRecipientService.get_enabled_emails()
                if not recipients:
                    error_msg = "通知人员列表为空，请先在'通知人员'标签页添加收件人"
                    logger.warning(f"通知发送失败 (channel={channel.get('name')}): {error_msg}")
                    return False, error_msg
                logger.info(f"正常告警通知将发送至 {len(recipients)} 个启用的通知人员")
            
            success, error = await EmailNotificationService.send_email(
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                smtp_password=smtp_password,
                recipients=recipients,
                subject=subject,
                body=message,
                use_ssl=use_ssl,
            )
            
            # 计算发送耗时
            send_duration_ms = (time_module.time() - start_time) * 1000
            
            # 更新发送状态
            await cls.update_channel_status(channel_id, success, error)
            
            # 记录发送日志
            await NotificationLogService.add_log(
                channel_id=channel_id,
                channel_name=channel.get("name", ""),
                alert_type=alert_type,
                success=success,
                node_name=node_name,
                node_url=node_url,
                subject=subject,
                message=message,
                recipients=", ".join(recipients),
                error_message=error if not success else None,
                send_duration_ms=send_duration_ms,
            )
            
            return success, error
        
        elif channel_type == "webhook":
            webhook_url = channel.get("webhook_url")
            webhook_secret = channel.get("webhook_secret")

            if not webhook_url:
                await cls.update_channel_status(
                    channel_id, False, "Webhook URL 未配置"
                )
                return False, "Webhook URL 未配置"

            payload = WebhookNotificationService._build_payload(
                alert_type=alert_type,
                node_name=node_name,
                node_url=node_url,
                subject=subject,
                message=message,
            )

            success, error = await WebhookNotificationService.send_webhook(
                url=webhook_url,
                payload=payload,
                secret=webhook_secret,
            )

            send_duration_ms = (time_module.time() - start_time) * 1000

            await cls.update_channel_status(channel_id, success, error)

            await NotificationLogService.add_log(
                channel_id=channel_id,
                channel_name=channel.get("name", ""),
                alert_type=alert_type,
                success=success,
                node_name=node_name,
                node_url=node_url,
                subject=subject,
                message=message,
                recipients=webhook_url,
                error_message=error if not success else None,
                send_duration_ms=send_duration_ms,
            )

            return success, error

        else:
            return False, f"不支持的渠道类型: {channel_type}"
    
    @classmethod
    async def send_alert(
        cls,
        alert_type: str,
        node_name: str,
        node_url: str,
        message: str,
        details: Optional[dict] = None,
    ):
        """
        发送告警通知到所有启用的渠道
        
        Args:
            alert_type: 告警类型 (node_down, node_recovery, latency_high)
            node_name: 节点名称
            node_url: 节点URL
            message: 告警消息
            details: 详细信息
        """
        channels = await cls.get_enabled_channels()
        
        for channel in channels:
            # 检查通知类型是否匹配
            if alert_type == "node_down" and not channel.get("notify_on_down"):
                continue
            if alert_type == "node_recovery" and not channel.get("notify_on_recovery"):
                continue
            if alert_type == "latency_high" and not channel.get("notify_on_latency"):
                continue
            
            # 构建邮件主题
            type_names = {
                "node_down": "节点离线",
                "node_recovery": "节点恢复",
                "latency_high": "延迟告警",
            }
            subject = f"[服务器监控] {type_names.get(alert_type, '告警')}: {node_name}"
            
            # 构建邮件正文
            body = f"""
告警类型: {type_names.get(alert_type, alert_type)}
节点名称: {node_name}
节点URL: {node_url}
触发时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

告警详情:
{message}
"""
            if details:
                body += f"\n附加信息:\n"
                for key, value in details.items():
                    body += f"  - {key}: {value}\n"
            
            try:
                success, error = await cls.send_notification(
                    channel_id=channel["id"],
                    subject=subject,
                    message=body,
                    alert_type=alert_type,
                    node_name=node_name,
                    node_url=node_url,
                )
                if success:
                    logger.info(f"告警已通过渠道 '{channel['name']}' 发送成功，跳过备用渠道: {alert_type} - {node_name}")
                    # 发送成功一个即停止尝试后续渠道
                    break
                else:
                    logger.warning(f"告警通过渠道 '{channel['name']}' 发送失败，将尝试下一个: {error}")
            except Exception as e:
                logger.error(f"通过渠道 '{channel['name']}' 发送告警时异常: {e}")


# 全局服务实例
_notification_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    """获取通知服务实例"""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service


def get_notification_log_service() -> NotificationLogService:
    """获取通知日志服务实例"""
    return NotificationLogService()
