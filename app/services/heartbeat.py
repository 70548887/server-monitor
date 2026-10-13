"""心跳执行器 - 异步 HTTP 请求与结果采集"""

import time
import asyncio
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

import httpx

from ..services.config import NodeConfig
from ..services.observability import metrics
from ..utils.timeutil import utcnow

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatResult:
    """心跳检测结果"""
    node_name: str
    url: str
    timestamp: datetime
    status_code: int
    latency_ms: float
    success: bool
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    response_size: Optional[int] = None
    body_matched: bool = True
    probe_revision: Optional[str] = None


class HeartbeatService:
    """心跳检测服务"""

    # 可重试的瞬时错误。状态码不符/响应体不匹配是真实故障，
    # 重试没有意义，只会推迟告警并浪费配额。
    RETRYABLE_ERRORS = {"Timeout", "ConnectError"}

    def __init__(
        self,
        timeout_seconds: float = 5.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        retry_count: int = 0,
        retry_delay_ms: int = 200,
    ):
        self.timeout_seconds = timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None
        self._max_connections = max_connections
        self._max_keepalive = max_keepalive_connections
        # 瞬时错误（超时/连接失败）的重试次数与初始退避。
        # 抖动型故障往往第二次就成功，能显著降低误报率。
        self.retry_count = retry_count
        self.retry_delay_ms = retry_delay_ms
    
    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(
                max_connections=self._max_connections,
                max_keepalive_connections=self._max_keepalive,
            )
            self._client = httpx.AsyncClient(
                limits=limits,
                follow_redirects=True,
                verify=True,  # TLS 验证
            )
        return self._client
    
    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    async def check_node(self, node: NodeConfig) -> HeartbeatResult:
        """
        执行单个节点的心跳检测

        瞬时错误（超时/连接失败）会按配置重试，指数退避。
        抖动型故障往往第二次就成功，能显著降低误报率。
        状态码不符/响应体不匹配属于真实故障，不重试。

        Args:
            node: 节点配置

        Returns:
            HeartbeatResult 检测结果
        """
        timestamp = utcnow()
        timeout_s = node.timeout_ms / 1000.0

        start_time = time.perf_counter()
        attempts = self.retry_count + 1

        probe = None
        for attempt in range(attempts):
            probe = await self._probe(node, timeout_s)

            # 成功，或遇到不可重试的错误，直接返回
            if probe["success"] or probe["error_type"] not in self.RETRYABLE_ERRORS:
                break

            if attempt < attempts - 1:
                delay = (self.retry_delay_ms / 1000.0) * (2 ** attempt)
                logger.info(
                    f"心跳重试 {attempt + 1}/{attempts - 1}: "
                    f"节点={node.name}, 错误={probe['error_type']}, 等待 {delay * 1000:.0f}ms"
                )
                await asyncio.sleep(delay)

        latency_ms = (time.perf_counter() - start_time) * 1000

        # 更新 Prometheus 指标
        metrics.record_heartbeat(
            node_name=node.name,
            url=node.url,
            region=node.region,
            status_code=probe["status_code"],
            latency_ms=latency_ms,
            success=probe["success"],
            error_type=probe["error_type"],
        )

        logger.info(
            f"Heartbeat: node={node.name}, url={node.url}, "
            f"status={probe['status_code']}, latency={latency_ms:.2f}ms, "
            f"success={probe['success']}, error={probe['error_type']}"
        )

        return HeartbeatResult(
            node_name=node.name,
            url=node.url,
            timestamp=timestamp,
            status_code=probe["status_code"],
            latency_ms=latency_ms,
            success=probe["success"],
            error_type=probe["error_type"],
            error_message=probe["error_message"],
            response_size=probe["response_size"],
            body_matched=probe["body_matched"],
            probe_revision=node.probe_revision,
        )

    async def _probe(self, node: NodeConfig, timeout_s: float) -> dict:
        """
        单次探测请求

        不记录指标与日志 —— 那由 check_node 在重试结束后统一做一次，
        避免重试产生多条样本。
        """
        status_code = 0
        success = False
        error_type: Optional[str] = None
        error_message: Optional[str] = None
        response_size: Optional[int] = None
        body_matched = True

        try:
            client = await self._get_client()
            timeout = httpx.Timeout(timeout_s, connect=timeout_s)

            response = await client.get(node.url, timeout=timeout)
            status_code = response.status_code
            response_size = len(response.content)

            # 检查状态码
            if status_code == node.expect.status:
                success = True

                # 检查响应体关键字
                if node.expect.body_contains:
                    body_text = response.text
                    if node.expect.body_contains not in body_text:
                        success = False
                        body_matched = False
                        error_type = "BodyMismatch"
                        error_message = (
                            f"Response body does not contain: {node.expect.body_contains}"
                        )
            else:
                error_type = "StatusMismatch"
                error_message = f"Expected {node.expect.status}, got {status_code}"

        except httpx.TimeoutException as e:
            error_type = "Timeout"
            error_message = str(e)
            logger.warning(f"心跳超时: 节点={node.name}, url={node.url}, 错误={e}")

        except httpx.ConnectError as e:
            error_type = "ConnectError"
            error_message = str(e)
            logger.warning(f"心跳连接错误: 节点={node.name}, url={node.url}, 错误={e}")

        except httpx.HTTPStatusError as e:
            error_type = "HTTPStatusError"
            error_message = str(e)
            status_code = e.response.status_code if e.response else 0

        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e)
            logger.error(f"心跳错误: 节点={node.name}, url={node.url}, 错误={e}")

        return {
            "status_code": status_code,
            "success": success,
            "error_type": error_type,
            "error_message": error_message,
            "response_size": response_size,
            "body_matched": body_matched,
        }
    
    async def check_nodes(
        self,
        nodes: list[NodeConfig],
        concurrency: int = 50,
    ) -> list[HeartbeatResult]:
        """
        并发检测多个节点
        
        Args:
            nodes: 节点配置列表
            concurrency: 最大并发数
        
        Returns:
            检测结果列表
        """
        # 过滤启用的节点
        enabled_nodes = [n for n in nodes if n.enabled]
        
        if not enabled_nodes:
            return []
        
        # 使用信号量限制并发
        semaphore = asyncio.Semaphore(concurrency)
        
        async def check_with_semaphore(node: NodeConfig) -> HeartbeatResult:
            async with semaphore:
                return await self.check_node(node)
        
        # 并发执行
        tasks = [check_with_semaphore(node) for node in enabled_nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理异常结果
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                node = enabled_nodes[i]
                logger.error(f"心跳任务异常: 节点={node.name}, 错误={result}")
                final_results.append(HeartbeatResult(
                    node_name=node.name,
                    url=node.url,
                    timestamp=utcnow(),
                    status_code=0,
                    latency_ms=0,
                    success=False,
                    error_type=type(result).__name__,
                    error_message=str(result),
                    probe_revision=node.probe_revision,
                ))
            else:
                final_results.append(result)
        
        return final_results
