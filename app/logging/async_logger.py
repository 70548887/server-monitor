"""
异步日志初始化（QueueHandler/QueueListener，JSON 格式）

功能说明：
    - 支持通过环境变量 LOG_LEVEL 控制日志级别
    - DEBUG: 全量日志，显示所有调试信息
    - INFO: 默认级别，显示一般运行信息
    - WARNING: 仅显示警告和错误日志（生产环境推荐）
    - 支持 JSON 和纯文本两种日志格式
    - 异步写入，不阻塞主线程

使用方法：
    1. 设置环境变量: LOG_LEVEL=DEBUG/INFO/WARNING
    2. 调用 init_async_logging() 初始化
"""

import os
import sys
import queue
import logging
import logging.handlers
from pathlib import Path
from typing import Optional

# ========== 支持的日志级别 ==========
# 从低到高: DEBUG(10) < INFO(20) < WARNING(30) < ERROR(40) < CRITICAL(50)
# 设置某个级别后，会显示该级别及以上的所有日志
VALID_LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

# 尝试导入 JSON 日志格式化器
try:
    from pythonjsonlogger import jsonlogger
    _HAS_JSON_LOGGER = True
except ImportError:
    _HAS_JSON_LOGGER = False

# 全局日志队列与监听器
_log_queue: queue.Queue = queue.Queue(maxsize=10000)
_listener: Optional[logging.handlers.QueueListener] = None


def get_log_level_from_env(default: str = "INFO") -> str:
    """
    从环境变量获取日志级别
    
    环境变量：
        LOG_LEVEL: 日志级别，可选值 DEBUG/INFO/WARNING/ERROR/CRITICAL
    
    Args:
        default: 默认日志级别（环境变量未设置或无效时使用）
    
    Returns:
        日志级别字符串（大写）
    
    示例：
        # 在 .env 或环境变量中设置:
        # LOG_LEVEL=DEBUG  -> 显示所有日志（全量调试）
        # LOG_LEVEL=WARNING -> 仅显示警告和错误
    """
    try:
        level = os.getenv('LOG_LEVEL', default).upper()
        if level in VALID_LOG_LEVELS:
            return level
        else:
            # 日志级别无效，输出警告并使用默认值
            print(f"[WARNING] Invalid LOG_LEVEL '{level}', using default '{default}'. "
                  f"Valid levels: {', '.join(VALID_LOG_LEVELS)}", file=sys.stderr)
            return default
    except Exception as e:
        # 捕获任何异常，确保日志系统能正常启动
        print(f"[ERROR] Failed to get LOG_LEVEL from env: {e}", file=sys.stderr)
        return default


class SimpleJsonFormatter(logging.Formatter):
    """简单 JSON 格式化器（备用）"""
    
    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime
        
        log_obj = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # 添加额外字段
        if hasattr(record, "node"):
            log_obj["node"] = record.node
        if hasattr(record, "url"):
            log_obj["url"] = record.url
        if hasattr(record, "latency_ms"):
            log_obj["latency_ms"] = record.latency_ms
        if hasattr(record, "status_code"):
            log_obj["status_code"] = record.status_code
        if hasattr(record, "error"):
            log_obj["error"] = record.error
        if hasattr(record, "trace_id"):
            log_obj["trace_id"] = record.trace_id
        
        # 异常信息
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_obj, ensure_ascii=False)


def init_async_logging(
    level: Optional[str] = None,
    log_format: str = "json",
    file_path: Optional[str] = None,
    max_size_mb: int = 100,
    backup_count: int = 7,
    queue_size: int = 10000,
) -> logging.handlers.QueueListener:
    """
    初始化异步日志系统
    
    支持通过环境变量 LOG_LEVEL 控制日志级别：
        - DEBUG: 全量日志，显示所有调试信息，适合开发调试
        - INFO: 显示一般运行信息（默认）
        - WARNING: 仅显示警告和错误日志，适合生产环境
    
    Args:
        level: 日志级别，如为 None 则从环境变量 LOG_LEVEL 读取
        log_format: 日志格式 (json 或 text)
        file_path: 日志文件路径（可选，不设置则不写文件）
        max_size_mb: 单个日志文件最大大小（MB）
        backup_count: 保留的日志文件数量
        queue_size: 日志队列大小
    
    Returns:
        QueueListener 实例
    
    示例：
        # 使用环境变量（推荐）
        init_async_logging()  # 自动读取 LOG_LEVEL 环境变量
        
        # 手动指定级别
        init_async_logging(level='DEBUG')  # 全量日志
        init_async_logging(level='WARNING')  # 仅警告和错误
    """
    global _log_queue, _listener
    
    try:
        # 优先使用传入的级别，否则从环境变量读取
        effective_level = level if level else get_log_level_from_env("INFO")
        
        # 重新创建队列
        _log_queue = queue.Queue(maxsize=queue_size)
        
        # 获取根日志器并设置级别
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, effective_level.upper(), logging.INFO))
        
        # 清除现有处理器，避免重复日志
        root_logger.handlers.clear()
        
        # 添加队列处理器到根日志器
        queue_handler = logging.handlers.QueueHandler(_log_queue)
        root_logger.addHandler(queue_handler)
        
        # 准备目标处理器列表
        handlers = []
        
        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, effective_level.upper(), logging.INFO))
        
        # 选择格式化器
        if log_format == "json":
            if _HAS_JSON_LOGGER:
                formatter = jsonlogger.JsonFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s",
                    timestamp=True,
                )
            else:
                formatter = SimpleJsonFormatter()
        else:
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)
        
        # 文件处理器（可选）
        if file_path:
            try:
                log_dir = Path(file_path).parent
                log_dir.mkdir(parents=True, exist_ok=True)
                
                file_handler = logging.handlers.RotatingFileHandler(
                    file_path,
                    maxBytes=max_size_mb * 1024 * 1024,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
                file_handler.setLevel(getattr(logging, effective_level.upper(), logging.INFO))
                file_handler.setFormatter(formatter)
                handlers.append(file_handler)
            except Exception as e:
                # 文件处理器创建失败，继续使用控制台输出
                print(f"[ERROR] Failed to create log file handler: {e}", file=sys.stderr)
        
        # 创建并启动队列监听器
        _listener = logging.handlers.QueueListener(
            _log_queue,
            *handlers,
            respect_handler_level=True,
        )
        _listener.start()
        
        logging.info(f"异步日志已初始化: 级别={effective_level}, 格式={log_format}")
        return _listener
        
    except Exception as e:
        # 日志初始化失败是严重错误，但不应影响应用启动
        print(f"[CRITICAL] Failed to initialize async logging: {e}", file=sys.stderr)
        # 回退到基本日志配置
        logging.basicConfig(level=logging.INFO)
        return None


def shutdown_logging():
    """
    关闭日志系统
    
    确保在应用退出前调用，以确保所有日志都被写入。
    """
    global _listener
    try:
        if _listener:
            _listener.stop()
            _listener = None
            logging.info("Async logging shutdown")
    except Exception as e:
        # 关闭日志失败不应影响应用退出
        print(f"[ERROR] Failed to shutdown logging: {e}", file=sys.stderr)


def get_logger(name: str) -> logging.Logger:
    """获取带有结构化字段支持的日志器"""
    return logging.getLogger(name)


class StructuredLoggerAdapter(logging.LoggerAdapter):
    """结构化日志适配器，支持额外字段"""
    
    def process(self, msg, kwargs):
        extra = kwargs.get("extra", {})
        extra.update(self.extra)
        kwargs["extra"] = extra
        return msg, kwargs


def get_structured_logger(name: str, **default_fields) -> StructuredLoggerAdapter:
    """获取结构化日志器，支持默认字段"""
    logger = logging.getLogger(name)
    return StructuredLoggerAdapter(logger, default_fields)
