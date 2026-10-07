"""
心跳检测测试

用 httpx.MockTransport 注入假响应，不产生真实网络请求。
"""

from datetime import datetime

import httpx
import pytest

from app.services.config import NodeConfig, NodeExpectConfig
from app.services.heartbeat import HeartbeatResult, HeartbeatService


def make_node(name="test-node", url="http://test.local/health", **kwargs):
    """构造节点配置"""
    expect = kwargs.pop("expect", None)
    params = {
        "name": name,
        "url": url,
        "region": "test",
        "tags": ["test"],
        "timeout_ms": 2000,
        "enabled": True,
        "expect": expect or NodeExpectConfig(status=200),
    }
    params.update(kwargs)
    return NodeConfig(**params)


def make_service(handler) -> HeartbeatService:
    """构造注入了假传输层的心跳服务"""
    service = HeartbeatService(timeout_seconds=2.0)
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    return service


class TestHeartbeatExecutor:
    """心跳执行"""

    async def test_successful_heartbeat(self):
        """状态码匹配时判定为成功"""
        service = make_service(lambda req: httpx.Response(200, text="ok"))
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is True
        assert result.status_code == 200
        assert result.error_type is None
        assert result.body_matched is True

    async def test_timeout_heartbeat(self):
        """请求超时归类为 Timeout"""
        def handler(request):
            raise httpx.TimeoutException("timed out", request=request)

        service = make_service(handler)
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is False
        assert result.error_type == "Timeout"
        assert result.status_code == 0

    async def test_connection_error(self):
        """连接失败归类为 ConnectError"""
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        service = make_service(handler)
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is False
        assert result.error_type == "ConnectError"

    async def test_wrong_status_code(self):
        """状态码不匹配判定为失败，且保留实际状态码"""
        service = make_service(lambda req: httpx.Response(503, text="down"))
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is False
        assert result.status_code == 503
        assert result.error_type == "StatusMismatch"
        assert "503" in (result.error_message or "")

    async def test_body_mismatch(self):
        """响应体缺少期望关键字时判定为失败"""
        node = make_node(expect=NodeExpectConfig(status=200, body_contains="healthy"))
        service = make_service(lambda req: httpx.Response(200, text="ok"))
        try:
            result = await service.check_node(node)
        finally:
            await service.close()

        assert result.success is False
        assert result.body_matched is False
        assert result.error_type == "BodyMismatch"

    async def test_body_match_succeeds(self):
        """响应体包含期望关键字时成功"""
        node = make_node(expect=NodeExpectConfig(status=200, body_contains="healthy"))
        service = make_service(
            lambda req: httpx.Response(200, text='{"status": "healthy"}')
        )
        try:
            result = await service.check_node(node)
        finally:
            await service.close()

        assert result.success is True
        assert result.body_matched is True

    async def test_latency_recorded(self):
        """延迟被记录且为非负数值"""
        service = make_service(lambda req: httpx.Response(200, text="ok"))
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert isinstance(result.latency_ms, float)
        assert result.latency_ms >= 0

    async def test_response_size_recorded(self):
        """响应体大小被记录，供后续分析使用"""
        body = "x" * 500
        service = make_service(lambda req: httpx.Response(200, text=body))
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.response_size == len(body.encode())

    async def test_result_carries_node_identity(self):
        """结果携带节点名与 URL，供上层关联"""
        node = make_node(name="api-gateway", url="http://gw.local/health")
        service = make_service(lambda req: httpx.Response(200))
        try:
            result = await service.check_node(node)
        finally:
            await service.close()

        assert result.node_name == "api-gateway"
        assert result.url == "http://gw.local/health"

    async def test_batch_execution(self):
        """批量检测返回每个节点的结果"""
        service = make_service(lambda req: httpx.Response(200, text="ok"))
        nodes = [make_node(name=f"node-{i}") for i in range(5)]
        try:
            results = await service.check_nodes(nodes)
        finally:
            await service.close()

        assert len(results) == 5
        assert {r.node_name for r in results} == {f"node-{i}" for i in range(5)}
        assert all(r.success for r in results)

    async def test_batch_records_failures_individually(self):
        """批量检测中单个节点失败不影响其他节点"""
        def handler(request):
            if "bad" in str(request.url):
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, text="ok")

        service = make_service(handler)
        nodes = [
            make_node(name="good", url="http://good.local/health"),
            make_node(name="bad", url="http://bad.local/health"),
        ]
        try:
            results = await service.check_nodes(nodes)
        finally:
            await service.close()

        by_name = {r.node_name: r for r in results}
        assert by_name["good"].success is True
        assert by_name["bad"].success is False
        assert by_name["bad"].error_type == "ConnectError"


class TestHeartbeatResult:
    """结果数据类"""

    def test_result_fields(self):
        result = HeartbeatResult(
            node_name="n1",
            url="http://n1/health",
            timestamp=datetime.now(),
            status_code=200,
            latency_ms=12.5,
            success=True,
        )
        assert result.node_name == "n1"
        assert result.success is True

    def test_result_with_error(self):
        result = HeartbeatResult(
            node_name="n1",
            url="http://n1/health",
            timestamp=datetime.now(),
            status_code=0,
            latency_ms=0.0,
            success=False,
            error_type="Timeout",
            error_message="timed out",
        )
        assert result.success is False
        assert result.error_type == "Timeout"

    def test_body_matched_defaults_true(self):
        """默认 body_matched 为 True，避免未做断言时被误判为失败"""
        result = HeartbeatResult(
            node_name="n1",
            url="http://n1/health",
            timestamp=datetime.now(),
            status_code=200,
            latency_ms=1.0,
            success=True,
        )
        assert result.body_matched is True


class TestHeartbeatRetry:
    """瞬时错误重试

    背景：app.yaml 曾声明 retry_count: 3 但代码里完全没有重试逻辑，
    属于"配置承诺了、代码没实现"的虚假承诺。此类测试防止回归。
    """

    def _service(self, handler, **kwargs):
        service = make_service(handler)
        service.retry_count = kwargs.get("retry_count", 2)
        service.retry_delay_ms = kwargs.get("retry_delay_ms", 1)
        return service

    async def test_timeout_is_retried(self):
        """超时属瞬时错误，应重试后成功"""
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) < 3:
                raise httpx.TimeoutException("timed out", request=request)
            return httpx.Response(200, text="ok")

        service = self._service(handler, retry_count=2)
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is True
        assert len(attempts) == 3, "前两次超时后第三次应成功"

    async def test_connection_error_is_retried(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) < 2:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, text="ok")

        service = self._service(handler, retry_count=2)
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is True
        assert len(attempts) == 2

    async def test_status_mismatch_not_retried(self):
        """
        状态码不符是真实故障，不重试

        重试只会推迟告警并浪费配额。
        """
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(500, text="boom")

        service = self._service(handler, retry_count=3)
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is False
        assert len(attempts) == 1, "状态码不符不应重试"

    async def test_body_mismatch_not_retried(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            # 响应体不含期望关键字 —— 真实故障，不应重试
            return httpx.Response(200, text="ERROR")

        node = make_node(expect=NodeExpectConfig(status=200, body_contains="healthy"))
        service = self._service(handler, retry_count=3)
        try:
            result = await service.check_node(node)
        finally:
            await service.close()

        assert result.success is False
        assert result.error_type == "BodyMismatch"
        assert len(attempts) == 1

    async def test_retries_exhausted(self):
        """重试用尽后仍失败，尝试次数 = retry_count + 1"""
        attempts = []

        def handler(request):
            attempts.append(1)
            raise httpx.TimeoutException("timed out", request=request)

        service = self._service(handler, retry_count=2)
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is False
        assert result.error_type == "Timeout"
        assert len(attempts) == 3

    async def test_retry_disabled_by_default_zero(self):
        """retry_count=0 时不重试（保持单次请求的原有语义）"""
        attempts = []

        def handler(request):
            attempts.append(1)
            raise httpx.TimeoutException("timed out", request=request)

        service = make_service(handler)
        service.retry_count = 0
        try:
            result = await service.check_node(make_node())
        finally:
            await service.close()

        assert result.success is False
        assert len(attempts) == 1

    async def test_metrics_recorded_once_per_check(self, monkeypatch):
        """
        重试只产生一条样本

        否则一次检测会写入多条样本，污染成功率与延迟统计。
        """
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) < 3:
                raise httpx.TimeoutException("timed out", request=request)
            return httpx.Response(200, text="ok")

        recorded = []
        import app.services.heartbeat as hb_mod

        monkeypatch.setattr(
            hb_mod.metrics, "record_heartbeat",
            lambda **kw: recorded.append(kw),
        )

        service = self._service(handler, retry_count=2)
        try:
            await service.check_node(make_node())
        finally:
            await service.close()

        assert len(attempts) == 3
        assert len(recorded) == 1, "重试期间只应记录一次指标"


class TestHeartbeatModule:
    """模块可导入性与结构"""

    def test_heartbeat_module_import(self):
        import app.services.heartbeat as mod

        assert hasattr(mod, "HeartbeatService")
        assert hasattr(mod, "HeartbeatResult")

    def test_executor_can_import(self):
        """心跳服务可实例化，且持有预期的构造参数"""
        import inspect

        sig = inspect.signature(HeartbeatService.__init__)
        for param in ("timeout_seconds", "max_connections",
                      "max_keepalive_connections"):
            assert param in sig.parameters

        service = HeartbeatService()
        assert service.timeout_seconds > 0

    def test_result_structure(self):
        """结果字段齐全，缺字段会让上层统计静默出错"""
        from dataclasses import fields

        names = {f.name for f in fields(HeartbeatResult)}
        assert names >= {
            "node_name", "url", "timestamp", "status_code", "latency_ms",
            "success", "error_type", "error_message", "response_size",
            "body_matched",
        }

    def test_latency_recording(self):
        """延迟为浮点数，供 p95 等统计使用"""
        result = HeartbeatResult(
            node_name="n1", url="u", timestamp=datetime.now(),
            status_code=200, latency_ms=42.5, success=True,
        )
        assert isinstance(result.latency_ms, float)
        assert result.latency_ms == 42.5

    def test_result_dataclass_fields(self):
        from dataclasses import fields

        names = {f.name for f in fields(HeartbeatResult)}
        for expected in (
            "node_name", "url", "timestamp", "status_code",
            "latency_ms", "success", "error_type",
        ):
            assert expected in names
