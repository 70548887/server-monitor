"""
时间工具

项目的时间约定（重要，改动前务必确认）：
    - **存储层**：数据库列为朴素 DateTime（未启用 timezone=True），
      统一写入 **UTC 朴素时间**。日志里可以直接看到这个约定：
      asctime 是本地时间，而 timestamp 字段是 UTC。
    - **展示层**：静默时段、发送时段这类配置由用户按**本地时刻**填写，
      判定前必须先换算，见 to_local_time()。

为什么不直接用 datetime.now(timezone.utc)：
    数据库列是朴素 DateTime，写入带时区的对象会让 SQLAlchemy 静默
    转换或直接报错，且与已有数据混用会产生 8 小时偏差。这里保留
    朴素 UTC 表示，只消除 utcnow() 的弃用警告，行为完全不变。
"""

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """
    当前 UTC 时间（朴素）

    等价于已弃用的 datetime.utcnow()，但不会触发弃用警告。
    Python 3.12 起 utcnow() 已被标记弃用，未来版本会移除。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_aware() -> datetime:
    """当前 UTC 时间（带时区），用于需要显式标注时区的场景"""
    return datetime.now(timezone.utc)


def to_local_time(
    ts: Optional[datetime],
    assume_utc: bool = True,
) -> Optional[datetime]:
    """
    把时间戳转换为本地时间，供"按本地时刻"的判定使用

    心跳时间戳是 UTC 朴素时间，而静默时段这类配置由用户按本地时刻
    填写。两者直接比较会整体错位 —— 在 UTC+8 上，拿 UTC 时间戳去套
    23:00-07:00，实际静默的是本地 07:00-15:00，与用户意图恰好相反。

    Args:
        ts: 待转换的时间戳
        assume_utc: 朴素时间戳是否按 UTC 解释。默认 True，对应心跳与
                    数据库的写入约定；调用方已持有本地时间时传 False。
    """
    if ts is None:
        return None
    if ts.tzinfo is not None:
        # 已带时区：直接换算
        return ts.astimezone()
    if assume_utc:
        return ts.replace(tzinfo=timezone.utc).astimezone()
    # 已是本地朴素时间，无需转换
    return ts


def to_utc_naive(ts: Optional[datetime], assume_utc: bool = True) -> Optional[datetime]:
    """
    把时间戳转换为 UTC 朴素时间，与数据库的存储约定一致

    Args:
        ts: 待转换的时间戳
        assume_utc: 朴素时间戳是否已按 UTC 解释
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts if assume_utc else ts.astimezone(
            timezone.utc
        ).replace(tzinfo=None)
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def format_iso_utc(ts: Optional[datetime]) -> Optional[str]:
    """格式化为带 Z 后缀的 ISO 字符串，用于 JSON 输出"""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")