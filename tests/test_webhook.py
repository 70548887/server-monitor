"""
Webhook 通知测试

此前该分支是 `return False, "Webhook通知暂未实现"`，但模型、Schema、
前端表单都支持 webhook —— 用户能在面板上创建渠道并点测试，结果永远失败。
这里覆盖补齐后的发送实现。
"""

import hashlib
import hmac
import json

import httpx
import pytest

from app.services.notification import WebhookNotificationService


def make_mock_transport(handler):
    """把 WebhookNotificationService 的 httpx 调用重定向到假传输层"""
    return httpx.MockTransport(handler)


@pytest.fixture
def patch_httpx(monkeypatch):
    """
    让服务内部创建的 AsyncClient 使用假传输层

    实现里是 httpx.AsyncClient(...) 直接构造，因此替换 httpx.AsyncClient
    为绑定了 transport 的工厂。
    """
    def _install(handler):
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = make_mock_transport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return _install


class TestWebhookPayload:
    """请求体构造"""

    def test_payload_fields(self):
        payload = WebhookNotificationService._build_payload(
            alert_type="node_down",
            node_name="api-gateway",
            node_url="http://gw/health",
            subject="节点离线",
            message="连续失败 3 次",
        )
        assert payload["alert_type"] == "node_down"
        assert payload["node_name"] == "api-gateway"
        assert payload["node_url"] == "http://gw/health"
        assert payload["subject"] == "节点离线"
        assert payload["message"] == "连续失败 3 次"
        assert payload["source"] == "server-monitor"
        assert "triggered_at" in payload

    def test_payload_serializable_with_chinese(self):
        """中文内容必须能序列化，且保持可读（不转义）"""
        payload = WebhookNotificationService._build_payload(
            alert_type="node_recovery",
            node_name="分发A API",
            node_url="http://x/health",
            subject="节点恢复",
            message="已恢复正常",
        )
        body = json.dumps(payload, ensure_ascii=False)
        assert "分发A API" in body
        assert json.loads(body)["node_name"] == "分发A API"


class TestWebhookSignature:
    """签名"""

    def test_signature_is_hmac_sha256(self):
        body = b'{"a":1}'
        secret = "topsecret"
        expected = hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        assert WebhookNotificationService._sign(body, secret) == expected

    def test_signature_differs_with_secret(self):
        body = b'{"a":1}'
        assert (
            WebhookNotificationService._sign(body, "s1")
            != WebhookNotificationService._sign(body, "s2")
        )


class TestWebhookSending:
    """发送成功与失败路径"""

    async def test_success(self, patch_httpx):
        received = {}

        def handler(request):
            received["body"] = json.loads(request.content)
            received["headers"] = dict(request.headers)
            return httpx.Response(200, text="ok")

        patch_httpx(handler)
        payload = WebhookNotificationService._build_payload(
            "node_down", "n1", "http://n1/health", "s", "m"
        )

        success, error = await WebhookNotificationService.send_webhook(
            "https://hook.example.com/notify", payload
        )

        assert success is True
        assert error is None
        assert received["body"]["node_name"] == "n1"
        assert received["headers"]["content-type"].startswith("application/json")

    async def test_signature_header_sent_when_secret_set(self, patch_httpx):
        captured = {}

        def handler(request):
            captured["sig"] = request.headers.get("x-signature")
            captured["algo"] = request.headers.get("x-signature-algorithm")
            captured["raw"] = request.content
            return httpx.Response(200)

        patch_httpx(handler)
        payload = WebhookNotificationService._build_payload(
            "node_down", "n1", "http://n1", "s", "m"
        )

        await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", payload, secret="mysecret"
        )

        expected = WebhookNotificationService._sign(captured["raw"], "mysecret")
        assert captured["sig"] == expected
        assert captured["algo"] == "hmac-sha256"

    async def test_no_signature_header_without_secret(self, patch_httpx):
        captured = {}

        def handler(request):
            captured["sig"] = request.headers.get("x-signature")
            return httpx.Response(200)

        patch_httpx(handler)
        payload = WebhookNotificationService._build_payload(
            "node_down", "n1", "http://n1", "s", "m"
        )

        await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", payload
        )

        assert captured["sig"] is None

    async def test_empty_url_rejected(self):
        success, error = await WebhookNotificationService.send_webhook("", {"a": 1})
        assert success is False
        assert "为空" in error

    async def test_4xx_not_retried(self, patch_httpx, monkeypatch):
        """4xx 是配置问题，重试无意义"""
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401, text="unauthorized")

        patch_httpx(handler)
        monkeypatch.setattr(
            WebhookNotificationService, "RETRY_BASE_DELAY_SECONDS", 0.001
        )

        success, error = await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", {"a": 1}
        )

        assert success is False
        assert len(calls) == 1, "4xx 不应重试"
        assert "401" in error

    async def test_429_is_retried(self, patch_httpx, monkeypatch):
        """429 限流应重试，最终成功"""
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, text="too many requests")
            return httpx.Response(200)

        patch_httpx(handler)
        monkeypatch.setattr(
            WebhookNotificationService, "RETRY_BASE_DELAY_SECONDS", 0.001
        )

        success, _ = await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", {"a": 1}
        )

        assert success is True
        assert len(calls) == 2

    async def test_server_error_retried_then_fails(self, patch_httpx, monkeypatch):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(500, text="boom")

        patch_httpx(handler)
        monkeypatch.setattr(
            WebhookNotificationService, "RETRY_BASE_DELAY_SECONDS", 0.001
        )

        success, error = await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", {"a": 1}
        )

        assert success is False
        assert len(calls) == WebhookNotificationService.MAX_RETRIES + 1
        assert "500" in error

    async def test_connection_error_retried(self, patch_httpx, monkeypatch):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ConnectError("refused", request=request)

        patch_httpx(handler)
        monkeypatch.setattr(
            WebhookNotificationService, "RETRY_BASE_DELAY_SECONDS", 0.001
        )

        success, error = await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", {"a": 1}
        )

        assert success is False
        assert "连接失败" in error

    async def test_timeout_reported_clearly(self, patch_httpx, monkeypatch):
        def handler(request):
            raise httpx.TimeoutException("timed out", request=request)

        patch_httpx(handler)
        monkeypatch.setattr(
            WebhookNotificationService, "RETRY_BASE_DELAY_SECONDS", 0.001
        )

        success, error = await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", {"a": 1}
        )

        assert success is False
        assert "超时" in error

    async def test_2xx_range_all_succeed(self, patch_httpx):
        for code in (200, 201, 202, 204):
            def handler(request, _code=code):
                return httpx.Response(code)

            patch_httpx(handler)
            success, _ = await WebhookNotificationService.send_webhook(
                "https://hook.example.com/x", {"a": 1}
            )
            assert success is True, f"{code} 应视为成功"


class TestWebhookExponentialBackoff:
    """
    指数退避重试

    对端长时间不可用时，固定间隔重试会持续加压且不给恢复窗口。
    退避让压力随时间释放。
    """

    def test_delay_doubles(self):
        svc = WebhookNotificationService
        assert svc._retry_delay(0) == 1.0
        assert svc._retry_delay(1) == 2.0
        assert svc._retry_delay(2) == 4.0
        assert svc._retry_delay(3) == 8.0

    def test_delay_capped(self):
        """退避封顶，不会无限增长拖垮告警链路"""
        svc = WebhookNotificationService
        assert svc._retry_delay(10) == svc.MAX_RETRY_DELAY_SECONDS
        assert svc._retry_delay(100) == svc.MAX_RETRY_DELAY_SECONDS

    def test_delay_monotonic_increasing(self):
        """在封顶前严格递增"""
        svc = WebhookNotificationService
        delays = [svc._retry_delay(i) for i in range(svc.MAX_RETRIES)]
        assert delays == sorted(delays)
        assert len(set(delays)) == len(delays), "封顶前不应出现重复延迟"

    async def test_uses_backoff_not_fixed_delay(self, patch_httpx, monkeypatch):
        """重试实际等待时间应逐次翻倍"""
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        import app.services.notification as notif_mod
        monkeypatch.setattr(notif_mod.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(
            WebhookNotificationService, "RETRY_BASE_DELAY_SECONDS", 1.0
        )
        monkeypatch.setattr(
            WebhookNotificationService, "MAX_RETRY_DELAY_SECONDS", 30.0
        )

        def handler(request):
            return httpx.Response(500, text="boom")

        patch_httpx(handler)

        await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", {"a": 1}
        )

        # MAX_RETRIES=3 → 3 次重试 → 延迟 1s / 2s / 4s
        assert slept == [1.0, 2.0, 4.0], f"退避序列不符合预期: {slept}"

    async def test_total_attempts_bounded(self, patch_httpx, monkeypatch):
        """总尝试次数有上限，不会无限重试"""

        async def fake_sleep(seconds):
            pass

        import app.services.notification as notif_mod
        monkeypatch.setattr(notif_mod.asyncio, "sleep", fake_sleep)
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)

        patch_httpx(handler)

        await WebhookNotificationService.send_webhook(
            "https://hook.example.com/x", {"a": 1}
        )

        assert len(calls) == WebhookNotificationService.MAX_RETRIES + 1
        assert len(calls) <= 5, "重试次数必须有界"

    def test_no_stub_constants_left(self):
        """旧的固定间隔常量已不存在，避免误用"""
        assert not hasattr(WebhookNotificationService, "RETRY_DELAY_SECONDS")


class TestWebhookDispatch:
    """渠道分发接入"""

    def test_service_is_exported(self):
        """发送服务可被导入，由 NotificationService 分发调用"""
        from app.services.notification import WebhookNotificationService as W

        assert hasattr(W, "send_webhook")

    def test_stub_removed(self):
        """占位实现已移除 —— 否则 webhook 渠道仍会必然失败"""
        import inspect

        src = inspect.getsource(
            __import__(
                "app.services.notification", fromlist=["NotificationService"]
            ).NotificationService.send_notification
        )
        assert "未实现" not in src, "send_notification 中仍有 Webhook 占位实现"
        assert "send_webhook" in src, "send_notification 未接入 webhook 发送"
