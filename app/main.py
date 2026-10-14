"""
服务器监控面板 - FastAPI 主入口

启动命令: uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
"""

import os
import sys
import time
import logging
from pathlib import Path
from contextlib import asynccontextmanager

# ========== 加载环境变量 ==========
# .env 的加载已下沉到 app/services/config.py（load_config 内部调用），
# 保证迁移脚本、引导脚本等非 main 入口也能读到环境变量。
# 这里保留 main 的显式加载，以便应用启动最早阶段即可使用，
# 且不影响功能（load_dotenv 默认不覆盖已存在的环境变量）。
try:
    from dotenv import load_dotenv
    # 确定项目根目录
    _project_root = Path(__file__).parent.parent
    _env_file = _project_root / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
        print(f"[信息] 已加载环境变量: {_env_file}")
except ImportError:
    print("[警告] python-dotenv 未安装，.env 文件将不会被加载")

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse

# 确保项目根目录在 Python 路径中
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
os.chdir(project_root)

from app import __version__
from app.logging import init_async_logging, shutdown_logging
from app.services.config import load_config, get_config
from app.services.scheduler import get_scheduler
from app.services.observability import metrics
from app.services.availability import get_availability_service, HeartbeatRecord
from app.services.auth import create_default_admin
from app.routes import health_router, nodes_router, metrics_router, ui_router
from app.routes.auth import router as auth_router
from app.routes.notifications import router as notifications_router
from app.routes.updates import router as updates_router
from app.routes.nodes import update_node_status_from_result, result_is_current
from app.middleware.auth import AuthMiddleware
from app.middleware.security import SecurityMiddleware, TrustedHostMiddleware

logger = logging.getLogger(__name__)

# 数据库是否可用标志
_db_available = False


async def _init_database(config):
    """
    初始化数据库
    
    1. 初始化数据库连接，建表并补齐已发布的缺失字段和索引
    2. 将 YAML 配置同步到数据库
    3. 从数据库加载历史心跳记录
    """
    global _db_available
    
    logger.info("正在初始化数据库并检查表结构...")
    _db_available = False
    from app.models.schema import SchemaMigrationError
    
    try:
        from app.models.base import init_db
        from app.models.admin import Admin  # 确保 Admin 表会被创建
        from app.repositories.mysql_repository import get_mysql_repository
        
        # 初始化数据库并完成增量升级；此后才能启动仓库和调度器
        schema_started = time.perf_counter()
        await init_db(
            host=config.database.mysql_host,
            port=config.database.mysql_port,
            user=config.database.mysql_user,
            password=config.database.mysql_password,
            database=config.database.mysql_database,
            pool_size=config.database.mysql_pool_size,
            pool_recycle=config.database.mysql_pool_recycle,
            create_tables=True,
        )
        
        _db_available = True
        logger.info("数据库初始化完成，表结构已检查并补齐，耗时=%.2f秒", time.perf_counter() - schema_started)
        
        # 获取仓库实例
        repo = get_mysql_repository()
        
        # 检查数据库中是否已有节点
        existing_nodes = await repo.get_all_nodes()
        
        if existing_nodes:
            # 数据库中已有节点，使用数据库数据为主
            logger.info(f"数据库中已有 {len(existing_nodes)} 个节点，使用数据库为主数据源")
        else:
            # 数据库为空，从 YAML 导入初始节点
            yaml_nodes = config.nodes
            if yaml_nodes:
                logger.info(f"数据库为空，正在从 YAML 导入 {len(yaml_nodes)} 个节点...")
                for node in yaml_nodes:
                    await repo.upsert_node({
                        "name": node.name,
                        "url": node.url,
                        "region": node.region,
                        "tags": node.tags,
                        "timeout_ms": node.timeout_ms,
                        "enabled": node.enabled,
                        "expect": {
                            "status": node.expect.status,
                            "body_contains": node.expect.body_contains,
                        },
                    })
                logger.info("已从 YAML 导入初始节点")
            else:
                logger.info("数据库和 YAML 中都没有节点")
        
        # 从数据库加载历史心跳记录到可用性服务
        logger.info("正在从数据库加载历史样本...")
        history_started = time.perf_counter()
        samples = await repo.get_recent_samples(hours=24)  # 加载最近24小时的数据
        
        if samples:
            from app.services.availability import get_availability_service, HeartbeatRecord
            availability_service = get_availability_service()
            records = [
                HeartbeatRecord(
                    node_name=s["node_name"],
                    timestamp=s["timestamp"],
                    success=s["success"],
                    latency_ms=s["latency_ms"],
                    status_code=s["status_code"],
                    error_type=s.get("error_type"),
                )
                for s in samples
            ]
            availability_service.add_records(records)
            logger.info("已加载 %s 条历史样本（查询及缓存合计 %.2f秒）", len(records), time.perf_counter() - history_started)
        else:
            logger.info("没有找到历史样本")
        
    except SchemaMigrationError:
        _db_available = False
        logger.exception("数据库结构升级未完成，已停止启动；修复数据库权限或错误后重启")
        raise
    except Exception as e:
        logger.warning(f"数据库初始化失败: {e}")
        logger.info("进入内存模式运行（数据不会持久化）")
        _db_available = False


async def _init_storage_health_check(config):
    """
    初始化存储层健康检测
    
    在应用启动时自动检测 MySQL 和 Redis 状态
    """
    global _db_available
    
    # Ping MySQL (异步)
    try:
        from app.models.base import ping_database
        mysql_ok, mysql_latency = await ping_database()
        metrics.record_storage_ping("mysql", mysql_ok, mysql_latency)
        _db_available = mysql_ok
        logger.info(f"MySQL 健康检查: 状态={'正常' if mysql_ok else '异常'}, 延迟={mysql_latency:.2f}ms")
    except Exception as e:
        logger.warning(f"MySQL 健康检查失败: {e}")
        metrics.record_storage_ping("mysql", False, 0)
    
    # Ping Redis (异步)
    try:
        if config.database.redis_host:
            from app.repositories.redis_repository import get_redis_repository
            redis_repo = get_redis_repository()
            redis_ok, redis_latency = await redis_repo.ping()
            metrics.record_storage_ping("redis", redis_ok, redis_latency)
            logger.info(f"Redis 健康检查: 状态={'正常' if redis_ok else '异常'}, 延迟={redis_latency:.2f}ms")
        else:
            logger.info("未配置 Redis，跳过健康检查")
    except Exception as e:
        logger.warning(f"Redis 健康检查失败: {e}")
        metrics.record_storage_ping("redis", False, 0)


async def _init_default_notification_channels():
    """
    初始化默认通知渠道
    
    从环境变量读取配置，如果配置完整则创建默认邮箱渠道。
    环境变量:
        - DEFAULT_EMAIL_NAME: 渠道名称
        - DEFAULT_SMTP_HOST: SMTP 服务器
        - DEFAULT_SMTP_PORT: SMTP 端口
        - DEFAULT_SMTP_USER: 发件人邮箱
        - DEFAULT_SMTP_PASSWORD: SMTP 密码
        - DEFAULT_EMAIL_RECIPIENTS: 收件人邮箱
    """
    import os
    from app.services.notification import get_notification_service
    
    # 从环境变量读取配置
    channel_name = os.getenv("DEFAULT_EMAIL_NAME")
    smtp_host = os.getenv("DEFAULT_SMTP_HOST")
    smtp_user = os.getenv("DEFAULT_SMTP_USER")
    smtp_password = os.getenv("DEFAULT_SMTP_PASSWORD")
    email_recipients = os.getenv("DEFAULT_EMAIL_RECIPIENTS")
    
    # 如果没有配置环境变量，跳过创建
    if not all([channel_name, smtp_host, smtp_user, smtp_password, email_recipients]):
        logger.info("未配置默认通知渠道（请设置 DEFAULT_EMAIL_* 环境变量）")
        return
    
    service = get_notification_service()
    
    # 检查是否已存在该渠道
    existing = await service.get_channel_by_name(channel_name)
    if existing:
        logger.info(f"默认通知渠道 '{channel_name}' 已存在")
        return
    
    # 创建预设的邮箱渠道
    channel_data = {
        "name": channel_name,
        "channel_type": "email",
        "enabled": True,
        "smtp_host": smtp_host,
        "smtp_port": int(os.getenv("DEFAULT_SMTP_PORT", "465")),
        "smtp_ssl": os.getenv("DEFAULT_SMTP_SSL", "true").lower() == "true",
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "email_recipients": email_recipients,
        "notify_on_down": True,
        "notify_on_recovery": True,
        "notify_on_latency": False,
    }
    
    await service.create_channel(channel_data)
    logger.info(f"已创建默认通知渠道: {channel_name}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # ========== 启动 ==========
    startup_started = time.perf_counter()
    logger.info("正在启动服务器监控应用...")
    
    # 加载配置
    config = load_config(
        app_config_path="configs/app.yaml",
        nodes_config_path="configs/nodes.yaml",
    )
    
    # ========== 初始化异步日志 ==========
    # 优先使用环境变量 LOG_LEVEL，否则使用配置文件中的设置
    # DEBUG: 全量日志，显示所有调试信息
    # WARNING: 仅警告和错误，适合生产环境
    log_level = os.getenv('LOG_LEVEL', config.logging.level)
    file_path = config.logging.file_path if config.logging.file_enabled else None
    init_async_logging(
        level=log_level,
        log_format=config.logging.format,
        file_path=file_path,
        max_size_mb=config.logging.max_size_mb,
        backup_count=config.logging.backup_count,
        queue_size=config.logging.async_queue_size,
    )
    
    # 设置应用信息到指标
    metrics.set_app_info(version=__version__, name=config.name)
    
    # ========== 初始化数据库 ==========
    await _init_database(config)
    
    # 启动调度器
    scheduler = get_scheduler()
    
    # 设置心跳结果回调 - 更新可用性数据、持久化、告警评估
    async def on_heartbeat_results(results):
        """心跳检测完成后的回调"""
        global _db_available
        availability_service = get_availability_service()
        records = []
        samples_to_save = []
        
        results = [r for r in results if update_node_status_from_result(r.node_name, r)]
        for r in results:
            # 创建可用性记录
            records.append(HeartbeatRecord(
                node_name=r.node_name,
                timestamp=r.timestamp,
                success=r.success,
                latency_ms=r.latency_ms,
                status_code=r.status_code,
                error_type=r.error_type,
            ))
            # 准备持久化数据
            samples_to_save.append({
                "node_name": r.node_name,
                "url": r.url,
                "timestamp": r.timestamp,
                "status_code": r.status_code,
                "latency_ms": r.latency_ms,
                "success": r.success,
                "error_type": r.error_type,
                "error_message": r.error_message,
                "response_size": r.response_size,
            })
        
        # 批量添加记录到内存
        availability_service.add_records(records)
        
        # 持久化到数据库 (异步)
        if _db_available:
            try:
                from app.repositories.mysql_repository import get_mysql_repository
                repo = get_mysql_repository()
                saved = await repo.save_samples_batch(samples_to_save)
                logger.debug(f"已持久化 {saved} 条样本到数据库")
            except Exception as e:
                logger.warning(f"持久化样本失败: {e}")
        
        # ========== 告警评估 ==========
        try:
            from app.services.alerting import get_alerting_service, process_alerts
            alerting_service = get_alerting_service()
            
            # 评估所有心跳结果，检查是否需要触发告警
            alert_events = await alerting_service.evaluate_batch(
                [r for r in results if result_is_current(r.node_name, r)]
            )
            
            if alert_events:
                logger.info(f"触发了 {len(alert_events)} 个告警: {[e.alert_type for e in alert_events]}")
                # 发送告警通知
                await process_alerts(alert_events)
        except Exception as e:
            logger.error(f"告警评估错误: {e}", exc_info=True)
        
        logger.debug(f"已更新 {len(records)} 个节点的可用性数据")
    
    scheduler.set_on_results_callback(on_heartbeat_results)
    scheduler.start()
    
    # ========== 初始化存储层检测 ==========
    logger.info("正在初始化存储层健康检查...")
    await _init_storage_health_check(config)
    
    # 输出数据库中的节点数量
    if _db_available:
        try:
            from app.repositories.mysql_repository import get_mysql_repository
            repo = get_mysql_repository()
            db_nodes = await repo.get_all_nodes()
            logger.info(f"已加载 {len(db_nodes)} 个监控节点（来自数据库）")
        except Exception as e:
            logger.warning(f"获取节点数量失败: {e}")
    else:
        logger.info(f"已加载 {len(config.nodes)} 个监控节点（来自 YAML）")
    
    # ========== 初始化默认管理员账户 ==========
    try:
        await create_default_admin()  # 从环境变量读取 ADMIN_USERNAME, ADMIN_PASSWORD
    except Exception as e:
        logger.warning(f"创建默认管理员失败: {e}")
    
    # ========== 初始化默认通知渠道 ==========
    if _db_available:
        try:
            await _init_default_notification_channels()
        except Exception as e:
            logger.warning(f"初始化通知渠道失败: {e}")
    
    logger.info("服务器监控 v%s 启动成功，初始化总耗时=%.2f秒", __version__, time.perf_counter() - startup_started)
    yield
    
    # ========== 关闭 ==========
    logger.info("正在关闭服务器监控应用...")
    
    # 停止调度器
    scheduler = get_scheduler()
    scheduler.stop()
    
    # 关闭日志
    shutdown_logging()
    
    logger.info("服务器监控已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="服务器监控面板",
    description="监控多个服务器心跳接口的状态、延迟与可用性",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ========== 全局异常处理 ==========
@app.exception_handler(404)
async def custom_404_handler(request: Request, exc):
    """
    404 处理：页面跳登录页，API 返回 JSON

    页面路径（如 /ui/nodes/xxx）跳登录页便于用户重新认证。
    但 API 路径必须返回 JSON 404 —— 否则调用方拿到的是 307 跳转
    到一个 HTML 登录页，既掩盖了"资源不存在"，也无法被程序处理。

    路由自身抛出的 HTTPException(404) 带有业务错误码（如 E003 节点不存在），
    这里必须原样透传，否则统一的处理器会把具体错误码抹成 E404。
    """
    detail = getattr(exc, "detail", None)

    path = request.url.path
    if path.startswith("/api/") or path == "/metrics":
        if detail is None:
            detail = {"code": "E404", "message": f"资源不存在: {path}"}
        return JSONResponse(status_code=404, content={"detail": detail})
    return RedirectResponse(url="/login")

# ========== 中间件 ==========
# 安全中间件（请求过滤、速率限制、安全响应头）
app.add_middleware(SecurityMiddleware)

# CORS
# 不使用通配符：allow_origins=["*"] 与 allow_credentials=True 的组合
# 会被浏览器直接拒绝（规范禁止），等于既没生效又留下错误印象。
# 前后端同源部署时 CORS 根本不需要；这里默认关闭，
# 需要跨域时在 .env 里配置明确的来源白名单。
_allowed_origins = [
    o.strip()
    for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info(f"CORS 已启用，允许来源: {_allowed_origins}")
else:
    logger.info("CORS 未启用（前后端同源部署）")

# GZip 压缩
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 可信主机校验 - 防止 Host 头攻击
# 需在 .env 中配置 ALLOWED_HOSTS（逗号分隔）才会启用。
# 不配置时不注册：默认放行，否则本地调试会被误拦。
_allowed_hosts = [
    h.strip()
    for h in os.getenv("ALLOWED_HOSTS", "").split(",")
    if h.strip()
]
if _allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)
    logger.info(f"TrustedHost 已启用，允许主机: {_allowed_hosts}")

# 认证中间件
app.add_middleware(AuthMiddleware)

# ========== 静态文件 ==========
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# ========== 路由注册 ==========
app.include_router(auth_router)  # 认证路由（登录/登出）
app.include_router(health_router)
app.include_router(nodes_router)
app.include_router(metrics_router)
app.include_router(notifications_router)  # 通知渠道路由
app.include_router(updates_router)
app.include_router(ui_router)


# ========== 根路由 ==========
@app.get("/")
async def root():
    """根路由 - 重定向到面板"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui/dashboard")


@app.get("/api")
async def api_info():
    """API 信息"""
    return {
        "name": "Server Monitor API",
        "version": __version__,
        "docs": "/docs",
        "health": "/healthz",
        "metrics": "/metrics",
    }


# ========== 爬虫协议 ==========
@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """搜索引擎协议"""
    return FileResponse("robots.txt")


@app.get("/anti-crawling.txt", include_in_schema=False)
async def anti_crawling_txt():
    """反爬虫协议说明"""
    return FileResponse("anti-crawling.txt")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """站点图标"""
    return FileResponse(Path(__file__).parent / "static" / "favicon.svg")


# ========== 主函数 ==========
if __name__ == "__main__":
    import uvicorn
    
    # 加载配置获取端口
    config = load_config()
    
    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=True,
        log_level="info",
    )
