"""配置加载与管理服务"""

import os
import logging
from pathlib import Path
from typing import Optional, Any
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

# 全局配置实例
_config: Optional["AppConfig"] = None


@dataclass
class DatabaseConfig:
    """数据库配置"""
    engine: str = "mysql"
    
    # MySQL
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "server_monitor"
    mysql_pool_size: int = 10
    mysql_pool_recycle: int = 1800
    
    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_prefix: str = "srvmon:"


@dataclass
class SchedulerConfig:
    """调度器配置"""
    interval_seconds: int = 60
    concurrency: int = 50
    timeout_seconds: float = 5.0
    retry_count: int = 3
    retry_delay_ms: int = 200


@dataclass
class AlertingConfig:
    """告警配置"""
    enabled: bool = True
    consecutive_failures: int = 3
    latency_p95_ms: int = 2000
    error_rate_threshold: float = 0.05
    suppress_minutes: int = 15
    window_minutes: int = 5
    notify_on_recovery: bool = True  # 节点恢复时是否发送通知
    # 告警抑制是否持久化到 Redis（跨进程重启保持，Redis 不可用时自动降级内存）
    suppress_persist: bool = True
    # ===== 事件化提醒（Phase 2）=====
    # 阶梯提醒节奏：距事件开始的时间点（分钟）。
    # [0, 60, 240, 480, 720] = 立即 / 1h / 5h / 13h / 25h，
    # 之后按 intervals_loop_minutes 循环。节奏随故障持续时长递减，
    # 不再受自然日边界影响。
    reminder_intervals: tuple = (0, 60, 240, 480, 720)
    intervals_loop_minutes: int = 720   # 走完阶梯后的循环间隔（12h）
    # 单个事件的通知次数上限，0 表示不限（由阶梯自然收敛）
    max_notifications_per_incident: int = 0
    # ===== 静默时段（Phase 3）=====
    # 夜间不打扰：落在静默期内的提醒顺延到结束时刻合并发出。
    # 恢复通知不受限制（好消息应当立即送达，且用于关闭事件）。
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"
    webhook_enabled: bool = False
    webhook_url: str = ""
    email_enabled: bool = False


@dataclass
class LoggingConfig:
    """日志配置"""
    level: str = "INFO"
    format: str = "json"
    file_enabled: bool = True
    file_path: str = "logs/app.log"
    max_size_mb: int = 100
    backup_count: int = 7
    async_queue_size: int = 10000


@dataclass
class ServerConfig:
    """服务器配置"""
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class RetentionConfig:
    """
    数据留存配置

    样本表按每分钟每节点一行增长，100 个节点一年约 5000 万行。
    此前 app.yaml 里配了 retention 段但没有对应代码解析，
    清理方法也从未被调度，样本表会无限增长。
    """
    samples_days: int = 30        # 样本保留天数
    aggregates_days: int = 90     # 聚合数据保留天数
    cleanup_hour: int = 3         # 每日清理时刻（0-23）
    enabled: bool = True          # 是否启用自动清理


@dataclass
class NodeExpectConfig:
    """节点期望配置"""
    status: int = 200
    body_contains: Optional[str] = None


@dataclass
class NodeConfig:
    """节点配置"""
    name: str
    url: str
    region: str = "default"
    tags: list = field(default_factory=list)
    timeout_ms: int = 2000
    enabled: bool = True
    expect: NodeExpectConfig = field(default_factory=NodeExpectConfig)


@dataclass
class AppConfig:
    """应用配置"""
    name: str = "server-monitor"
    version: str = "1.0.0"
    debug: bool = False
    
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    alerting: AlertingConfig = field(default_factory=AlertingConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    
    nodes: list[NodeConfig] = field(default_factory=list)


def _parse_database_config(data: dict) -> DatabaseConfig:
    """
    解析数据库配置
    
    优先级：环境变量 > YAML 配置 > 默认值
    
    支持的环境变量：
        MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
        REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB
    """
    config = DatabaseConfig()
    config.engine = data.get("engine", "mysql")
    
    # ========== MySQL 配置（环境变量优先） ==========
    mysql = data.get("mysql", {})
    config.mysql_host = os.getenv("MYSQL_HOST", mysql.get("host", "localhost"))
    config.mysql_port = int(os.getenv("MYSQL_PORT", mysql.get("port", 3306)))
    config.mysql_user = os.getenv("MYSQL_USER", mysql.get("user", "root"))
    config.mysql_password = os.getenv("MYSQL_PASSWORD", mysql.get("password", ""))
    config.mysql_database = os.getenv("MYSQL_DATABASE", mysql.get("database", "server_monitor"))
    config.mysql_pool_size = int(os.getenv("MYSQL_POOL_SIZE", mysql.get("pool_size", 10)))
    config.mysql_pool_recycle = int(os.getenv("MYSQL_POOL_RECYCLE", mysql.get("pool_recycle", 1800)))
    
    # ========== Redis 配置（环境变量优先） ==========
    redis = data.get("redis", {})
    config.redis_host = os.getenv("REDIS_HOST", redis.get("host", "localhost"))
    config.redis_port = int(os.getenv("REDIS_PORT", redis.get("port", 6379)))
    config.redis_db = int(os.getenv("REDIS_DB", redis.get("db", 0)))
    config.redis_password = os.getenv("REDIS_PASSWORD", redis.get("password", ""))
    config.redis_prefix = os.getenv("REDIS_PREFIX", redis.get("prefix", "srvmon:"))
    
    # 记录配置来源（调试用）
    if os.getenv("MYSQL_HOST") or os.getenv("MYSQL_PASSWORD"):
        logger.info("Database config loaded from environment variables")
    
    return config


def _parse_node_config(data: dict) -> NodeConfig:
    """解析节点配置"""
    expect_data = data.get("expect", {})
    expect = NodeExpectConfig(
        status=expect_data.get("status", 200),
        body_contains=expect_data.get("body_contains"),
    )
    
    return NodeConfig(
        name=data["name"],
        url=data["url"],
        region=data.get("region", "default"),
        tags=data.get("tags", []),
        timeout_ms=data.get("timeout_ms", 2000),
        enabled=data.get("enabled", True),
        expect=expect,
    )


def load_config(
    app_config_path: str = "configs/app.yaml",
    nodes_config_path: str = "configs/nodes.yaml",
) -> AppConfig:
    """
    加载应用配置
    
    Args:
        app_config_path: 应用配置文件路径
        nodes_config_path: 节点配置文件路径
    
    Returns:
        AppConfig 实例
    """
    global _config
    
    config = AppConfig()
    
    # 加载应用配置
    app_path = Path(app_config_path)
    if app_path.exists():
        with open(app_path, "r", encoding="utf-8") as f:
            app_data = yaml.safe_load(f) or {}
        
        # 应用信息
        app_info = app_data.get("app", {})
        config.name = app_info.get("name", "server-monitor")
        config.version = app_info.get("version", "1.0.0")
        config.debug = app_info.get("debug", False)
        
        # 数据库配置
        if "database" in app_data:
            config.database = _parse_database_config(app_data["database"])
        
        # 调度器配置
        sched = app_data.get("scheduler", {})
        config.scheduler = SchedulerConfig(
            interval_seconds=sched.get("interval_seconds", 60),
            concurrency=sched.get("concurrency", 50),
            timeout_seconds=sched.get("timeout_seconds", 5.0),
            retry_count=sched.get("retry_count", 3),
            retry_delay_ms=sched.get("retry_delay_ms", 200),
        )
        
        # 告警配置
        alert = app_data.get("alerting", {})
        channels = alert.get("channels", {})
        config.alerting = AlertingConfig(
            enabled=alert.get("enabled", True),
            consecutive_failures=alert.get("consecutive_failures", 3),
            latency_p95_ms=alert.get("latency_p95_ms", 2000),
            error_rate_threshold=alert.get("error_rate_threshold", 0.05),
            suppress_minutes=alert.get("suppress_minutes", 15),
            window_minutes=alert.get("window_minutes", 5),
            suppress_persist=alert.get("suppress_persist", True),
            reminder_intervals=tuple(
                alert.get("reminder_intervals_minutes", [0, 60, 240, 480, 720])
            ),
            intervals_loop_minutes=alert.get("intervals_loop_minutes", 720),
            max_notifications_per_incident=alert.get("max_notifications_per_incident", 0),
            quiet_hours_enabled=alert.get("quiet_hours", {}).get("enabled", False),
            quiet_hours_start=alert.get("quiet_hours", {}).get("start", "23:00"),
            quiet_hours_end=alert.get("quiet_hours", {}).get("end", "07:00"),
            webhook_enabled=channels.get("webhook", {}).get("enabled", False),
            webhook_url=channels.get("webhook", {}).get("url", ""),
            email_enabled=channels.get("email", {}).get("enabled", False),
        )
        
        # 日志配置
        log = app_data.get("logging", {})
        log_file = log.get("file", {})
        config.logging = LoggingConfig(
            level=log.get("level", "INFO"),
            format=log.get("format", "json"),
            file_enabled=log_file.get("enabled", True),
            file_path=log_file.get("path", "logs/app.log"),
            max_size_mb=log_file.get("max_size_mb", 100),
            backup_count=log_file.get("backup_count", 7),
            async_queue_size=log.get("async_queue_size", 10000),
        )
        
        # 服务器配置
        server = app_data.get("server", {})
        config.server = ServerConfig(
            host=server.get("host", "0.0.0.0"),
            port=server.get("port", 8080),
        )

        # 数据留存配置
        retention = app_data.get("retention", {})
        config.retention = RetentionConfig(
            samples_days=retention.get("samples_days", 30),
            aggregates_days=retention.get("aggregates_days", 90),
            cleanup_hour=retention.get("cleanup_hour", 3),
            enabled=retention.get("enabled", True),
        )
        
        logger.info(f"已加载应用配置: {app_config_path}")
    else:
        logger.warning(f"App config not found: {app_config_path}, using defaults")
    
    # 加载节点配置
    nodes_path = Path(nodes_config_path)
    if nodes_path.exists():
        with open(nodes_path, "r", encoding="utf-8") as f:
            nodes_data = yaml.safe_load(f) or {}
        
        nodes_list = nodes_data.get("nodes", [])
        config.nodes = [_parse_node_config(n) for n in nodes_list]
        logger.info(f"已加载 {len(config.nodes)} 个节点配置: {nodes_config_path}")
    else:
        logger.warning(f"Nodes config not found: {nodes_config_path}")
    
    _config = config
    return config


def get_config() -> AppConfig:
    """获取当前配置实例"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _yaml_scalar(value) -> str:
    """把标量渲染为 YAML 字面量"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        # 时间这类含冒号的值必须加引号，否则会被 YAML 解析成字典
        if value == "" or ":" in value or value.strip() != value:
            return f'"{value}"'
        return value
    return str(value)


def _render_alerting_block(alerting: dict) -> str:
    """把 alerting 字典渲染成 YAML 文本块（用于整段追加，保留行内注释时不走此路径）"""
    lines = ["alerting:"]
    for key, value in alerting.items():
        if isinstance(value, dict):
            lines.append(f"  {key}:")
            for sub_key, sub_val in value.items():
                lines.append(f"    {sub_key}: {_yaml_scalar(sub_val)}")
        elif isinstance(value, list):
            rendered = ", ".join(_yaml_scalar(v) for v in value)
            lines.append(f"  {key}: [{rendered}]")
        else:
            lines.append(f"  {key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def _patch_alerting_lines(lines: list, new_values: dict) -> tuple:
    """
    在 alerting 段内逐键替换值，保留所有其他行（含注释与空行）

    整段重写会抹掉段内的说明注释，而配置文件里的注释正是运维
    理解各项含义的唯一线索，因此改为：只替换被管理键所在的行，
    其余行一字不动。缺失的键追加到段末。

    Args:
        lines: 文件的全部行（含换行符）
        new_values: 键 -> 新值；值为 dict 时按子键处理

    Returns:
        (修改后的行列表, 实际替换的键集合)
    """
    start_idx = None
    end_idx = len(lines)

    for i, line in enumerate(lines):
        if line.startswith("alerting:"):
            start_idx = i
            break

    if start_idx is None:
        # 文件里没有 alerting 段：在末尾追加
        block = _render_alerting_block(new_values)
        for line in reversed(block.splitlines(keepends=True)):
            lines.insert(len(lines), line)
        return lines, set(new_values.keys())

    for j in range(start_idx + 1, len(lines)):
        if lines[j].strip() and not lines[j][0].isspace():
            end_idx = j
            break

    replaced = set()
    pending = dict(new_values)

    i = start_idx + 1
    while i < end_idx:
        raw = lines[i]
        stripped = raw.strip()
        # 跳过注释与空行
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        indent = len(raw) - len(raw.lstrip())
        if ":" not in stripped:
            i += 1
            continue

        key = stripped.split(":", 1)[0].strip()

        if indent == 2 and key in pending:
            value = pending.pop(key)
            if isinstance(value, dict):
                # 嵌套块：找到其子行范围，整体替换
                sub_end = i + 1
                while sub_end < end_idx:
                    sub_raw = lines[sub_end]
                    sub_stripped = sub_raw.strip()
                    if sub_stripped and not sub_stripped.startswith("#"):
                        sub_indent = len(sub_raw) - len(sub_raw.lstrip())
                        if sub_indent <= indent:
                            break
                    sub_end += 1
                rendered = [f"  {key}:\n"]
                for sub_key, sub_val in value.items():
                    rendered.append(f"    {sub_key}: {_yaml_scalar(sub_val)}\n")
                lines[i:sub_end] = rendered
                delta = len(rendered) - (sub_end - i)
                end_idx += delta
                i += len(rendered)
                replaced.add(key)
                continue
            else:
                if isinstance(value, list):
                    rendered_val = "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
                else:
                    rendered_val = _yaml_scalar(value)
                lines[i] = f"  {key}: {rendered_val}\n"
                replaced.add(key)
        i += 1

    # 未被文件覆盖的新键，追加到段末
    if pending:
        additions = []
        for key, value in pending.items():
            if isinstance(value, dict):
                additions.append(f"  {key}:\n")
                for sub_key, sub_val in value.items():
                    additions.append(f"    {sub_key}: {_yaml_scalar(sub_val)}\n")
            elif isinstance(value, list):
                additions.append(
                    f"  {key}: [" + ", ".join(_yaml_scalar(v) for v in value) + "]\n"
                )
            else:
                additions.append(f"  {key}: {_yaml_scalar(value)}\n")
        lines[end_idx:end_idx] = additions
        replaced.update(pending.keys())

    return lines, replaced


def save_alerting_config(app_config_path: str = "configs/app.yaml") -> bool:
    """
    把当前告警配置写回 app.yaml

    面板上的告警设置只改了内存中的配置对象，若不落盘，
    服务重启后会回到文件里的旧值 —— 用户会以为"没保存成功"。

    实现上只改 alerting 段内被管理键所在的行，其余行——包括所有注释——
    原样保留。早期实现用 yaml.safe_dump 重写整个文件，会把文件里所有
    注释抹掉（实测丢失 21 行说明），段内注释更是运维理解配置的唯一线索，
    因此改为逐键替换。
    写入采用"临时文件 + 原子替换"，并在写前校验可解析，
    避免中途失败损坏配置。

    Returns:
        是否写入成功
    """
    global _config

    if _config is None:
        logger.warning("配置尚未加载，无法保存")
        return False

    app_path = Path(app_config_path)
    if not app_path.exists():
        logger.warning(f"App config not found: {app_config_path}")
        return False

    try:
        text = app_path.read_text(encoding="utf-8")

        a = _config.alerting
        new_values = {
            "enabled": a.enabled,
            "consecutive_failures": a.consecutive_failures,
            "latency_p95_ms": a.latency_p95_ms,
            "error_rate_threshold": a.error_rate_threshold,
            "suppress_minutes": a.suppress_minutes,
            "suppress_persist": a.suppress_persist,
            "window_minutes": a.window_minutes,
            "reminder_intervals_minutes": list(a.reminder_intervals),
            "intervals_loop_minutes": a.intervals_loop_minutes,
            "max_notifications_per_incident": a.max_notifications_per_incident,
            "quiet_hours": {
                "enabled": a.quiet_hours_enabled,
                "start": a.quiet_hours_start,
                "end": a.quiet_hours_end,
            },
        }

        lines = text.splitlines(keepends=True)
        lines, replaced = _patch_alerting_lines(lines, new_values)
        new_text = "".join(lines)

        # 写前校验，避免落下无法解析的配置
        yaml.safe_load(new_text)

        tmp_path = app_path.with_suffix(app_path.suffix + ".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(app_path)

        logger.info(
            f"告警配置已写回 {app_config_path}，更新 {len(replaced)} 项"
        )
        return True
    except Exception as e:
        logger.error(f"保存告警配置失败: {e}")
        return False


def reload_nodes(nodes_config_path: str = "configs/nodes.yaml") -> list[NodeConfig]:
    """热重载节点配置"""
    global _config
    
    nodes_path = Path(nodes_config_path)
    if not nodes_path.exists():
        logger.warning(f"Nodes config not found: {nodes_config_path}")
        return []
    
    with open(nodes_path, "r", encoding="utf-8") as f:
        nodes_data = yaml.safe_load(f) or {}
    
    nodes_list = nodes_data.get("nodes", [])
    nodes = [_parse_node_config(n) for n in nodes_list]
    
    if _config:
        _config.nodes = nodes
    
    logger.info(f"Reloaded {len(nodes)} nodes from {nodes_config_path}")
    return nodes
