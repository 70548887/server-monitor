"""调度器服务 - APScheduler 定时任务管理"""

import time
import asyncio
import logging
from datetime import datetime
from typing import Optional, Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import get_config, NodeConfig, NodeExpectConfig
from .heartbeat import HeartbeatService, HeartbeatResult
from .observability import metrics

logger = logging.getLogger(__name__)


class SchedulerService:
    """调度器服务"""
    
    def __init__(
        self,
        heartbeat_service: Optional[HeartbeatService] = None,
        interval_seconds: int = 60,
        concurrency: int = 50,
    ):
        self.interval_seconds = interval_seconds
        self.concurrency = concurrency
        self.heartbeat_service = heartbeat_service or HeartbeatService()
        
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._running = False
        self._on_results_callback: Optional[Callable[[list[HeartbeatResult]], Awaitable[None]]] = None
    
    def set_on_results_callback(self, callback: Callable[[list[HeartbeatResult]], Awaitable[None]]):
        """设置心跳结果回调（用于持久化和告警）"""
        self._on_results_callback = callback
    
    async def _get_nodes_from_db(self) -> list:
        """从数据库获取节点列表"""
        try:
            from app.repositories.mysql_repository import get_mysql_repository
            repo = get_mysql_repository()
            db_nodes = await repo.get_all_nodes()
            
            # 转换为 NodeConfig 格式
            nodes = []
            for n in db_nodes:
                if n.get("enabled", True):  # 只检测启用的节点
                    nodes.append(NodeConfig(
                        name=n["name"],
                        url=n["url"],
                        region=n.get("region", "default"),
                        tags=n.get("tags", []),
                        timeout_ms=n.get("timeout_ms", 2000),
                        enabled=n.get("enabled", True),
                        expect=NodeExpectConfig(
                            status=n.get("expect_status", 200),
                            body_contains=n.get("expect_body_contains"),
                        ),
                    ))
            return nodes
        except Exception as e:
            logger.error(f"从数据库获取节点失败: {e}")
            # 回退到配置文件
            config = get_config()
            return config.nodes
    
    async def _run_heartbeat_task(self):
        """执行心跳检测任务"""
        start_time = time.perf_counter()
        success = True
        
        try:
            # 从数据库获取节点列表
            nodes = await self._get_nodes_from_db()
            
            if not nodes:
                logger.warning("未配置心跳检测节点")
                return
            
            logger.info(f"开始心跳检测: {len(nodes)} 个节点")
            
            # 执行心跳检测
            results = await self.heartbeat_service.check_nodes(
                nodes=nodes,
                concurrency=self.concurrency,
            )
            
            # 统计结果
            success_count = sum(1 for r in results if r.success)
            error_count = len(results) - success_count
            
            logger.info(
                f"心跳检测完成: 总数={len(results)}, "
                f"成功={success_count}, 失败={error_count}"
            )
            
            # 回调处理结果
            if self._on_results_callback:
                try:
                    await self._on_results_callback(results)
                except Exception as e:
                    logger.error(f"结果回调错误: {e}")
            
        except Exception as e:
            success = False
            logger.error(f"心跳检测任务错误: {e}", exc_info=True)
        
        finally:
            duration = time.perf_counter() - start_time
            metrics.record_heartbeat_run(duration, success)
            metrics.scheduler_stats.status = "running" if success else "error"
    
    def start(self):
        """启动调度器"""
        if self._running:
            logger.warning("调度器已在运行")
            return
        
        self._scheduler = AsyncIOScheduler()
        
        # 添加心跳任务
        self._scheduler.add_job(
            self._run_heartbeat_task,
            trigger=IntervalTrigger(seconds=self.interval_seconds),
            id="heartbeat_task",
            name="Heartbeat Check",
            replace_existing=True,
            max_instances=1,  # 防止任务堆积
        )
        
        self._scheduler.start()
        self._running = True
        metrics.scheduler_stats.status = "running"
        
        logger.info(f"调度器已启动: 间隔={self.interval_seconds}秒")
    
    def stop(self):
        """停止调度器"""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        
        self._running = False
        metrics.scheduler_stats.status = "stopped"
        logger.info("调度器已停止")
    
    async def run_once(self) -> list[HeartbeatResult]:
        """手动执行一次心跳检测"""
        nodes = await self._get_nodes_from_db()
        
        if not nodes:
            return []
        
        results = await self.heartbeat_service.check_nodes(
            nodes=nodes,
            concurrency=self.concurrency,
        )
        
        return results
    
    @property
    def is_running(self) -> bool:
        """调度器是否运行中"""
        return self._running
    
    def get_status(self) -> dict:
        """获取调度器状态"""
        return {
            "running": self._running,
            "interval_seconds": self.interval_seconds,
            "concurrency": self.concurrency,
            "stats": {
                "status": metrics.scheduler_stats.status,
                "last_run_at": metrics.scheduler_stats.last_run_at.isoformat() if metrics.scheduler_stats.last_run_at else None,
                "runs_total": metrics.scheduler_stats.runs_total,
                "errors_total": metrics.scheduler_stats.errors_total,
                "last_duration_seconds": round(metrics.scheduler_stats.last_duration_seconds, 3),
            },
        }


# 全局调度器实例
_scheduler_service: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    """获取调度器实例"""
    global _scheduler_service
    if _scheduler_service is None:
        config = get_config()
        _scheduler_service = SchedulerService(
            interval_seconds=config.scheduler.interval_seconds,
            concurrency=config.scheduler.concurrency,
        )
    return _scheduler_service
